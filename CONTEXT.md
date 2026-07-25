# pyplaypen-sandbox

A one-shot subprocess sandbox: runs a string of Python per call, inside a
container, with process-group isolation, POSIX rlimits, and a non-root UID —
defense in depth on top of the container, not a replacement for it.

## Language

**Sandbox**:
The parent-side object a caller constructs once and reuses across calls. Owns the concurrency semaphore and the startup self-check.
_Avoid_: Backend, executor, runner (Runner is the child, a different thing)

**Call**:
One invocation of `Sandbox.execute` — one child process, one workspace, one result.
_Avoid_: Request, execution, job

**Context**:
The per-call identity and placement: `request_id`, `artifact_root`, `env`, `extra`. Not the "trust boundary" sense of context used in the README's sandbox disclaimer — that's just prose, not this type.
_Avoid_: Config (Limits is config; Context is identity/placement)

**Limits**:
The per-call resource policy: wall/cpu/memory/process-count budgets, plus stdout/stderr/return-value/artifact byte caps. Shared verbatim between the parent (which enforces wall-clock and post-run artifact limits) and the child (which enforces rlimits and the return-value cap).
_Avoid_: Policy, config, quota

**Workspace**:
The per-call directory `<artifact_root>/sandbox-runs/<request_id>/` that sandboxed code runs in (cwd) and writes outputs into. Deleted unless the call succeeded and left files behind.
_Avoid_: Sandbox dir, working directory, scratch space

**Artifact**:
A file present in the Workspace after a call ends, reported as `{path, name, bytes, mime_type}` with `path` relative to `artifact_root`. Symlinks are never artifacts — scanning raises instead of skipping them, because a symlink means something in the workspace points somewhere it shouldn't.
_Avoid_: Output, file, result file

**Runner**:
The child bootstrap module (`_runner.py`), launched fresh per call as `python -m pyplaypen_sandbox._runner`. Applies rlimits and drops root before importing or running anything call-supplied.
_Avoid_: Child, worker (used informally for "the runner process" but Runner is the noun for the module/protocol)

**Protocol frame**:
The single JSON object the Runner writes to its dedicated result fd: `{protocol_version, status, return_value, error, artifacts}`. Distinct from the final result dict `Sandbox.execute` returns to the caller (which has `stdout` instead of `protocol_version`, and re-scans artifacts authoritatively).
_Avoid_: Response, payload (payload is the *request* the parent sends; frame is the child's reply)

**globals_provider**:
An import-path string (`"module:function"`) naming a caller-owned factory that runs inside the Runner, after the privilege drop, to add names to the exec namespace. The one extension point — this library assumes nothing about what a provider does or imports.
_Avoid_: Plugin, hook, extension (used loosely in prose; globals_provider is the actual field/parameter name)

**extra**:
Opaque, JSON-serializable, call-scoped data on `Context`, handed to the globals_provider and read by nothing else in this library.
_Avoid_: Metadata, params

**Enforcement map**:
A per-limit declaration of how honestly it's enforced on the current platform — `hard`, `best_effort_per_process`, `hard_post_run`, `unsupported`, etc. Computed once at import time (platform doesn't change at runtime) and recorded in every audit log entry.
_Avoid_: Guarantees (guarantees is the informal word; enforcement map is the concrete recorded artifact)

**Trust boundary**:
The container around the whole process. This library is explicitly *not* the trust boundary — it's the layer of defense in depth inside it. A hostile-code sandbox is a different, unclaimed guarantee.
_Avoid_: Isolation boundary, security boundary
