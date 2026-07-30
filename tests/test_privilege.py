"""privilege.py: usable standalone, without Sandbox at all."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from pyplaypen_sandbox.privilege import drop_root_privileges


def test_drop_root_privileges_is_a_noop_when_not_root():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root; no-op path not exercised")
    before = os.environ.get("HOME")
    drop_root_privileges(65534)
    assert os.environ.get("HOME") == before


def test_apply_resource_limits_enforces_cpu_seconds_standalone():
    # No Sandbox, no _runner.py — just the primitive, applied via the exact
    # mechanism a caller embedding this in their own preexec_fn would use.
    # Run in a subprocess: applying real rlimits in-process would confine
    # the test runner itself.
    code = (
        "from pyplaypen_sandbox.privilege import apply_resource_limits\n"
        "apply_resource_limits({'cpu_seconds': 1, 'file_bytes': 10_000_000, "
        "'memory_bytes': 1024**3, 'process_count': 16, 'open_files': 256})\n"
        "while True: pass\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    # A plain nonzero exit is a weak check on its own — a KeyError from a
    # missing dict field would also satisfy it. Pin the actual cause: killed
    # by the CPU-time signal, not crashed for some unrelated reason.
    assert result.returncode == -signal.SIGXCPU, result.stderr


def test_apply_resource_limits_permits_generous_cpu_seconds_standalone():
    # Counterfactual for the enforcement test above: identical primitive,
    # same shape of call, but with headroom the code stays inside — proves
    # the kill above is caused by the 1-second budget, not by merely calling
    # apply_resource_limits or by some fixed penalty independent of the value.
    code = (
        "from pyplaypen_sandbox.privilege import apply_resource_limits\n"
        "apply_resource_limits({'cpu_seconds': 5, 'file_bytes': 10_000_000, "
        "'memory_bytes': 1024**3, 'process_count': 16, 'open_files': 256})\n"
        "sum(range(10**6))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_apply_resource_limits_enforces_open_files_standalone():
    code = (
        "from pyplaypen_sandbox.privilege import apply_resource_limits\n"
        "apply_resource_limits({'cpu_seconds': 5, 'file_bytes': 10_000_000, "
        "'memory_bytes': 1024**3, 'process_count': 16, 'open_files': 8})\n"
        "import tempfile\n"
        "opened = [tempfile.TemporaryFile() for _ in range(64)]\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    assert result.returncode == 1
    assert b"Too many open files" in result.stderr, result.stderr


def test_apply_resource_limits_permits_generous_open_files_standalone():
    # Counterfactual: identical 64-file workload, only open_files raised from
    # 8 to 256 — proves 64 files isn't intrinsically over some other system
    # or interpreter ceiling and that 8 specifically is what trips the above.
    code = (
        "from pyplaypen_sandbox.privilege import apply_resource_limits\n"
        "apply_resource_limits({'cpu_seconds': 5, 'file_bytes': 10_000_000, "
        "'memory_bytes': 1024**3, 'process_count': 16, 'open_files': 256})\n"
        "import tempfile\n"
        "opened = [tempfile.TemporaryFile() for _ in range(64)]\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    assert result.returncode == 0, result.stderr
