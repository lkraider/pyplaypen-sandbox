"""Coverage for ported-but-previously-untested paths: return-value size cap,
symlink rejection, workspace cleanup on failure/empty success, concurrent
workspace isolation, and numpy/pandas projection."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.util

import pytest

from pyplaypen_sandbox import DEFAULT_LIMITS, Sandbox, run


@pytest.fixture
def sandbox():
    return Sandbox(self_check=False)


async def test_return_limit_is_typed_and_valid(tmp_path, sandbox):
    limits = replace(DEFAULT_LIMITS, return_value_bytes=100)
    result = await run('"x" * 1000', artifact_dir=str(tmp_path), sandbox=sandbox, limits=limits)
    assert result["status"] == "error"
    assert result["error"]["type"] == "return_limit"


async def test_symlink_artifact_is_rejected_and_cleaned(tmp_path, sandbox):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    result = await run(
        f'import os\nos.symlink({str(outside)!r}, "link.txt")\n"link.txt"',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    assert result["status"] == "error"
    assert result["error"]["type"] == "artifact_limit"
    assert outside.read_text() == "outside"


async def test_failure_cleans_workspace_and_empty_success_removes_it(tmp_path, sandbox):
    failed = await run(
        'open("partial.txt", "w").write("bad")\nraise ValueError("stop")',
        artifact_dir=str(tmp_path), sandbox=sandbox,
    )
    empty = await run("40 + 2", artifact_dir=str(tmp_path), sandbox=sandbox)
    assert failed["status"] == "error"
    assert failed["artifacts"] == []
    assert empty["status"] == "ok"
    runs = tmp_path / "sandbox-runs"
    assert not runs.exists() or not any(runs.iterdir())


async def test_workspaces_isolate_concurrent_identical_filenames(tmp_path):
    sandbox = Sandbox(self_check=False, max_concurrency=2)
    code = 'open("same.txt", "w").write("value")\n"same.txt"'
    first, second = await asyncio.gather(*[
        run(code, artifact_dir=str(tmp_path), sandbox=sandbox) for _ in range(2)
    ])
    paths = {first["return_value"], second["return_value"]}
    assert first["status"] == second["status"] == "ok"
    assert len(paths) == 2
    assert all((tmp_path / path).read_text() == "value" for path in paths)


@pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None or importlib.util.find_spec("pandas") is None,
    reason="numpy/pandas not installed; projection is exercised only when importable",
)
async def test_numpy_and_pandas_projection(tmp_path, sandbox):
    code = '''
import numpy as np
import pandas as pd
{
 "scalar": np.int64(7),
 "array": np.array([[1, 2], [3, 4]]),
 "series": pd.Series([1, pd.NA], name="x"),
 "frame": pd.DataFrame({"a": [1, 2], "b": [3, 4]}),
 "timestamp": pd.Timestamp("2026-07-14T12:00:00Z"),
 "na": pd.NA,
}
'''
    result = await run(code, artifact_dir=str(tmp_path), sandbox=sandbox)
    assert result["status"] == "ok", result
    assert result["return_value"]["scalar"] == 7
    assert result["return_value"]["array"] == [[1, 2], [3, 4]]
    assert result["return_value"]["series"]["values"] == [1, None]
    assert result["return_value"]["frame"]["row_count"] == 2
    assert result["return_value"]["na"] is None
