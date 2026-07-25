"""type_projector: the extension point for custom return types, exercised
end to end (mirrors test_extension.py for globals_provider)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyplaypen_sandbox import Context, Sandbox

FIXTURES_PYTHONPATH = str(Path(__file__).parent)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": FIXTURES_PYTHONPATH}


async def test_custom_type_without_a_projector_is_a_typed_serialization_error(tmp_path):
    sandbox = Sandbox(self_check=False)
    context = Context(artifact_root=tmp_path, env=_env())
    code = "from fixtures.example_projector import Point\nPoint(1, 2)"
    result = await sandbox.execute(code, context)
    assert result["status"] == "error"
    assert result["error"]["type"] == "serialization"


async def test_type_projector_converts_a_custom_type(tmp_path):
    sandbox = Sandbox(self_check=False, type_projector="fixtures.example_projector:project")
    context = Context(artifact_root=tmp_path, env=_env())
    code = "from fixtures.example_projector import Point\nPoint(1, 2)"
    result = await sandbox.execute(code, context)
    assert result["status"] == "ok"
    assert result["return_value"] == {"x": 1, "y": 2}


async def test_type_projector_recurses_into_containers(tmp_path):
    sandbox = Sandbox(self_check=False, type_projector="fixtures.example_projector:project")
    context = Context(artifact_root=tmp_path, env=_env())
    code = "from fixtures.example_projector import Point\n[Point(1, 2), {'p': Point(3, 4)}]"
    result = await sandbox.execute(code, context)
    assert result["status"] == "ok"
    assert result["return_value"] == [{"x": 1, "y": 2}, {"p": {"x": 3, "y": 4}}]


async def test_projector_failure_is_a_typed_serialization_error_not_a_crash(tmp_path):
    sandbox = Sandbox(self_check=False, type_projector="fixtures.example_projector:broken_project")
    context = Context(artifact_root=tmp_path, env=_env())
    code = "from fixtures.example_projector import Point\nPoint(1, 2)"
    result = await sandbox.execute(code, context)
    assert result["status"] == "error"
    assert result["error"]["type"] == "serialization"


def test_self_check_rejects_a_bad_projector_before_first_call():
    with pytest.raises(RuntimeError, match="type_projector"):
        Sandbox(self_check=True, startup_timeout=10.0, type_projector="nope.nope:project")


def test_self_check_accepts_a_good_projector():
    Sandbox(
        self_check=True, startup_timeout=10.0, child_env=_env(),
        type_projector="fixtures.example_projector:project",
    )
