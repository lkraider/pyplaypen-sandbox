"""Trusted one-shot child: parses a JSON request from stdin, execs the code,
writes a JSON result frame to a dedicated fd. Launched only by Sandbox.execute.

Not a hostile-code sandbox by itself — the container around the whole process
is the accepted trust boundary. This is defense in depth on top of that:
rlimits, UID drop, and bounded/typed I/O so a buggy or resource-hungry script
can't take the host down or return something that breaks the caller.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import json
import math
import os
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._artifacts import ArtifactError, scan
from .privilege import apply_resource_limits, drop_root_privileges

PROTOCOL_VERSION = 1
MAX_PROJECTION_DEPTH = 50


class ProjectionError(ValueError):
    pass


def _project(value: Any, *, seen: set[int] | None = None, depth: int = 0) -> Any:
    """Project a Python (optionally numpy/pandas) value into JSON-safe data.

    numpy/pandas are imported lazily and only touched if the value's own
    module says so, so neither is a hard dependency of this library.
    """
    if depth > MAX_PROJECTION_DEPTH:
        raise ProjectionError("return value exceeds maximum nesting depth")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectionError("non-finite float is not JSON serializable")
        return value
    if isinstance(value, (datetime, date, time, Decimal, Path)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    module = type(value).__module__.split(".", 1)[0]
    if module == "numpy":
        import numpy as np
        if isinstance(value, np.ndarray):
            return _project(value.tolist(), seen=seen, depth=depth + 1)
        if isinstance(value, np.generic):
            return _project(value.item(), seen=seen, depth=depth + 1)
    if module == "pandas":
        import pandas as pd
        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, pd.DataFrame):
            return {
                "columns": [str(item) for item in value.columns.tolist()],
                "rows": _project(value.values.tolist(), seen=seen, depth=depth + 1),
                "row_count": int(len(value)),
            }
        if isinstance(value, pd.Series):
            return {
                "name": None if value.name is None else str(value.name),
                "index": _project(value.index.tolist(), seen=seen, depth=depth + 1),
                "values": _project(value.tolist(), seen=seen, depth=depth + 1),
            }

    if isinstance(value, (list, tuple, set, frozenset, dict)):
        identity = id(value)
        active = set() if seen is None else seen
        if identity in active:
            raise ProjectionError("cyclic return value is not supported")
        active.add(identity)
        try:
            if isinstance(value, dict):
                return {
                    str(key): _project(item, seen=active, depth=depth + 1)
                    for key, item in value.items()
                }
            return [_project(item, seen=active, depth=depth + 1) for item in value]
        finally:
            active.remove(identity)

    raise ProjectionError(f"unsupported return type: {type(value).__name__}")


def _replace_paths(value: Any, translations: dict[str, str]) -> Any:
    if isinstance(value, str):
        return translations.get(value, value)
    if isinstance(value, list):
        return [_replace_paths(item, translations) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, translations) for key, item in value.items()}
    return value


def _provisional_artifacts(workspace: Path, artifact_root: Path, limits: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Scan once; scan() already excludes symlinks/dotfiles/dirs, so build
    both relative- and absolute-path translations straight from its result
    instead of re-walking the workspace a second time."""
    artifacts = scan(workspace, artifact_root, limits)
    workspace_prefix = workspace.relative_to(artifact_root)
    translations: dict[str, str] = {}
    for artifact in artifacts:
        global_path = artifact["path"]
        relative = Path(global_path).relative_to(workspace_prefix)
        translations[str(relative)] = global_path
        translations[str(workspace / relative)] = global_path
    return artifacts, translations


