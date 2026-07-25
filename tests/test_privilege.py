"""privilege.py: usable standalone, without Sandbox at all."""

from __future__ import annotations

import os
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
        "'memory_bytes': 1024**3, 'process_count': 16})\n"
        "while True: pass\n"
    )
    result = subprocess.run([sys.executable, "-c", code], timeout=5, capture_output=True)
    assert result.returncode != 0
