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
  process count, max file size, open file descriptors
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
snippet in an isolated subprocess, capped at 5 seconds of wall time and
256 MB of memory, and return the result: <code>
```

To bound a command without writing any integration, prefix it:
`pyplaypen run -- python script.py` (see "Command line").

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
`memory_limit`, `process_limit`, `open_files_limit`, `artifact_limit`,
`return_limit`, `busy`, `cancelled`, `crash`, `protocol`, `internal`,
`extension`.

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
status plus bounded stdout+stderr. Use this when your code doesn't
speak the return-value protocol and you already have your own way of
collecting output files, e.g. a fixed entrypoint script run per call:

```python
result = await sandbox.run_process(
    [sys.executable, "entrypoint.py"], cwd=workspace, limits=Limits(wall_seconds=60),
)
# or just as well: ["./entrypoint.sh"], ["/usr/bin/some-tool", "--flag"], ...
# {"status": "ok" | "timeout" | "busy" | "cancelled" | "internal",
#  "returncode": int | None, "timed_out": bool, "stdout": str, "stderr": str}
```

`argv` accepts any process: a script, a shell command, a compiled binary.

Pass `merge_output=True` to fold stderr into stdout at the OS level, so
the two interleave in one stream (in emission order) for an operator log
that needs stderr in the context of the stdout around it — interleaving
that has to be captured here and can't be reconstructed from the two
separate strings. `stderr` is then `""` and the merged stream is bounded
by `stdout_bytes`. Ordering is best-effort (only writes within `PIPE_BUF`
are atomic, and child buffering can still reorder).

**`pyplaypen_sandbox.privilege`** — the two primitives everything else is
built from, with no dependency on `Sandbox`, asyncio, or anything else in
this package:

```python
from pyplaypen_sandbox.privilege import apply_resource_limits, drop_root_privileges

def preexec():
    apply_resource_limits({"cpu_seconds": 5, "memory_bytes": 2**30,
                            "process_count": 16, "file_bytes": 2**26,
                            "open_files": 256})
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

## Container reference

The repo's `Dockerfile` is the intended shape: a Linux image that creates a
dedicated non-root UID and installs the package, with the parent free to stay
root so each call drops to that UID — which is what makes `RLIMIT_NPROC` bind.
Base your own image on it, or copy the pattern. If you instead run the container
as a fixed non-root user (no root parent, no per-call drop), pair it with a
container pids cap (`docker run --pids-limit=N`) so `process_count` is enforced
at the container layer — otherwise the enforcement gate will refuse to start
(see "Enforcement gate"). CI builds the `test` stage and runs the suite as root
and as the dedicated non-root UID with `--pids-limit`, plus a step asserting the
gate rejects an uncapped non-root container — so both deployment shapes and the
gate are exercised on real Linux, not just where they no-op.

### Command line

Installing the package puts a `pyplaypen` executable on PATH. Prefix any command
with it to run that command under enforced limits, with no code to write. Agent
harnesses spawn Python as a subprocess, so this is how a harness uses the
sandbox: shim `pyplaypen run --` in front of the interpreter it already calls.

```
pyplaypen run [--limit name=value]... -- <argv>...
pyplaypen enforcement
```

```bash
pyplaypen run -- python script.py
pyplaypen run --limit wall_seconds=300 --limit memory_bytes=2_147_483_648 -- pytest -q
PYPLAYPEN_LIMITS=wall_seconds=60 pyplaypen run -- ./some-tool --flag
```

`run` starts your command in its own process group, in the current directory,
with the current environment, capping CPU time, memory, open files and file size
per process. It kills the whole group, including anything the command spawned,
when `wall_seconds` runs out. Your command's stdout and stderr are printed when
it exits, truncated at `stdout_bytes`/`stderr_bytes`, and you get its exit code
back.

Any `Limits` field is a valid `--limit` name. Values are plain integers, and
`int()` accepts `_` separators. `PYPLAYPEN_LIMITS=name=value,name=value` sets
defaults for every call, so a shim configures it once and a per-call `--limit`
still wins. Negative or non-finite values are rejected: each one disables the
limit outright, which would let a `--limit` silently escape a shim's
`PYPLAYPEN_LIMITS` policy.

