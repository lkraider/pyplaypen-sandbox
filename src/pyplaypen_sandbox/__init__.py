from pathlib import Path
from typing import Any, Mapping

from .supervisor import DEFAULT_LIMITS, Context, Limits, Sandbox

__all__ = ["Sandbox", "Context", "Limits", "DEFAULT_LIMITS", "run"]
__version__ = "0.1.0"


async def run(
    code: str,
    *,
    artifact_dir: str | Path = ".",
    env: Mapping[str, str] | None = None,
    limits: Limits = DEFAULT_LIMITS,
    sandbox: Sandbox | None = None,
) -> dict[str, Any]:
    """One-off convenience wrapper: spins up a Sandbox (or reuses the one
    passed in) and runs a single call. For repeated calls, construct a
    Sandbox yourself to reuse its self-check and concurrency semaphore.
    """
    backend = sandbox or Sandbox(self_check=False)
    kwargs: dict[str, Any] = {} if env is None else {"env": dict(env)}
    context = Context(artifact_root=Path(artifact_dir), **kwargs)
    return await backend.execute(code, context, limits)
