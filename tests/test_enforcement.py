"""Enforcement map honesty and the construction-time gate."""

from __future__ import annotations

import logging

import pytest

from pyplaypen_sandbox import Sandbox
from pyplaypen_sandbox import supervisor


def _fully_enforced() -> dict[str, str]:
    return {k: "hard" for k in supervisor._enforcement_map()}


# --- _cgroup_pids_capped: reads the leaf pids controller, never raises -------

def test_cgroup_pids_capped_true_for_finite_value(tmp_path, monkeypatch):
    capped = tmp_path / "pids.max"
    capped.write_text("512\n")
    monkeypatch.setattr(supervisor, "Path", lambda p: capped)
    assert supervisor._cgroup_pids_capped() is True


def test_cgroup_pids_capped_false_for_literal_max(tmp_path, monkeypatch):
    uncapped = tmp_path / "pids.max"
    uncapped.write_text("max\n")
    monkeypatch.setattr(supervisor, "Path", lambda p: uncapped)
    assert supervisor._cgroup_pids_capped() is False


def test_cgroup_pids_capped_false_and_silent_when_absent(tmp_path, monkeypatch):
    # No cgroup file anywhere (e.g. macOS): read raises, caught → False.
    missing = tmp_path / "does-not-exist" / "pids.max"
    monkeypatch.setattr(supervisor, "Path", lambda p: missing)
    assert supervisor._cgroup_pids_capped() is False


def test_cgroup_pids_capped_false_for_garbage(tmp_path, monkeypatch):
    junk = tmp_path / "pids.max"
    junk.write_text("not-a-number\n")
    monkeypatch.setattr(supervisor, "Path", lambda p: junk)
    assert supervisor._cgroup_pids_capped() is False


# --- _enforcement_map: process_count reflects the real mechanism -------------

def _force(monkeypatch, *, platform="linux", is_root=False, capped=False):
    monkeypatch.setattr(supervisor.sys, "platform", platform)
    monkeypatch.setattr(supervisor.os, "geteuid", lambda: 0 if is_root else 1000, raising=False)
    monkeypatch.setattr(supervisor, "_cgroup_pids_capped", lambda: capped)


def test_process_count_hard_per_user_under_root(monkeypatch):
    _force(monkeypatch, is_root=True)
    assert supervisor._enforcement_map()["process_count"] == "hard_per_user"


def test_process_count_container_when_cgroup_capped(monkeypatch):
    _force(monkeypatch, is_root=False, capped=True)
    m = supervisor._enforcement_map()
    assert m["process_count"] == "container"
    # Counterfactual: a cgroup cap is the operator's value, not the caller's —
    # must not be reported as "hard" (that would overclaim Limits.process_count).
    assert m["process_count"] != "hard"


def test_process_count_unsupported_when_non_root_and_uncapped(monkeypatch):
    _force(monkeypatch, is_root=False, capped=False)
    assert supervisor._enforcement_map()["process_count"] == "unsupported"


def test_enforcement_map_on_macos_is_honest(monkeypatch):
    _force(monkeypatch, platform="darwin", is_root=False)
    m = supervisor._enforcement_map()
    assert m["memory_bytes"] == "unsupported"
    assert m["process_count"] == "unsupported"
    assert m["cpu_seconds"] == "hard_per_process"
    assert m["open_files"] == "hard"
    assert m["file_bytes"] == "hard_per_file"


# --- the gate: fail loud by default, warn_only to proceed, Linux-only --------

@pytest.mark.real_enforcement
def test_gate_raises_when_a_limit_is_unsupported(monkeypatch):
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    unsupported = {**_fully_enforced(), "process_count": "unsupported"}
    monkeypatch.setattr(supervisor, "_enforcement_map", lambda: unsupported)
    with pytest.raises(RuntimeError, match="process_count"):
        Sandbox(self_check=False)


@pytest.mark.real_enforcement
def test_gate_warn_only_proceeds_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    unsupported = {**_fully_enforced(), "process_count": "unsupported"}
    monkeypatch.setattr(supervisor, "_enforcement_map", lambda: unsupported)
    with caplog.at_level(logging.WARNING, logger=supervisor.LOGGER.name):
        sandbox = Sandbox(self_check=False, warn_only=True)
    assert sandbox.enforcement["process_count"] == "unsupported"
    assert any("process_count" in r.message for r in caplog.records)


@pytest.mark.real_enforcement
def test_gate_does_not_raise_when_fully_enforced(monkeypatch):
    enforced = _fully_enforced()
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "_enforcement_map", lambda: enforced)
    Sandbox(self_check=False)  # no raise


@pytest.mark.real_enforcement
def test_gate_no_ops_off_linux_even_when_unsupported(monkeypatch):
    # Counterfactual: same unsupported limit, non-Linux platform → gate is a
    # no-op (a dev host, not a deployment target).
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    unsupported = {**_fully_enforced(), "process_count": "unsupported"}
    monkeypatch.setattr(supervisor, "_enforcement_map", lambda: unsupported)
    Sandbox(self_check=False)  # no raise


def test_enforcement_property_is_a_copy():
    sandbox = Sandbox(self_check=False, warn_only=True)
    snapshot = sandbox.enforcement
    snapshot["process_count"] = "tampered"
    assert sandbox.enforcement["process_count"] != "tampered"