`pyplaypen enforcement` prints one line per limit saying what actually enforces
it on this machine (`hard`, `container`, `unsupported`, ...). Run it at setup
time to find out whether the guarantees you need are available here.

`run` refuses a limit it cannot keep. `--limit process_count=4` on a non-root
host with no pids cap exits 2 and tells you how to fix it; the same limit left at
its default runs normally and prints one `pyplaypen:` line naming what goes
unenforced here, which `PYPLAYPEN_QUIET=1` silences. The Python API refuses at
construction instead; see "Enforcement gate". `PYPLAYPEN_AUDIT=1` prints the
per-call audit record to stderr.

Exit codes: your command's own, or `128+signum` if a signal killed it; `124` on
timeout; `125` if the sandbox could not run it at all; `2` for a usage error or a
refused limit. Every line the CLI writes to stderr starts with `pyplaypen:`, so
it separates from your command's own output, and it names the limit behind a
signal death that would otherwise arrive as a bare `-24`.

What it does not do. **No stdin**: your command reads EOF immediately, so a bare
interpreter or a `-` argument is refused instead of silently running an empty
program (`python -c`, `python script.py` and `pytest` are unaffected). **No
streaming**: output arrives when the command exits. **No filesystem
confinement**: `cwd` is your real project directory.
**Text output only**: decoded with `errors="replace"`, so binary stdout is
mangled. **A `file_bytes` breach is silent**: the write is truncated at the cap
and your command is not told. **Under root, ownership restore covers `cwd`
only**: a file your command creates inside it keeps the dropped child uid.

## Enforcement gate

On Linux, `Sandbox()` **fails at construction** if any limit it accepts can't
actually be enforced on this deployment. A limit that is set but silently does
nothing gives no protection while looking like it does, so the gate refuses to
start rather than let that through — the same way `self_check=True` bails on a
broken runner.

The gate checks every limit, not one in particular. On Linux, `process_count`
is simply the only limit whose enforceability depends on the deployment; every
other limit is enforced either by the supervisor itself or by a per-process
rlimit that always applies, so none of them can come up unenforced. A non-root
container with no process cap can't enforce `process_count`, so that deployment
is rejected until you fix it — run the container so the parent is root (each
call drops to a dedicated UID, binding `RLIMIT_NPROC` to your value), set a
container-level pids cap (`docker run --pids-limit=N`), or pass `warn_only=True`
to acknowledge the gap and proceed anyway. The gate is Linux-only; on other
platforms (dev hosts) it no-ops.

`Sandbox.enforcement` exposes the same truth as a dict — what each limit
actually enforces here (`"hard"`, `"hard_per_user"`, `"container"`,
`"unsupported"`, ...). Gate your own deploy on it if you want a specific
guarantee:

```python
sandbox = Sandbox()
assert sandbox.enforcement["process_count"] != "unsupported"
```

## Platform notes

On macOS, `cpu_seconds`, `file_bytes`, and `open_files` are enforced natively,
and the wall-clock timeout and process-group teardown apply everywhere.
`memory_bytes` and `process_count` are reported `"unsupported"`: Darwin has no
honest primitive for either — `RLIMIT_AS` aliases the unenforced `RLIMIT_RSS`,
and per-UID `RLIMIT_NPROC` needs a root-drop that isn't the supported shape. For
full enforcement on a Mac, run the Linux image in a Linux VM (Apple `container`,
Colima, or Docker) — the recommended route. `Sandbox.enforcement` reports which
limits are real on the current host. Root-UID drop is a no-op if the parent
isn't running as root.

`process_count` binds to your exact `Limits` value only when the parent is root
and drops to a dedicated UID (`RLIMIT_NPROC` is per real UID, so it's only safe
to set once this process owns its UID). A non-root container instead relies on
a **container-level** pids cap (`docker run --pids-limit`, a Kubernetes pod pids
cgroup, systemd `TasksMax`) — real enforcement, but at the operator's number,
not your `Limits.process_count`. `Sandbox.enforcement["process_count"]` reports
which you have: `"hard_per_user"` (root, your value), `"container"` (cgroup cap,
operator's value), or `"unsupported"` (neither — the gate rejects this).

