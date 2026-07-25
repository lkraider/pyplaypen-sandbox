# pyplaypen-sandbox

One-shot subprocess sandbox for running Python inside a container.

**Not a hostile-code sandbox.** The container around this process is the
accepted trust boundary. This library adds defense in depth inside that
boundary; it does not hold against code that deliberately tries to escape
it. For that, use gVisor, Firecracker, or a VM.

## What it does

Each call to `Sandbox.execute` forks a fresh child, in its own process group,
that:

- runs your code with a wall-clock timeout that kills the whole process
  group (`os.killpg`), not just the immediate child, so background
  descendants can't outlive the call
- applies POSIX rlimits pre-exec: CPU seconds, address space (memory),
  process count, max file size
- drops from root to a dedicated non-root UID before running your code
  (Linux exempts root from `RLIMIT_NPROC`, so this matters if the parent
  runs as root)
- enables `PR_SET_CHILD_SUBREAPER` on Linux so orphaned grandchildren still
  get reaped after the process group is torn down
- captures stdout/stderr bounded and SHA-256-hashed, and returns the child's
  final expression value as JSON (plain Python types only, by default —
  anything else needs a `type_projector`, see "Extending it")
- collects files written into the call's workspace as artifacts, rejecting
  symlinks and any path that would escape the workspace, with per-file,
  aggregate, and count byte limits
- logs a structured audit record per call including exactly what was
  enforced vs. best-effort vs. unsupported on the current platform (see
  `_enforcement_map` in `supervisor.py`)

## For coding agents

For a single one-off call:

```
Use https://github.com/lkraider/pyplaypen-sandbox to run this Python
snippet in an isolated subprocess and return the result: <code>
```

For a project integration, read "Extending it" and "Lower-level building
blocks" below first. The decision points are `execute()` vs.
`run_process()`, whether a `globals_provider` or `type_projector` is
warranted, and what `Limits` fit the workload. Write the integration the
project needs instead of copying the examples verbatim.

Copy-paste to a coding agent, for a project integration:

```
Set up a process sandbox in this project using
https://github.com/lkraider/pyplaypen-sandbox. Read the README fully
first, then decide the integration this project needs.
```

## Install

```
pip install pyplaypen-sandbox
```

No dependencies, required or optional. See `type_projector` below if you
need numpy/pandas, or anything else, in a return value.

## Usage

```python
import asyncio
from pyplaypen_sandbox import run

async def main():
    result = await run("1 + 1", artifact_dir="./artifacts")
    print(result)  # {"status": "ok", "return_value": 2, ...}

asyncio.run(main())
```

For repeated calls, construct a `Sandbox` once (it runs a startup self-check
and owns a concurrency semaphore) and reuse it:

```python
from pyplaypen_sandbox import Sandbox, Context, Limits

sandbox = Sandbox(max_concurrency=4)
result = await sandbox.execute(
    code, Context(artifact_root=Path("./artifacts")), Limits(wall_seconds=10),
)
```

## Result shape

```python
{
    "status": "ok" | "error",
    "return_value": <JSON value or None>,
    "stdout": "<bounded, possibly truncated>",
    "error": None | {"type": "...", "message": "..."},
    "artifacts": [{"path": ..., "name": ..., "bytes": ..., "mime_type": ...}],
}
```

`error.type` is one of: `syntax`, `runtime`, `serialization`, `timeout`,
`memory_limit`, `process_limit`, `artifact_limit`, `return_limit`, `busy`,
`cancelled`, `crash`, `protocol`, `internal`, `extension`.

## Extending it

This library has no built-in helpers and no dependency on numpy, pandas,
httpx, duckdb, or anything else sandboxed code might need. To let
sandboxed code call out to something — an HTTP client, a query engine,
whatever — give `Sandbox` an import path to a factory function. It runs
inside the child, after the privilege drop, so an extension is bound by
the same rlimits as user code, and its imports live in your module.

```python
# yourpkg/sandbox_ext.py
def build_globals(ctx: dict) -> dict:
    import httpx  # your dependency, not pyplaypen_sandbox's
    def fetch(url: str) -> str:
        return httpx.get(url, timeout=ctx["extra"]["timeout"]).text
    return {"fetch": fetch}
```

```python
sandbox = Sandbox(globals_provider="yourpkg.sandbox_ext:build_globals")
context = Context(artifact_root=Path("./artifacts"), extra={"timeout": 5.0})
result = await sandbox.execute('fetch("https://example.com")', context)
```

