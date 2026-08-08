"""The pyplaypen CLI: run_process behind an argv interface."""

from __future__ import annotations

import logging
import os
import sys
import time

import pytest

from pyplaypen_sandbox import supervisor
from pyplaypen_sandbox.cli import main

PY = sys.executable


@pytest.fixture(autouse=True)
def _clean_invocation(tmp_path, monkeypatch):
    # The CLI runs in cwd and, under root, chowns it back afterwards.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYPLAYPEN_QUIET", "1")
    for name in ("PYPLAYPEN_LIMITS", "PYPLAYPEN_AUDIT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def unsupported_process_count(monkeypatch):
    real = supervisor._enforcement_map
    monkeypatch.setattr(
        supervisor, "_enforcement_map", lambda: {**real(), "process_count": "unsupported"}
    )


def run(*argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        main(["run", *argv])
    return exc.value.code


def test_child_exit_code_is_the_cli_exit_code():
    assert run("--", PY, "-c", "import sys; sys.exit(7)") == 7


def test_streams_stay_separate(capsys):
    assert run("--", PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)") == 0
    captured = capsys.readouterr()
    assert captured.out == "out\n"
    assert captured.err == "err\n"


def test_missing_separator_is_a_usage_error(capsys):
    assert run(PY, "-c", "pass") == 2
    assert "pyplaypen:" in capsys.readouterr().err


def test_empty_target_after_separator_is_a_usage_error():
    assert run("--") == 2


def test_separator_inside_the_target_is_preserved(capsys):
    assert run("--", PY, "-c", "import sys; print(sys.argv[1:])", "--", "-k", "foo") == 0
    assert capsys.readouterr().out == "['--', '-k', 'foo']\n"


def test_missing_program_exits_nonzero_without_crashing_the_cli():
    assert run("--", "/no/such/program-xyz") != 0


def test_unknown_limit_is_rejected_with_the_valid_names(capsys):
    assert run("--limit", "bogus=1", "--", PY, "-c", "pass") == 2
    assert "wall_seconds" in capsys.readouterr().err


@pytest.mark.parametrize("pair", ["wall_seconds=abc", "cpu_seconds=1.5", "open_files"])
def test_uncoercible_limit_values_are_rejected(pair):
    assert run("--limit", pair, "--", PY, "-c", "pass") == 2


@pytest.mark.parametrize("pair", ["wall_seconds=1.5", "stdout_bytes=1_048_576"])
def test_well_typed_limit_values_are_accepted(pair):
    # Counterfactual to the rejections above: the float field takes a float,
    # and int() accepts underscores.
    assert run("--limit", pair, "--", PY, "-c", "print(1)") == 0


def test_env_limits_apply_and_flags_override_them(monkeypatch):
    monkeypatch.setenv("PYPLAYPEN_LIMITS", "wall_seconds=0.4")
    assert run("--", PY, "-c", "import time; time.sleep(3)") == 124
    assert run("--limit", "wall_seconds=5", "--", PY, "-c", "import time; time.sleep(0.5)") == 0


def test_requesting_an_unenforceable_limit_is_refused(unsupported_process_count, capsys):
    assert run("--limit", "process_count=4", "--", PY, "-c", "print(1)") == 2
    err = capsys.readouterr().err
    assert "process_count" in err and "pyplaypen enforcement" in err


def test_the_same_limit_left_at_its_default_is_not_refused(unsupported_process_count, capsys):
    assert run("--", PY, "-c", "print(1)") == 0
    assert capsys.readouterr().out == "1\n"


def test_unenforceable_limit_via_env_is_refused(unsupported_process_count, monkeypatch):
    monkeypatch.setenv("PYPLAYPEN_LIMITS", "process_count=4")
    assert run("--", PY, "-c", "print(1)") == 2


def test_notice_names_unsupported_dimensions(unsupported_process_count, monkeypatch, capsys):
    monkeypatch.delenv("PYPLAYPEN_QUIET")
    assert run("--", PY, "-c", "print(1)") == 0
    # memory_bytes is unsupported off Linux too, so match the line shape plus
    # the one dimension under test.
    err = capsys.readouterr().err
    assert err.startswith("pyplaypen: not enforced here:") and "process_count" in err


def test_quiet_silences_the_notice(unsupported_process_count, capsys):
    assert run("--", PY, "-c", "print(1)") == 0
    assert capsys.readouterr().err == ""


@pytest.mark.real_enforcement
def test_library_warn_only_warning_never_reaches_stderr(unsupported_process_count, capsys):
    # The gate runs for real here; its warning tells the reader to pass a
    # Python kwarg, which a CLI user cannot do.
    assert run("--", PY, "-c", "print(1)") == 0
    assert "warn_only" not in capsys.readouterr().err


def test_timeout_exits_124_naming_wall_seconds(capsys):
    started = time.monotonic()
    assert run("--limit", "wall_seconds=0.5", "--", PY, "-c", "import time; time.sleep(30)") == 124
    assert time.monotonic() - started < 4.0
    assert "wall_seconds=0.5 exceeded" in capsys.readouterr().err


@pytest.mark.linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGXCPU delivery")
def test_cpu_limit_is_named(capsys):
    code = run("--limit", "cpu_seconds=1", "--", PY, "-c", "x=0\nwhile 1: x+=1")
    assert code == 128 + 24  # SIGXCPU
    assert "cpu_seconds=1 exceeded" in capsys.readouterr().err


def test_output_past_stdout_bytes_carries_the_truncation_marker(capsys):
    assert run("--limit", "stdout_bytes=64", "--", PY, "-c", "print('x' * 5000)") == 0
    assert "...[truncated]" in capsys.readouterr().out


def test_background_descendant_does_not_outlive_the_call(capsys):
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    assert run("--limit", "wall_seconds=2", "--", PY, "-c", code) == 124
    pid = int(capsys.readouterr().out.strip())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail("descendant ignoring SIGTERM was not reaped")


@pytest.mark.parametrize("target", [[PY], [PY, "-"], ["cat", "-"]])
def test_stdin_targets_are_refused(target, capsys):
    assert run("--", *target) == 2
    assert "stdin is not available" in capsys.readouterr().err


def test_enforcement_prints_every_dimension(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["enforcement"])
    assert exc.value.code == 0
    printed = dict(line.split(": ", 1) for line in capsys.readouterr().out.splitlines())
    assert printed == supervisor._enforcement_map()


@pytest.mark.linux
@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0, reason="root chown path"
)
def test_cwd_ownership_survives_a_run_as_root(tmp_path):
    before = os.stat(tmp_path)
    assert run("--", PY, "-c", "open('out.txt', 'w').write('hi')") == 0
    after = os.stat(tmp_path)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_audit_record_is_emitted_on_demand(monkeypatch, caplog):
    # Otherwise unreachable from the CLI: _audit_process logs at INFO and
    # nothing configures logging.
    monkeypatch.setenv("PYPLAYPEN_AUDIT", "1")
    with caplog.at_level(logging.INFO, logger="pyplaypen_sandbox"):
        assert run("--", PY, "-c", "print(1)") == 0
    assert any('"event":"process_execution"' in record.message for record in caplog.records)
