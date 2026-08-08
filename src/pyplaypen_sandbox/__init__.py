from pathlib import Path
from typing import Any, Mapping

from .privilege import apply_resource_limits, drop_root_privileges
from .supervisor import DEFAULT_LIMITS, Context, Limits, Sandbox

__all__ = [
    "Sandbox", "Context", "Limits", "DEFAULT_LIMITS", "run",
    "apply_resource_limits", "drop_root_privileges",
]
__version__ = "0.4.0"


async def run(
    code: str,
    *,
    artifact_dir: str | Path = ".",
    env: Mapping[str, str] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    sandbox: Sandbox | None = None,
    warn_only: bool = False,
) -> dict[str, Any]:
    """One-off convenience wrapper: spins up a Sandbox (or reuses the one
    passed in) and runs a single call. For repeated calls, construct a
    Sandbox yourself to reuse its self-check and concurrency semaphore.
    warn_only lets the throwaway Sandbox construct on a deployment that can't
    enforce every limit instead of failing the enforcement gate; ignored when
    a sandbox is passed in (it already made that choice).
    """
    backend = sandbox or Sandbox(self_check=False, warn_only=warn_only)
    kwargs: dict[str, Any] = {} if env is None else {"env": dict(env)}
    context = Context(artifact_root=Path(artifact_dir), **kwargs)
    return await backend.execute(code, context, limits)
