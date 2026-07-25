"""Parent-side supervisor: one fresh subprocess process-group per call."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ._artifacts import ArtifactError, scan

LOGGER = logging.getLogger(__name__)
PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 30.0
    cpu_seconds: int = 25
    memory_bytes: int = 1024 * 1024 * 1024
    process_count: int = 16
    stdout_bytes: int = 64 * 1024
    stderr_bytes: int = 64 * 1024
    return_value_bytes: int = 1_000_000
    result_frame_bytes: int = 2_000_000
    artifact_count: int = 16
    artifact_bytes: int = 50 * 1024 * 1024
    file_bytes: int = 50 * 1024 * 1024


DEFAULT_LIMITS = Limits()


# This dataclass, the "context" dict in the wire payload built below (adds
# workspace/uid/globals_provider), and the ctx dict _runner.py hands to a
# globals_provider (drops env/uid, adds nothing back) are three different
# shapes sharing one name across files — don't assume one where you see
# another.
@dataclass(frozen=True)
class Context:
    artifact_root: Path
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # Opaque, JSON-serializable, call-scoped config for Sandbox's globals_provider.
    # This library never reads it — it exists only for the caller's extension.
    extra: Mapping[str, Any] = field(default_factory=dict)


def _bounded_fd_read(fd: int, cap: int) -> tuple[bytes, int, bool]:
    kept = bytearray()
    total = 0
    oversized = False
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if len(kept) < cap:
                kept.extend(chunk[: cap - len(kept)])
            if total > cap:
                oversized = True
    finally:
        os.close(fd)
    return bytes(kept), total, oversized


def _restore_workspace_ownership(workspace: Path, artifact_root: Path) -> None:
    """Return retained outputs to the artifact directory's owner."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    owner = artifact_root.stat()
    for path in sorted(workspace.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
    os.chown(workspace, owner.st_uid, owner.st_gid, follow_symlinks=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _bounded_stream_read(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, int, str]:
    kept = bytearray()
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
        if len(kept) < cap:
            kept.extend(chunk[: cap - len(kept)])
    return bytes(kept), total, digest.hexdigest()


_TRUNCATION_MARKER = "\n...[truncated]"


def _decode_stdout(data: bytes, truncated: bool, cap: int) -> str:
    if not truncated:
        return data.decode("utf-8", errors="replace")
    marker = _TRUNCATION_MARKER.encode("utf-8")
    text = data[: max(0, cap - len(marker))].decode("utf-8", errors="ignore")
    return text + _TRUNCATION_MARKER


def _error(error_type: str, message: str, stdout: str = "") -> dict[str, Any]:
    return {
        "status": "error",
        "stdout": stdout,
        "return_value": None,
        "error": {"type": error_type, "message": message},
        "artifacts": [],
    }


def _parse_frame(data: bytes, oversized: bool) -> dict[str, Any]:
    if oversized:
        raise ValueError("result frame exceeds the configured byte limit")
    if not data:
        raise ValueError("child closed without a result frame")
    try:
        text = data.decode("utf-8")
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(text)
        if text[end:].strip():
            raise ValueError("multiple values in result frame")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed result frame") from exc
    if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("result frame protocol version mismatch")
    if value.get("status") not in {"ok", "error"}:
        raise ValueError("result frame status is invalid")
    return value


def _enforcement_map() -> dict[str, str]:
    """What's actually enforced per limit, on this platform, right now.

    Computed once at import time — enforcement depends only on the
    platform, which cannot change while the process is running.
    """
    linux = sys.platform.startswith("linux")
    return {
        "wall_seconds": "hard",
        "cpu_seconds": "hard_per_process" if os.name == "posix" else "unsupported",
        "memory_bytes": "best_effort_per_process" if linux else "unsupported",
        "process_count": "hard_per_user" if linux else "unsupported",
        "stdout_bytes": "hard_retained",
        "stderr_bytes": "hard_retained",
        "return_value_bytes": "hard",
        "result_frame_bytes": "hard",
        "artifact_count": "hard_post_run",
        "artifact_bytes": "hard_post_run",
        "file_bytes": "hard_per_file" if os.name == "posix" else "hard_post_run",
        "container_resources": "container",
    }


_ENFORCEMENT_MAP = _enforcement_map()


class Sandbox:
    """One fresh supervised CPython process group per call.

    Not a hostile-code sandbox on its own: it enforces POSIX rlimits, a
    dedicated non-root UID, process-group teardown, and bounded/typed I/O,
    but the surrounding container is the accepted trust boundary. Treat this
    as defense in depth, not isolation.
    """

    # Versions this class's own audit-log shape, not a Limits instance —
    # bump it if the fields in _audit's record change, not when a caller
    # tunes a Limits value.
    policy_version = "v1"

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        startup_timeout: float = 30.0,
        self_check: bool = True,
        child_uid: int = 65534,
        child_env: Mapping[str, str] | None = None,
        globals_provider: str | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self.max_concurrency = max_concurrency
        self.child_uid = child_uid
        self.globals_provider = globals_provider
        self._subreaper_enabled = self._enable_subreaper()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        if self_check:
            self._run_self_check(startup_timeout, child_env, globals_provider)

    @staticmethod
    def _enable_subreaper() -> bool:
        """Adopt and reap grandchildren on Linux after process-group teardown."""
        if not sys.platform.startswith("linux"):
            return False
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _run_self_check(timeout: float, child_env: Mapping[str, str] | None, globals_provider: str | None) -> None:
        env = dict(os.environ if child_env is None else child_env)
        argv = [sys.executable, "-m", "pyplaypen_sandbox._runner", "--self-check"]
        if globals_provider:
            argv += ["--globals-provider", globals_provider]
        try:
            result = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"sandbox self-check unavailable: {exc}") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"sandbox self-check returned invalid output: {result.stderr.strip()[:200]}") from exc
        if payload.get("status") != "ok":
            raise RuntimeError(f"sandbox self-check failed: {payload.get('message', 'unknown error')}")

    async def execute(self, code: str, context: Context, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc)
        queue_ms = 0.0
        acquired = False
        spawned = False
        workspace: Path | None = None
        result = _error("internal", "execution did not start")
        stdout_total = stderr_total = 0
        stdout_hash = stderr_hash = hashlib.sha256(b"").hexdigest()
        limits_dict = dataclasses.asdict(limits)
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=limits.wall_seconds)
                acquired = True
                queue_ms = (time.monotonic() - started) * 1000.0
            except TimeoutError:
                queue_ms = (time.monotonic() - started) * 1000.0
                result = _error("busy", "concurrency slot was unavailable before the wall deadline")
                return result

            elapsed = time.monotonic() - started
            if limits.wall_seconds - elapsed <= 0:
                result = _error("busy", "concurrency slot was unavailable before the wall deadline")
                return result

            artifact_root = context.artifact_root.resolve()
            artifact_root.mkdir(parents=True, exist_ok=True)
            workspace = artifact_root / "sandbox-runs" / context.request_id
            workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
            resolved_workspace = workspace.resolve()
            if resolved_workspace.parts[: len(artifact_root.parts)] != artifact_root.parts:
                raise RuntimeError("call workspace escaped artifact root")
            workspace = resolved_workspace
            # The parent may remain root for bind-mounted artifact compatibility;
            # the child uses a dedicated real UID so RLIMIT_NPROC is enforceable
            # (Linux exempts root from that limit).
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(workspace, self.child_uid, self.child_uid)

            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "code": code,
                "context": {
                    "request_id": context.request_id,
                    "artifact_root": str(artifact_root),
                    "workspace": str(workspace),
                    "uid": self.child_uid,
                    "globals_provider": self.globals_provider,
                    "extra": dict(context.extra),
                },
                "limits": limits_dict,
            }
            request_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            read_fd, write_fd = os.pipe()
            proc: asyncio.subprocess.Process | None = None
            spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "pyplaypen_sandbox._runner", "--result-fd", str(write_fd),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=(write_fd,), start_new_session=True, env=dict(context.env),
            ))
            try:
                # If the caller is cancelled while the event loop is spawning,
                # recover the process handle and tear it down before propagating.
                proc = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                try:
                    proc = await spawn_task
                    spawned = True
                except Exception:
                    proc = None
                finally:
                    os.close(write_fd)
                if proc is not None:
                    await self._terminate_process_group(proc)
                os.close(read_fd)
                raise
            except Exception:
                os.close(write_fd)
                os.close(read_fd)
                raise
            else:
                spawned = True
                os.close(write_fd)

            assert proc is not None and proc.stdin is not None
            assert proc.stdout is not None and proc.stderr is not None
            result_task = asyncio.create_task(asyncio.to_thread(_bounded_fd_read, read_fd, limits.result_frame_bytes))
            stdout_task = asyncio.create_task(_bounded_stream_read(proc.stdout, limits.stdout_bytes))
            stderr_task = asyncio.create_task(_bounded_stream_read(proc.stderr, limits.stderr_bytes))
            try:
                proc.stdin.write(request_bytes)
                await proc.stdin.drain()
                proc.stdin.close()
                await proc.stdin.wait_closed()
                remaining = limits.wall_seconds - (time.monotonic() - started)
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    frame_tuple = await asyncio.wait_for(asyncio.shield(result_task), timeout=remaining)
                except TimeoutError:
                    await self._terminate_process_group(proc)
                    stdout_data, stdout_total, stdout_hash = await stdout_task
                    _stderr_data, stderr_total, stderr_hash = await stderr_task
                    await result_task
                    result = _error(
                        "timeout", "execution exceeded the wall-clock limit",
                        _decode_stdout(stdout_data, stdout_total > limits.stdout_bytes, limits.stdout_bytes),
                    )
                    return result
                # A valid child lifecycle ends when the dedicated result pipe closes.
                # Kill any background descendants before waiting for inherited output
                # pipes, which those descendants could otherwise keep open forever.
                await self._terminate_process_group(proc)
                _restore_workspace_ownership(workspace, artifact_root)
            except (asyncio.CancelledError, Exception):
                # Cancellation and broken stdin/pipes/unexpected supervisor
                # failures share one no-orphan teardown path. Listed together
                # because CancelledError is a BaseException, not an Exception,
                # since Python 3.8 — `except Exception` alone would miss it.
                await self._terminate_process_group(proc)
                drained = await asyncio.gather(stdout_task, stderr_task, result_task, return_exceptions=True)
                if isinstance(drained[0], tuple):
                    _data, stdout_total, stdout_hash = drained[0]
                if isinstance(drained[1], tuple):
                    _data, stderr_total, stderr_hash = drained[1]
                raise

            stdout_data, stdout_total, stdout_hash = await stdout_task
            stderr_data, stderr_total, stderr_hash = await stderr_task
            frame_data, _frame_total, frame_oversized = frame_tuple
            stdout = _decode_stdout(stdout_data, stdout_total > limits.stdout_bytes, limits.stdout_bytes)
            try:
                frame = _parse_frame(frame_data, frame_oversized)
            except ValueError as exc:
                error_type = self._classify_exit(proc.returncode)
                result = _error(error_type or "protocol", str(exc), stdout)
                if stderr_data:
                    result["error"]["details"] = {"stderr": stderr_data.decode("utf-8", errors="replace")}
                return result

            artifacts = frame.get("artifacts", [])
            if frame["status"] == "ok":
                try:
                    # Child enumeration enables return-path translation; this
                    # parent snapshot is authoritative after descendants stop.
                    artifacts = scan(workspace, artifact_root, limits_dict)
                except ArtifactError as exc:
                    result = _error("artifact_limit", str(exc), stdout)
                    return result
            result = {
                "status": frame["status"],
                "stdout": stdout,
                "return_value": frame.get("return_value"),
                "error": frame.get("error"),
                "artifacts": artifacts,
            }
            if result["status"] == "error" and stderr_data:
                result["error"].setdefault("details", {})["stderr"] = stderr_data.decode("utf-8", errors="replace")
            return result
        except asyncio.CancelledError:
            result = _error("cancelled", "execution was cancelled")
            raise
        except Exception as exc:
            result = _error("internal", f"sandbox supervisor failure: {type(exc).__name__}: {exc}")
            return result
        finally:
            if workspace is not None:
                failed = result.get("status") != "ok"
                if failed or not any(workspace.rglob("*")):
                    try:
                        shutil.rmtree(workspace)
                        parent = workspace.parent
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                    except OSError:
                        if failed and isinstance(result.get("error"), dict):
                            result["error"]["cleanup_failed"] = True
            if acquired:
                self._semaphore.release()
            self._audit(
                context, limits_dict, code, started, started_at, queue_ms, result,
                stdout_total, stdout_hash, stderr_total, stderr_hash, spawned,
            )

    @staticmethod
    def _classify_exit(returncode: int | None) -> str | None:
        if returncode is None or returncode >= 0:
            return None
        signum = -returncode
        if signum == getattr(signal, "SIGXCPU", -1):
            return "cpu_limit"
        if signum == getattr(signal, "SIGXFSZ", -1):
            return "artifact_limit"
        return "crash"

    @staticmethod
    async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
        """Terminate the child and any descendants, even if the leader exited."""
        def group_exists() -> bool:
            try:
                os.killpg(proc.pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 1.0
        while group_exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if group_exists():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()
        # With PR_SET_CHILD_SUBREAPER, terminated grandchildren are reparented
        # here. Reap only this request's process group so concurrent calls do
        # not consume each other's exit statuses.
        if sys.platform.startswith("linux"):
            for _ in range(100):
                reaped = False
                while True:
                    try:
                        pid, _status = os.waitpid(-proc.pid, os.WNOHANG)
                    except ChildProcessError:
                        return
                    if pid == 0:
                        break
                    reaped = True
                if not group_exists():
                    return
                if not reaped:
                    await asyncio.sleep(0.01)

    @staticmethod
    def _artifact_hashes(context: Context, artifacts: list[dict[str, Any]]) -> list[dict[str, str]]:
        root = context.artifact_root.resolve()
        hashes: list[dict[str, str]] = []
        for item in artifacts:
            try:
                relative = Path(str(item["path"]))
                path = (root / relative).resolve()
                if relative.is_absolute() or path.parts[: len(root.parts)] != root.parts or not path.is_file():
                    continue
                hashes.append({"path": str(relative), "sha256": _sha256_file(path)})
            except (KeyError, OSError):
                continue
        return hashes

    def _audit(
        self, context: Context, limits_dict: dict[str, Any], code: str, started: float,
        started_at: datetime, queue_ms: float, result: dict[str, Any],
        stdout_bytes: int, stdout_sha256: str, stderr_bytes: int,
        stderr_sha256: str, spawned: bool,
    ) -> None:
        artifacts = result.get("artifacts") or []
        record = {
            "event": "sandbox_execution",
            "request_id": context.request_id,
            "policy_version": self.policy_version,
            "effective_limits": limits_dict,
            "enforcement": _ENFORCEMENT_MAP,
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "queue_ms": round(queue_ms, 3),
            "spawned": spawned,
            "status": result.get("status", "error"),
            "failure_type": (result.get("error") or {}).get("type", ""),
            "stdout_bytes": stdout_bytes,
            "stdout_sha256": stdout_sha256,
            "stderr_bytes": stderr_bytes,
            "stderr_sha256": stderr_sha256,
            "artifact_count": len(artifacts),
            "artifact_bytes": sum(int(item.get("bytes", 0)) for item in artifacts),
            "artifact_hashes": self._artifact_hashes(context, artifacts),
        }
        LOGGER.info(json.dumps(record, separators=(",", ":"), sort_keys=True))
