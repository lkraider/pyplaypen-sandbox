# pyplaypen-sandbox

One-shot subprocess sandbox for running Python inside a container.

**Not a hostile-code sandbox.** The container around this process is the
accepted trust boundary. This library is defense in depth *inside* that
boundary — it does not claim to hold against code that specifically tries to
escape it. If you need that, use gVisor/Firecracker/a VM, not this.

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

This repo is meant to be adapted, not just vendored in as-is — see
"Extending it" and "Lower-level building blocks" below for the actual
decision points (`execute()` vs. `run_process()`, whether a
`globals_provider`/`type_projector` is warranted, what `Limits` fit the
workload). An agent wiring this in should read those sections and write
the integration a project actually needs, not copy the examples verbatim.

Copy-paste to a coding agent, for a project integration:

```
Set up a process sandbox in this project using
https://github.com/lkraider/pyplaypen-sandbox. Read the README fully
first, then decide the integration this project needs — don't just
paste its examples.
```

## Install

```
pip install pyplaypen-sandbox
```

No required dependencies, and none optional either — see `type_projector`
below if you need numpy/pandas (or anything else) in return values.

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

This library makes no assumption about what sandboxed code needs — no
numpy/pandas/httpx/duckdb dependency, no built-in helpers. If you want
sandboxed code to call out to something (an HTTP client, a query engine,
whatever), give `Sandbox` an import path to a factory function. It runs
*inside* the child, after the privilege drop, so an extension is bound by
the same rlimits as user code and never has to be a dependency of this
package — its own imports live in your module, not ours.

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

`ctx` is `{"request_id", "workspace", "artifact_root", "extra"}` — `extra`
is whatever JSON-serializable config you passed on `Context`; this library
never reads it. A bad provider (missing module, wrong return type) fails as
`error.type == "extension"`, distinct from user-code errors, and — if
`self_check=True` (the default) — is caught at `Sandbox()` construction
time rather than on first call.

The same shape extends what a return value can *be*. `_project()` (the
function that turns your final expression into JSON) knows only plain
Python types — anything else needs a `type_projector`, one function taking
an unsupported value and returning something projectable (plain data, or
another unsupported value — it recurses):

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

numpy/pandas support is not built in — it's the shipped example of this
exact mechanism, in `pyplaypen_sandbox.projectors`, usable as-is:

```python
sandbox = Sandbox(type_projector="pyplaypen_sandbox.projectors:project_numpy_pandas")
result = await sandbox.execute("import numpy as np\nnp.array([1, 2])", context)
# {"status": "ok", "return_value": [1, 2], ...}
```

Without a `type_projector` configured, returning a numpy array (or any
other non-plain type) is `error.type == "serialization"` — same failure
mode as any other unprojectable value, since as far as this library is
concerned, that's exactly what it is.

## Lower-level building blocks

`execute()` is one thing built on top of this library's real guarantees —
not the only thing you can build on them. Two layers underneath it are
public on purpose:

**`Sandbox.run_process(argv, cwd=...)`** — the argv twin of `execute()`.
Same rlimits, UID drop, process-group timeout/kill, and subreaper
reaping, but no JSON protocol: it runs an existing program (a script you
already materialized on disk, a CLI, whatever) and gives you back exit
status plus bounded/hashed stdout+stderr. Use this when your code doesn't
speak the return-value protocol and you already have your own way of
collecting output files — e.g. running a fixed entrypoint script per call:

```python
result = await sandbox.run_process(
    [sys.executable, "entrypoint.py"], cwd=workspace, limits=Limits(wall_seconds=60),
)
# {"status": "ok" | "timeout" | "busy" | "cancelled" | "internal",
#  "returncode": int | None, "timed_out": bool, "stdout": str, "stderr": str}
```

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

If you're already running your own `subprocess.Popen(preexec_fn=...)`
supervision and just want real rlimits and a real UID drop (the thing that
actually makes `RLIMIT_NPROC` bind on Linux, where root is exempt from it),
this is the whole ask — no need to adopt the rest of the library.

## Compared to restricted-interpreter sandboxes (e.g. Monty)

Pydantic's [Monty](https://github.com/pydantic/monty) takes a different
approach worth naming explicitly: it runs code **in-process**, as a
restricted Python-subset interpreter — sandboxed code can only call what
you explicitly inject as external functions, so there's no `import os`,
no ambient filesystem/network access, no arbitrary pip packages, because
the interpreter itself doesn't support that surface. Its resource limits
(allocation count, duration, memory) are counters inside its own runtime,
not kernel `rlimit`s — there's no process boundary at all.

That's a different sandboxing *strategy*, not a lighter version of this
one. Prefer pyplaypen-sandbox when the code needs real CPython (numpy,
pandas, any pip package, legitimately spawning subprocesses) or you want
actual kernel-enforced limits (wall-clock kill of a whole process tree,
real `RLIMIT_NPROC`/`RLIMIT_AS`, real UID drop) rather than an
interpreter's internal bookkeeping, which can't see what a C extension or
an injected function does with its own memory or subprocesses. Prefer a
restricted interpreter like Monty when call volume/latency matters (no
subprocess-per-call cost), the workload is bounded to a small fixed set
of host capabilities rather than open-ended generated code, or the threat
model is closer to untrusted input than your own automation and you lack
strong per-tenant container isolation underneath — a restricted language
is a stronger standalone claim than "full CPython, isolated a bit more,
but the container is still the real boundary," which is explicitly not
what this library claims to be.

## Platform notes

`RLIMIT_AS` and `RLIMIT_NPROC` are Linux-only (not enforced on macOS/BSD —
the wall-clock timeout and process-group teardown still apply everywhere).
Root-UID drop is a no-op if the parent isn't running as root.

