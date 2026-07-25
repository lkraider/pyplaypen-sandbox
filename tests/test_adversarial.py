"""Adversarial round: inputs chosen to break the value-projection, path-
translation, and artifact-boundary contracts, plus robustness guards that
must hold under hostile termination.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest

from pyplaypen_sandbox import Context, DEFAULT_LIMITS, Sandbox, run

FIXTURES_PYTHONPATH = str(Path(__file__).parent)


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": FIXTURES_PYTHONPATH}


@pytest.fixture
def sandbox():
    return Sandbox(self_check=False)


# --- value projection: distinct keys must not silently collapse ---

@pytest.mark.parametrize(
    "code",
    [
        '{1: "a", "1": "b"}',
        '{1.5: "a", "1.5": "b"}',
        '{None: "a", "None": "b"}',
    ],
)
async def test_str_coerced_keys_do_not_silently_drop_data(tmp_path, sandbox, code):
    """A non-string key that str()-coerces onto an existing string key must
    not silently overwrite it. JSON objects can't hold both, so the only
    correct outcomes are a typed serialization error or a lossless encoding —
    never a dict that quietly returns just one of the two values."""
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    if result["status"] == "ok":
        assert len(result["return_value"]) == 2, (
            f"two distinct keys collapsed into {result['return_value']!r}"
        )
    else:
        assert result["error"]["type"] == "serialization"


# --- path translation must not clobber ordinary data strings ---

async def test_data_string_matching_a_filename_is_not_rewritten(tmp_path, sandbox):
    """The return-value path rewrite is meant to relocate paths the program
    reports, not to mangle arbitrary strings. A value the program set to the
    literal "a.txt" (as data, not a path it returned to point at the file)
    must survive unchanged."""
    result = await run(
        'open("a.txt", "w").write("x")\n{"label": "a.txt", "n": 1}',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    assert result["status"] == "ok"
    assert result["return_value"]["label"] == "a.txt"
