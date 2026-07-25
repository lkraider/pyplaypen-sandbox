"""Optional type_projector implementations. Nothing here is imported by
core, and neither numpy nor pandas is a dependency of this package — this
module exists to be the worked example of the hook, and to be usable
as-is if numpy/pandas is exactly what you need projected.

Point Sandbox(type_projector=...) at one of these, or write your own with
the same shape: a function taking one unsupported value, returning
something _project() can serialize (plain data, or another unsupported
value — it recurses).
"""

from __future__ import annotations

from typing import Any


def project_numpy_pandas(value: Any) -> Any:
    module = type(value).__module__.split(".", 1)[0]
    if module == "numpy":
        import numpy as np
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    if module == "pandas":
        import pandas as pd
        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, pd.DataFrame):
            return {
                "columns": [str(item) for item in value.columns.tolist()],
                "rows": value.values.tolist(),
                "row_count": int(len(value)),
            }
        if isinstance(value, pd.Series):
            return {
                "name": None if value.name is None else str(value.name),
                "index": value.index.tolist(),
                "values": value.tolist(),
            }
    raise ValueError(f"no numpy/pandas projection for {type(value).__name__}")