def _load_globals(provider_path: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Import 'module:function', call it with a JSON-safe context, and merge
    the returned dict into the exec namespace. Runs in the child, after the
    privilege drop, so an extension gets no more trust than user code — it
    just gets to add names (numpy, pandas, an HTTP client, whatever the
    caller's own module imports) that this library never has to depend on.
    """
    module_name, _, func_name = provider_path.partition(":")
    if not func_name:
        raise ValueError("globals_provider must be 'module:function'")
    provider = getattr(importlib.import_module(module_name), func_name)
    result = provider(ctx)
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        raise ValueError("globals_provider must return a dict[str, Any]")
    return result


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "error",
        "return_value": None,
        "error": {"type": error_type, "message": message},
        "artifacts": [],
    }


def _validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported or missing protocol_version")
    if not isinstance(payload.get("code"), str):
        raise ValueError("code must be a string")
    if not isinstance(payload.get("context"), dict) or not isinstance(payload.get("limits"), dict):
        raise ValueError("context and limits must be objects")
    return payload


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload["context"]
    limits = payload["limits"]
    artifact_root = Path(context["artifact_root"]).resolve()
    workspace = Path(context["workspace"]).resolve()
    if workspace.parts[: len(artifact_root.parts)] != artifact_root.parts:
        return _error("internal", "workspace is outside the artifact root")

    apply_resource_limits(limits)
    drop_root_privileges(int(context["uid"]))
    os.chdir(workspace)
    globals_map: dict[str, Any] = {"__name__": "__sandbox_code__", "__builtins__": builtins.__dict__}
    provider_path = context.get("globals_provider")
    if provider_path:
        try:
            globals_map.update(_load_globals(provider_path, {
                "request_id": context["request_id"],
                "workspace": str(workspace),
                "artifact_root": str(artifact_root),
                "extra": context.get("extra", {}),
            }))
        except Exception as exc:
            return _error("extension", f"globals_provider failed: {type(exc).__name__}: {exc}")
    try:
        tree = ast.parse(payload["code"], filename="<sandbox_code>", mode="exec")
    except SyntaxError as exc:
        return _error("syntax", f"{exc.msg} (line {exc.lineno})")

    try:
        final_expression = tree.body[-1] if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        statements = tree.body[:-1] if final_expression is not None else tree.body
        if statements:
            module = ast.Module(body=statements, type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, "<sandbox_code>", "exec"), globals_map, globals_map)
        result = None
        if final_expression is not None:
            expression = ast.Expression(final_expression.value)
            ast.fix_missing_locations(expression)
            result = eval(compile(expression, "<sandbox_code>", "eval"), globals_map, globals_map)
        projected = _project(result)
        artifacts, translations = _provisional_artifacts(workspace, artifact_root, limits)
        projected = _replace_paths(projected, translations)
        encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > int(limits["return_value_bytes"]):
            return _error("return_limit", "serialized return value exceeds the configured byte limit")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": "ok",
            "return_value": projected,
            "error": None,
            "artifacts": artifacts,
        }
    except ProjectionError as exc:
        return _error("serialization", str(exc))
    except ArtifactError as exc:
        return _error("artifact_limit", str(exc))
    except MemoryError:
        return _error("memory_limit", "memory limit exceeded")
    except OSError as exc:
        if getattr(exc, "errno", None) == 11:
            return _error("process_limit", "process limit exceeded")
        return _error("runtime", f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        return _error("runtime", f"{type(exc).__name__}: {exc}")


def _write_frame(fd: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with os.fdopen(fd, "wb", closefd=True) as stream:
        stream.write(data)
        stream.flush()


def _self_check(provider_path: str | None) -> int:
    if provider_path:
        module_name, _, func_name = provider_path.partition(":")
        try:
            if not func_name or not hasattr(importlib.import_module(module_name), func_name):
                raise ImportError(f"{func_name!r} not found in {module_name!r}")
        except Exception as exc:
            print(json.dumps({"status": "error", "message": f"globals_provider: {exc}"}, separators=(",", ":")))
            return 1
    print(json.dumps({"status": "ok", "python": sys.version.split()[0]}, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-fd", type=int)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--globals-provider")
    args = parser.parse_args()
    if args.self_check:
        return _self_check(args.globals_provider)
    if args.result_fd is None:
        parser.error("--result-fd is required")
    try:
        payload = _validate_request(json.load(sys.stdin))
        result = _run(payload)
    except Exception as exc:
        result = _error("internal", f"runner request failure: {type(exc).__name__}: {exc}")
    _write_frame(args.result_fd, result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
