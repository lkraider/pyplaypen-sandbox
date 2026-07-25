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
        [sys.executable, "-c", code], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=0.4, cpu_seconds=1),
    )
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert elapsed < 2.0
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


async def test_missing_program_is_reported_not_raised(tmp_path, sandbox):
    result = await sandbox.run_process(["/no/such/program-xyz"], cwd=tmp_path)
    assert result["status"] in {"ok", "internal"}
    if result["status"] == "ok":
        assert result["returncode"] != 0


async def test_concurrency_is_shared_with_execute(tmp_path):
    sandbox = Sandbox(self_check=False, max_concurrency=1)
    slow = asyncio.create_task(sandbox.run_process(
        [sys.executable, "-c", "import time; time.sleep(0.4)"], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=2.0),
    ))
    await asyncio.sleep(0.05)
    busy = await sandbox.run_process(
        [sys.executable, "-c", "1"], cwd=tmp_path,
        limits=replace(DEFAULT_LIMITS, wall_seconds=0.4, cpu_seconds=1),
    )
    assert busy["status"] == "busy"
    assert (await slow)["status"] == "ok"


@pytest.mark.linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux resource semantics required")
async def test_linux_root_drops_to_dedicated_uid(tmp_path, sandbox):
    result = await sandbox.run_process([sys.executable, "-c", "import os; print(os.getuid())"], cwd=tmp_path)
    expected = sandbox.child_uid if os.geteuid() == 0 else os.getuid()
    assert result["stdout"].strip() == str(expected)
