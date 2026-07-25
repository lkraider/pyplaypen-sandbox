"""globals_provider: the one extension point, exercised end to end."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyplaypen_sandbox import Context, Sandbox

FIXTURES_PYTHONPATH = str(Path(__file__).parent)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": FIXTURES_PYTHONPATH}


async def test_globals_provider_extends_the_exec_namespace(tmp_path):
    sandbox = Sandbox(self_check=False, globals_provider="fixtures.example_provider:build_globals")
    context = Context(artifact_root=tmp_path, env=_env())
    result = await sandbox.execute('greet("world")', context)
    assert result["status"] == "ok"
    assert result["return_value"].startswith("hello world from")


async def test_context_extra_is_opaque_and_reaches_the_provider(tmp_path):
    sandbox = Sandbox(self_check=False, globals_provider="fixtures.example_provider:build_globals")
    context = Context(artifact_root=tmp_path, env=_env(), extra={"tenant": "acme"})
    result = await sandbox.execute("echo_extra()", context)
    assert result["status"] == "ok"
    assert result["return_value"] == {"tenant": "acme"}


async def test_missing_provider_module_is_a_typed_extension_error(tmp_path):
    sandbox = Sandbox(self_check=False, globals_provider="nope.nope:build")
    result = await sandbox.execute("1", Context(artifact_root=tmp_path))
    assert result["status"] == "error"
    assert result["error"]["type"] == "extension"


async def test_non_dict_provider_return_is_a_typed_extension_error(tmp_path):
    sandbox = Sandbox(self_check=False, globals_provider="fixtures.example_provider:not_a_dict_provider")
    context = Context(artifact_root=tmp_path, env=_env())
    result = await sandbox.execute("1", context)
    assert result["status"] == "error"
    assert result["error"]["type"] == "extension"


def test_self_check_rejects_a_bad_provider_before_first_call():
    with pytest.raises(RuntimeError, match="globals_provider"):
        Sandbox(self_check=True, startup_timeout=10.0, globals_provider="nope.nope:build")


def test_self_check_accepts_a_good_provider():
    Sandbox(
        self_check=True, startup_timeout=10.0, child_env=_env(),
        globals_provider="fixtures.example_provider:build_globals",
    )
