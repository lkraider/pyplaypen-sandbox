"""Workspace-to-artifact enumeration, shared by the child and parent scans.

The child's scan (in _runner.py) is provisional and used only to translate
paths in the return value; the parent's post-teardown scan is authoritative
because background descendants can still be writing until the process group
is fully dead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

_MIME_MAP: dict[str, str] = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/jsonlines",
    ".txt": "text/plain",
    ".parquet": "application/parquet",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


def guess_mime(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")


class ArtifactError(ValueError):
    pass


def scan(workspace: Path, artifact_root: Path, limits: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Enumerate workspace files as artifacts, enforcing size/count limits.

    Rejects symlinks and any path that escapes the workspace (belt-and-braces;
    the workspace is created fresh per call so escape would mean a bug
    upstream, not an actual attack surface).
    """
    artifacts: list[dict[str, Any]] = []
    total = 0
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            raise ArtifactError("symbolic links are not valid artifacts")
        if not path.is_file():
            continue
        # A hardlink shares an inode with a file that may live outside the
        # workspace, so its resolved path stays inside and passes the escape
        # check below while its content escaped it. The workspace is created
        # empty per call, so any extra link is one the child added.
        if path.stat().st_nlink > 1:
            raise ArtifactError("hard links are not valid artifacts")
        relative = path.relative_to(workspace)
        if any(part.startswith(".") for part in relative.parts):
            continue
        resolved = path.resolve()
        if resolved.parts[: len(workspace.parts)] != workspace.parts:
            raise ArtifactError("artifact path escapes the call workspace")
        size = path.stat().st_size
        total += size
        if size > int(limits["file_bytes"]):
            raise ArtifactError("artifact per-file byte limit exceeded")
        artifacts.append({
            "path": str(path.relative_to(artifact_root)),
            "name": path.name,
            "bytes": size,
            "mime_type": guess_mime(path),
        })
    if len(artifacts) > int(limits["artifact_count"]):
        raise ArtifactError("artifact count limit exceeded")
    if total > int(limits["artifact_bytes"]):
        raise ArtifactError("artifact aggregate byte limit exceeded")
    return artifacts
