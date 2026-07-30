"""run_process: the argv-execution twin of execute(), no JSON protocol."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import sys
import time

import pytest

from pyplaypen_sandbox import DEFAULT_LIMITS, Sandbox


@pytest.fixture
def sandbox():
    return Sandbox(self_check=False)


async def test_ok_run_returns_stdout_and_returncode(tmp_path, sandbox):
    result = await sandbox.run_process([sys.executable, "-c", "print(40 + 2)"], cwd=tmp_path)
    assert result == {
        "status": "ok", "returncode": 0, "timed_out": False, "stdout": "42\n", "stderr": "",
    }


async def test_nonzero_exit_is_ok_status_with_returncode(tmp_path, sandbox):
    result = await sandbox.run_process([sys.executable, "-c", "import sys; sys.exit(3)"], cwd=tmp_path)
    assert result["status"] == "ok"
    assert result["returncode"] == 3


async def test_stderr_is_captured_separately_from_stdout(tmp_path, sandbox):
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    result = await sandbox.run_process([sys.executable, "-c", code], cwd=tmp_path)
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"


async def test_timeout_kills_the_whole_process_group(tmp_path, sandbox):
    code = '''
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
print(p.pid, flush=True)
time.sleep(30)
'''
    started = time.monotonic()
    result = await sandbox.run_process(
        # wall_seconds needs real headroom for subprocess.Popen (a full
        # interpreter fork/exec) to complete on a loaded machine before the
        # deadline, or the child never reaches its own print() at all.
        [sys.executable, "-c", code], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=2.0, cpu_seconds=25),
    )
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert elapsed < 4.0
    pid = int(result["stdout"].strip())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("descendant ignoring SIGTERM was not reaped")


async def test_writes_files_into_cwd(tmp_path, sandbox):
    result = await sandbox.run_process(
        [sys.executable, "-c", 'open("out.txt", "w").write("hi")'], cwd=tmp_path,
    )
    assert result["status"] == "ok"
    assert (tmp_path / "out.txt").read_text() == "hi"


async def test_open_files_limit_is_enforced(tmp_path, sandbox):
    # Exercises _exec_bootstrap.py's own --open-files flag, the CLI-argv
    # path that's separate from execute()'s Limits-dict-in-JSON path.
    code = "import tempfile\n[tempfile.TemporaryFile() for _ in range(64)]\n"
    result = await sandbox.run_process(
        [sys.executable, "-c", code], cwd=tmp_path, limits=replace(DEFAULT_LIMITS, open_files=8),
    )
    assert result["status"] == "ok"
    assert result["returncode"] != 0
    assert "Too many open files" in result["stderr"]


async def test_open_files_headroom_permits_the_same_workload(tmp_path, sandbox):
    # Counterfactual: identical 64-file code, default open_files instead of
    # 8 — proves the failure above is the limit, not run_process itself or
    # the workload.
    code = "import tempfile\n[tempfile.TemporaryFile() for _ in range(64)]\n"
    result = await sandbox.run_process([sys.executable, "-c", code], cwd=tmp_path)
    assert result["status"] == "ok"
    assert result["returncode"] == 0


async def test_invalid_open_files_value_fails_fast_not_hangs(tmp_path, sandbox):
    # _exec_bootstrap.py's main() has no try/except of its own around
    # apply_resource_limits, unlike _runner.py's — a bad value must still
    # crash only the bootstrap subprocess, quickly, not hang the supervisor.
    started = time.monotonic()
    result = await sandbox.run_process(
        [sys.executable, "-c", "1 + 1"], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, open_files=2**64, wall_seconds=5.0),
    )
    assert time.monotonic() - started < 2.0
    assert result["status"] == "ok"
    assert result["returncode"] != 0
    assert "OverflowError" in result["stderr"]


async def test_valid_open_files_value_does_not_trip_the_overflow_path(tmp_path, sandbox):
    # Counterfactual: same shape, a value that's merely large rather than
    # out of C's rlim_t range — proves the crash above is about
    # unrepresentability, not about open_files being customized at all.
    result = await sandbox.run_process(
        [sys.executable, "-c", "1 + 1"], cwd=tmp_path, limits=replace(DEFAULT_LIMITS, open_files=4096),
    )
    assert result == {"status": "ok", "returncode": 0, "timed_out": False, "stdout": "", "stderr": ""}


async def test_missing_program_is_reported_not_raised(tmp_path, sandbox):
    result = await sandbox.run_process(["/no/such/program-xyz"], cwd=tmp_path)
    assert result["status"] in {"ok", "internal"}
    if result["status"] == "ok":
        assert result["returncode"] != 0


async def test_concurrency_is_shared_with_execute(tmp_path):
    sandbox = Sandbox(self_check=False, max_concurrency=1)
    # The holder must still own the only permit when the second call's acquire
    # deadline elapses, or the second call slips through and times out on its
    # own wall budget instead of reporting busy. Keep the hold well past the
    # busy call's wall so the outcome is unambiguous under a slow spawn path.
    slow = asyncio.create_task(sandbox.run_process(
        [sys.executable, "-c", "import time; time.sleep(1.0)"], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=3.0),
    ))
    await asyncio.sleep(0.1)
    busy = await sandbox.run_process(
        [sys.executable, "-c", "1"], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=0.3, cpu_seconds=1),
    )
    assert busy["status"] == "busy"
    assert (await slow)["status"] == "ok"


@pytest.mark.linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_root_drops_to_dedicated_uid(tmp_path, sandbox):
    result = await sandbox.run_process([sys.executable, "-c", "import os; print(os.getuid())"], cwd=tmp_path)
    expected = sandbox.child_uid if os.geteuid() == 0 else os.getuid()
    assert result["stdout"].strip() == str(expected)
