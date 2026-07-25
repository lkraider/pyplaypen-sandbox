"""Protocol and language-semantics tests: these are the ones that try to
break the value-projection and error-classification contract, not just
exercise the happy path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyplaypen_sandbox import Context, Sandbox, run
from pyplaypen_sandbox.supervisor import _parse_frame


@pytest.fixture
def sandbox():
    return Sandbox(self_check=False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("40 + 2", 42),
        ('{"answer": 42}', {"answer": 42}),
        ("[1, 2, 3]", [1, 2, 3]),
        ("x = 6\nx * 7", 42),
        ("x = 1", None),
        ("import statistics\nstatistics.mean([2, 4, 6])", 4),
    ],
)
async def test_ordinary_python_semantics(tmp_path, sandbox, code, expected):
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "ok"
    assert result["return_value"] == expected


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        ("def broken(:", "syntax"),
        ('raise ValueError("no")', "runtime"),
        ('raise SystemExit("stop")', "runtime"),
        ("object()", "serialization"),
        ("x = []\nx.append(x)\nx", "serialization"),
    ],
)
async def test_stable_error_classification(tmp_path, sandbox, code, error_type):
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "error"
    assert result["error"]["type"] == error_type
    assert result["artifacts"] == []


async def test_stdout_cannot_corrupt_result_and_stderr_is_failure_only(tmp_path, sandbox):
    result = await run(
        'import sys\nprint(\'{"protocol_version":999}\')\nprint("warning", file=sys.stderr)\n7',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    assert result["status"] == "ok"
    assert result["return_value"] == 7
    assert "protocol_version" in result["stdout"]
    assert "details" not in (result["error"] or {})


async def test_each_call_has_fresh_state_and_parent_is_unchanged(tmp_path, sandbox, monkeypatch):
    monkeypatch.delenv("SANDBOX_CHILD_SENTINEL", raising=False)
    first = await run(
        'import os\nos.environ["SANDBOX_CHILD_SENTINEL"] = "set"\nglobal_value = 9\nglobal_value',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    second = await run('"global_value" in globals()', artifact_dir=str(tmp_path), sandbox=sandbox)
    assert first["return_value"] == 9
    assert second["return_value"] is False
    assert "SANDBOX_CHILD_SENTINEL" not in os.environ


def test_protocol_parser_rejects_missing_malformed_oversized_and_multiple_values():
    with pytest.raises(ValueError, match="without a result"):
        _parse_frame(b"", False)
    with pytest.raises(ValueError, match="malformed"):
        _parse_frame(b"not-json", False)
    with pytest.raises(ValueError, match="exceeds"):
        _parse_frame(b"{}", True)
    with pytest.raises(ValueError, match="multiple"):
        _parse_frame(b'{"protocol_version":1,"status":"ok"}{}', False)
    with pytest.raises(ValueError, match="version"):
        _parse_frame(b'{"protocol_version":2,"status":"ok"}', False)


async def test_return_value_path_is_rewritten_relative_to_artifact_root(tmp_path, sandbox):
    result = await run(
        'open("out.txt", "w").write("hi")\n"out.txt"',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    assert result["status"] == "ok"
    assert result["return_value"] == result["artifacts"][0]["path"]
    assert (Path(tmp_path) / result["return_value"]).read_text() == "hi"


async def test_pid_returned_is_the_child_not_the_caller(tmp_path, sandbox):
    result = await run("import os\nos.getpid()", artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "ok"
    assert result["return_value"] != os.getpid()