`ctx` is `{"request_id", "workspace", "artifact_root", "extra"}`. `extra`
is whatever JSON-serializable config you passed on `Context`; this library
never reads it. A bad provider (missing module, wrong return type) fails
with `error.type == "extension"`, not a user-code error type. With
`self_check=True` (the default), that failure is caught when `Sandbox()`
is constructed, before the first call.

The same mechanism extends what a return value can be. `_project()`, the
function that turns your final expression into JSON, knows only plain
Python types. Anything else needs a `type_projector`: one function that
takes an unsupported value and returns something projectable (plain data,
or another unsupported value, since it recurses):

```python
# yourpkg/sandbox_ext.py
def project(value):
    if isinstance(value, YourType):
        return {"field": value.field}
    raise ValueError(f"no projection for {type(value).__name__}")
```

```python
sandbox = Sandbox(type_projector="yourpkg.sandbox_ext:project")
```

numpy/pandas support lives in `pyplaypen_sandbox.projectors` as the
shipped example of this mechanism, and works as-is:

```python
sandbox = Sandbox(type_projector="pyplaypen_sandbox.projectors:project_numpy_pandas")
result = await sandbox.execute("import numpy as np\nnp.array([1, 2])", context)
# {"status": "ok", "return_value": [1, 2], ...}
```

Without a `type_projector` configured, returning a numpy array (or any
other non-plain type) fails the same way any unprojectable value does:
`error.type == "serialization"`.

## Lower-level building blocks

This library's guarantees don't stop at `execute()`. Two layers underneath
it are public on purpose:

**`Sandbox.run_process(argv, cwd=...)`** — the argv twin of `execute()`.
Same rlimits, UID drop, process-group timeout/kill, and subreaper
reaping, but no JSON protocol: it runs an existing program (a script you
already materialized on disk, a CLI, whatever) and gives you back exit
status plus bounded/hashed stdout+stderr. Use this when your code doesn't
speak the return-value protocol and you already have your own way of
collecting output files, e.g. a fixed entrypoint script run per call:

```python
result = await sandbox.run_process(
    [sys.executable, "entrypoint.py"], cwd=workspace, limits=Limits(wall_seconds=60),
)
# {"status": "ok" | "timeout" | "busy" | "cancelled" | "internal",
#  "returncode": int | None, "timed_out": bool, "stdout": str, "stderr": str}
```

`argv` accepts any process: a script, a shell command, a compiled binary.

**`pyplaypen_sandbox.privilege`** — the two primitives everything else is
built from, with no dependency on `Sandbox`, asyncio, or anything else in
this package:

```python
from pyplaypen_sandbox.privilege import apply_resource_limits, drop_root_privileges

def preexec():
    apply_resource_limits({"cpu_seconds": 5, "memory_bytes": 2**30,
                            "process_count": 16, "file_bytes": 2**26})
    drop_root_privileges(uid=65534)

subprocess.Popen(argv, preexec_fn=preexec)  # works with plain subprocess.Popen too
```

If you already run your own `subprocess.Popen(preexec_fn=...)` supervision
and just want real rlimits and a real UID drop (the thing that makes
`RLIMIT_NPROC` bind on Linux, where root is exempt from it), that's all
this is. No need to adopt the rest of the library.

## Compared to restricted-interpreter sandboxes (e.g. Monty)

Pydantic's [Monty](https://github.com/pydantic/monty) solves the same
problem differently. It runs code in-process, in a restricted Python
subset: sandboxed code can only call what you inject as external
functions. There's no `import os`, no filesystem or network access, no
arbitrary pip packages, because the interpreter doesn't support any of
that. Its resource limits (allocation count, duration, memory) are
counters inside its own runtime, not kernel rlimits. There's no process
boundary.

These are two different strategies. Prefer pyplaypen-sandbox when code
needs real CPython (numpy, pandas, any pip package, subprocesses of its
own) or when you want kernel-enforced limits: a whole process tree killed
on timeout, real `RLIMIT_NPROC`/`RLIMIT_AS`, a real UID drop, instead of
an interpreter's internal bookkeeping, which can't see what a C extension
or an injected function does with memory or subprocesses on its own.

Prefer Monty when call volume or latency matters (no subprocess per
call), when the workload is a small fixed set of host capabilities rather
than open-ended generated code, or when the threat model is closer to
untrusted input than your own automation and there's no strong
per-tenant container isolation underneath it. A restricted language is a
stronger standalone safety claim than full CPython isolated by a
container that remains the real boundary, which is what this library is.

## Platform notes

`RLIMIT_AS` and `RLIMIT_NPROC` are Linux-only (not enforced on macOS/BSD —
the wall-clock timeout and process-group teardown still apply everywhere).
Root-UID drop is a no-op if the parent isn't running as root.

