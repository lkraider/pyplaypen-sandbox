"""Command line front end for Sandbox.run_process: bound a program's
resources without importing anything, so a harness can substitute this for
`python` through a PATH shim.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import math
import os
import signal
import sys
from pathlib import Path
from typing import Any, NoReturn

from .supervisor import DEFAULT_LIMITS, Limits, Sandbox

PREFIX = "pyplaypen:"


def _fail(message: str) -> NoReturn:
    print(f"{PREFIX} {message}", file=sys.stderr)
    raise SystemExit(2)


def _parse_limits(pairs: list[str]) -> dict[str, Any]:
    """name=value pairs; later ones win, so PYPLAYPEN_LIMITS sets policy once
    and --limit overrides it per call."""
    values: dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or name not in Limits.__dataclass_fields__:
            _fail(f"unknown limit {pair!r}. Valid: {', '.join(Limits.__dataclass_fields__)}")
        # RLIMIT_CPU quantizes to whole seconds while wall_seconds is an
        # asyncio timer, so cpu_seconds=1.5 must be refused and
        # wall_seconds=1.5 accepted. supervisor.py's postponed annotations
        # turn __dataclass_fields__[name].type into the string "float", so
        # the default's runtime type is what's usable here.
        coerce = type(getattr(DEFAULT_LIMITS, name))
        try:
            value = coerce(raw)
        except ValueError:
            _fail(f"limit {name} expects {coerce.__name__}, got {raw!r}")
        # On Linux, the deployment target, resource.RLIM_INFINITY is a
        # negative int, so --limit memory_bytes=-1 would silently remove that
        # cap and escape a PYPLAYPEN_LIMITS policy. wall_seconds=inf removes
        # the wall clock the same way. wall_seconds=nan reaches
        # asyncio.wait_for and crashes the event loop (TypeError from the
        # selector).
        if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            _fail(f"limit {name} must be a non-negative finite {coerce.__name__}, got {raw!r}")
        values[name] = value
    return values


def _build_sandbox() -> Sandbox:
    # The library gate refuses wholesale because Limits cannot distinguish a
    # requested value from a defaulted one, which would reject the non-root
    # shape this CLI targets; _run checks requested limits instead. The gate's
    # warn_only warning names a Python kwarg a CLI user cannot pass, and
    # WARNING reaches logging.lastResort with no logging configured, so it
    # would print on every invocation. Mutes that by configuring the
    # process-global "pyplaypen_sandbox" logger before Sandbox() runs the gate.
    audit = os.environ.get("PYPLAYPEN_AUDIT") == "1"
    logger = logging.getLogger("pyplaypen_sandbox")
    logger.propagate = audit
    logger.handlers = [] if audit else [logging.NullHandler()]
    if audit:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return Sandbox(self_check=False, warn_only=True)


def _outcome(result: dict[str, Any], limits: Limits) -> tuple[int, str | None]:
    """Exit code and stderr diagnostic, from one read of status/returncode:
    memory_bytes surfaces as MemoryError and open_files as EMFILE, both rc=1,
    and reading those out of stderr would be Python-specific and wrong for
    other targets."""
    status, rc = result["status"], result["returncode"]
    if status == "timeout":
        # GNU timeout's convention
        return 124, f"wall_seconds={limits.wall_seconds} exceeded; process group terminated"
    if status != "ok":
        return 125, f"{status}: the call did not run to completion"
    rc = rc or 0
    if rc >= 0:
        return rc, None
    code = 128 - rc  # shell convention for a signalled child
    named = {
        signal.SIGXCPU: f"cpu_seconds={limits.cpu_seconds} exceeded",
        signal.SIGXFSZ: f"file_bytes={limits.file_bytes} exceeded",
        signal.SIGKILL: "killed by SIGKILL — likely the container memory cgroup, not a pyplaypen limit",
    }
    if -rc in named:
        return code, named[-rc]
    try:
        return code, f"killed by {signal.Signals(-rc).name}"
    except ValueError:
        return code, f"killed by signal {-rc}"


def _run(target: list[str], requested: dict[str, Any]) -> None:
    sandbox = _build_sandbox()
    unsupported = [k for k, v in sandbox.enforcement.items() if v == "unsupported"]
    refused = [k for k in unsupported if k in requested]
    if refused:
        _fail(
            f"{', '.join(refused)} is not enforced here. Run the container with "
            "--pids-limit=N, or run it as root so each call drops to a dedicated "
            "UID. Drop the flag to proceed without it. See: pyplaypen enforcement"
        )
    if unsupported and os.environ.get("PYPLAYPEN_QUIET") != "1":
        print(f"{PREFIX} not enforced here: {', '.join(unsupported)}", file=sys.stderr)

    limits = dataclasses.replace(DEFAULT_LIMITS, **requested)
    cwd = Path.cwd()
    # Under root run_process chowns cwd to the child uid and, unlike execute(),
    # never restores it, because its callers own a scratch workspace. Here cwd
    # is the user's project directory.
    owner = os.stat(cwd) if hasattr(os, "geteuid") and os.geteuid() == 0 else None
    try:
        result = asyncio.run(sandbox.run_process(target, cwd=cwd, limits=limits))
    finally:
        if owner is not None:
            os.chown(cwd, owner.st_uid, owner.st_gid)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    code, note = _outcome(result, limits)
    if note:
        print(f"{PREFIX} {note}", file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="pyplaypen")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="run a program under enforced limits")
    runner.add_argument("--limit", action="append", default=[], metavar="NAME=VALUE")
    commands.add_parser("enforcement", help="print what each limit enforces here")
    # Split on the first '--' so the target keeps its own separator:
    # pyplaypen run -- pytest -- -k foo.
    sep = argv.index("--") if "--" in argv else None
    args = parser.parse_args(argv if sep is None else argv[:sep])

    if args.command == "enforcement":
        for key, value in _build_sandbox().enforcement.items():
            print(f"{key}: {value}")
        raise SystemExit(0)

    if sep is None:
        _fail("expected '--' followed by the target argv")
    target = argv[sep + 1:]
    if not target:
        _fail("empty target argv after '--'")
    # stdin is DEVNULL in run_process, so `python -` would exit 0 without
    # running anything.
    if (len(target) == 1 and Path(target[0]).name.startswith("python")) or target[1:2] == ["-"]:
        _fail("stdin is not available under pyplaypen run")

    env_limits = [pair.strip() for pair in os.environ.get("PYPLAYPEN_LIMITS", "").split(",") if pair.strip()]
    _run(target, _parse_limits([*env_limits, *args.limit]))
