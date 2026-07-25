"""Process supervision, queueing, output, and Linux limit tests.

These are deliberately adversarial: floods of output, background
descendants that ignore SIGTERM, cancellation mid-spawn. They exist to
break the no-orphan / no-deadlock / no-leaked-permit guarantees, not to be
a friendly smoke test.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import time

import pytest

from pyplaypen_sandbox import Context, DEFAULT_LIMITS, Sandbox, run


def short_limits(**changes):
    values = {"wall_seconds": 0.4, "cpu_seconds": 1, **changes}
    return replace(DEFAULT_LIMITS, **values)


@pytest.fixture
def sandbox():
    return Sandbox(self_check=False)


async def test_sleep_times_out_with_bounded_slack(tmp_path, sandbox):
    started = time.monotonic()
    result = await run(
        "import time\ntime.sleep(10)", artifact_dir=str(tmp_path), sandbox=sandbox,
        limits=short_limits(),
    )
    elapsed = time.monotonic() - started
    assert result["status"] == "error"
    assert result["error"]["type"] == "timeout"
    assert elapsed < 2.0


async def test_cancellation_reaps_child_and_removes_workspace(tmp_path, sandbox, caplog):
    caplog.set_level("INFO")
    task = asyncio.create_task(run(
        "import time\ntime.sleep(10)", artifact_dir=str(tmp_path), sandbox=sandbox,
    ))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runs = tmp_path / "sandbox-runs"
    assert not runs.exists() or not any(runs.iterdir())
    records = [
        json.loads(record.message)
        for record in caplog.records
        if '"event":"sandbox_execution"' in record.message
    ]
    assert records[-1]["failure_type"] == "cancelled"
    assert records[-1]["spawned"] is True


async def test_cancellation_during_spawn_reaps_recovered_child(tmp_path, sandbox, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    child_pid: list[int] = []

    async def delayed_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        child_pid.append(proc.pid)
        spawned.set()
        await asyncio.sleep(0.1)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(run("1", artifact_dir=str(tmp_path), sandbox=sandbox))
    await spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child_pid and await _wait_pid_gone(child_pid[0])
    runs = tmp_path / "sandbox-runs"
    assert not runs.exists() or not any(runs.iterdir())


@pytest.mark.parametrize(
    "code",
    [
        'print("x" * 1_000_000)',
        'import sys\nprint("x" * 1_000_000, file=sys.stderr)\nraise ValueError("done")',
        'import sys\nprint("x" * 1_000_000)\nprint("y" * 1_000_000, file=sys.stderr)',
    ],
)
async def test_output_floods_do_not_deadlock(tmp_path, sandbox, code):
    result = await run(
        code, artifact_dir=str(tmp_path), sandbox=sandbox,
        limits=replace(DEFAULT_LIMITS, wall_seconds=5.0),
    )
    assert result["error"] is None or result["error"]["type"] == "runtime"
    assert len(result["stdout"].encode()) <= DEFAULT_LIMITS.stdout_bytes + 32
    if result["stdout"]:
        assert "truncated" in result["stdout"]


async def _wait_pid_gone(pid: int) -> bool:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_successful_call_terminates_background_descendant(tmp_path, sandbox):
    code = '''
import subprocess, sys
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
p.pid
'''
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "ok"
    assert await _wait_pid_gone(result["return_value"])


async def test_artifact_metadata_is_snapshotted_after_descendants_stop(tmp_path, sandbox):
    code = '''
import subprocess, sys, time
open("changing.txt", "w").write("start")
script = """import signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open('.ready', 'w').write('1')
for _ in range(200):
    with open('changing.txt', 'a') as stream: stream.write('x' * 10)
    time.sleep(0.005)
