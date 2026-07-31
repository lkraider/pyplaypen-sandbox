"""POSIX rlimit application and real UID drop — the two primitives behind
every guarantee this library makes. Public and dependency-free on purpose:
call apply_resource_limits() then drop_root_privileges() inside any child
(a preexec_fn, a bootstrap script, whatever) to get the same confinement
without adopting Sandbox at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

RUNTIME_HOME = Path("/run/pyplaypen-sandbox")


def apply_resource_limits(limits: Mapping[str, Any]) -> None:
    """Set RLIMIT_CPU/RLIMIT_FSIZE/RLIMIT_NOFILE everywhere POSIX,
    RLIMIT_AS/RLIMIT_NPROC on Linux only (unsupported elsewhere). Call before
    drop_root_privileges, and before running anything caller-supplied.
    Requires limits['cpu_seconds'], ['file_bytes'], ['open_files'],
    ['memory_bytes'], ['process_count'].
    """
    try:
        import resource
    except ImportError:
        return

    def apply(name: str, soft: int, hard: int | None = None) -> None:
        resource_name = getattr(resource, name, None)
        if resource_name is None:
            return
        resource.setrlimit(resource_name, (soft, soft if hard is None else hard))

    apply("RLIMIT_CPU", int(limits["cpu_seconds"]), int(limits["cpu_seconds"]) + 1)
    # Permit one sentinel byte so a short write caused by RLIMIT_FSIZE is
    # detectable by a post-run configured-size check.
    apply("RLIMIT_FSIZE", int(limits["file_bytes"]) + 1)
    apply("RLIMIT_NOFILE", int(limits["open_files"]))
    if sys.platform.startswith("linux"):
        # Linux-only for a concrete reason, not tidiness: on Darwin RLIMIT_AS
        # aliases RLIMIT_RSS and setrlimit rejects a real cap (ValueError), so
        # applying it off-Linux would crash the child, not silently no-op.
        apply("RLIMIT_AS", int(limits["memory_bytes"]))
        # RLIMIT_NPROC is per real UID, system-wide, not per process group.
        # It's only safe to set when this process is about to drop to its
        # own dedicated UID (see drop_root_privileges): only then does the
        # limit bound this call's own process count. Applied while still
        # sharing the ambient UID, it would cap every other process already
        # running as that UID, sandboxed or not.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            apply("RLIMIT_NPROC", int(limits["process_count"]))


def drop_root_privileges(uid: int) -> None:
    """No-op unless running as root. Linux exempts root from RLIMIT_NPROC,
    so this is what makes that limit real. Sets HOME to a dedicated,
    uid-owned directory (RUNTIME_HOME) since the original HOME is usually
    root's and unreadable by the dropped uid; leaves everything else in the
    environment alone.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    RUNTIME_HOME.mkdir(mode=0o700, exist_ok=True)
    os.chown(RUNTIME_HOME, uid, uid)
    os.environ["HOME"] = str(RUNTIME_HOME)
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