"""
subprocess.Popen([sys.executable, "-c", script])
while not __import__('os').path.exists('.ready'): time.sleep(0.005)
"changing.txt"
'''
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "ok"
    artifact = result["artifacts"][0]
    path = tmp_path / artifact["path"]
    assert artifact["bytes"] == path.stat().st_size
    assert artifact["bytes"] > len("start")


async def test_timeout_kills_descendant_ignoring_sigterm(tmp_path, sandbox):
    code = '''
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
print(p.pid, flush=True)
time.sleep(30)
'''
    # wall_seconds needs real headroom for subprocess.Popen (a full
    # interpreter fork/exec) to complete on a loaded machine before the
    # deadline, or the child never reaches its own print() at all.
    result = await run(
        code, artifact_dir=str(tmp_path), sandbox=sandbox,
        limits=short_limits(wall_seconds=2.0, cpu_seconds=25),
    )
    pid = int(result["stdout"].strip())
    assert result["error"]["type"] == "timeout"
    assert await _wait_pid_gone(pid)


async def test_concurrency_one_queues_and_busy_timeout_does_not_leak_permit(tmp_path):
    sandbox = Sandbox(self_check=False, max_concurrency=1)
    first = asyncio.create_task(run(
        "import time\ntime.sleep(0.6)\n1", artifact_dir=str(tmp_path), sandbox=sandbox,
        limits=replace(DEFAULT_LIMITS, wall_seconds=2.0),
    ))
    await asyncio.sleep(0.1)
    second = await run(
        "2", artifact_dir=str(tmp_path), sandbox=sandbox, limits=short_limits(wall_seconds=0.2),
    )
    assert second["error"]["type"] == "busy"
    assert (await first)["return_value"] == 1
    third = await run("3", artifact_dir=str(tmp_path), sandbox=sandbox)
    assert third["return_value"] == 3


async def test_queued_cancellation_does_not_leak_permit(tmp_path):
    sandbox = Sandbox(self_check=False, max_concurrency=1)
    first = asyncio.create_task(run(
        "import time\ntime.sleep(0.5)", artifact_dir=str(tmp_path), sandbox=sandbox,
    ))
    await asyncio.sleep(0.1)
    queued = asyncio.create_task(run("2", artifact_dir=str(tmp_path), sandbox=sandbox))
    await asyncio.sleep(0.1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await first
    assert (await run("3", artifact_dir=str(tmp_path), sandbox=sandbox))["return_value"] == 3


async def test_repeated_timeouts_leave_no_workspaces(tmp_path, sandbox):
    limits = short_limits(wall_seconds=0.15)
    for _ in range(10):
        result = await run(
            "import time\ntime.sleep(5)", artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits,
        )
        assert result["error"]["type"] == "timeout"
    runs = tmp_path / "sandbox-runs"
    assert not runs.exists() or not any(runs.iterdir())


async def test_artifact_aggregate_limit_cleans_workspace(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, file_bytes=1024, artifact_bytes=1500)
    result = await run(
        'open("a.bin", "wb").write(b"x" * 800)\n'
        'open("b.bin", "wb").write(b"x" * 800)\nNone',
        artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits,
    )
    assert result["error"]["type"] == "artifact_limit"
    assert not list((tmp_path / "sandbox-runs").glob("*")) if (tmp_path / "sandbox-runs").exists() else True


@pytest.mark.linux
@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_cpu_limit_is_honestly_classified(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, wall_seconds=5.0, cpu_seconds=1)
    result = await run("while True: pass", artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits)
    assert result["status"] == "error"
    assert result["error"]["type"] in {"cpu_limit", "crash"}


@pytest.mark.linux
@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_memory_limit_and_artifact_cleanup(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, memory_bytes=160 * 1024 * 1024)
    result = await run(
        'open("partial.txt", "w").write("x")\nx = bytearray(512 * 1024 * 1024)\nlen(x)',
        artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits,
    )
    assert result["status"] == "error"
    assert result["error"]["type"] in {"memory_limit", "crash"}
    assert not list((tmp_path / "sandbox-runs").glob("*")) if (tmp_path / "sandbox-runs").exists() else True


@pytest.mark.linux
@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_root_runner_drops_to_dedicated_uid(tmp_path, sandbox):
    result = await run("import os\nos.getuid()", artifact_dir=str(tmp_path), sandbox=sandbox)
    expected = sandbox.child_uid if os.geteuid() == 0 else os.getuid()
    assert result["return_value"] == expected


@pytest.mark.linux
@pytest.mark.skipif(
    not os.sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="RLIMIT_NPROC is per real UID system-wide; only meaningful once "
           "root actually drops to its own dedicated UID",
)
async def test_linux_process_limit_is_enforced(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, wall_seconds=5.0, process_count=4)
    code = '''
import subprocess, sys
children = []
for _ in range(20):
    children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]))
len(children)
'''
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits)
    assert result["error"]["type"] == "process_limit"


@pytest.mark.linux
@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_per_file_limit_is_enforced(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, file_bytes=1024)
    result = await run(
        'open("large.bin", "wb").write(b"x" * 4096)\nNone',
        artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits,
    )
    assert result["error"]["type"] == "artifact_limit"


@pytest.mark.linux
@pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_artifact_count_limit_cleans_workspace(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, artifact_count=1)
    result = await run(
        'open("a.txt", "w").write("a")\nopen("b.txt", "w").write("b")\nNone',
        artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits,
    )
    assert result["error"]["type"] == "artifact_limit"
    assert result["artifacts"] == []
