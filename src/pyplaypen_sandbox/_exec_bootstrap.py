"""Child bootstrap for Sandbox.run_process: apply limits and drop privileges,
then exec straight into the caller's argv. No JSON protocol, no result-fd —
rlimits and uid persist across exec(), so the target program just inherits
them and speaks stdout/stderr/exit-code like it normally would.
"""

from __future__ import annotations

import argparse
import os
import sys

from .privilege import apply_resource_limits, drop_root_privileges


def main() -> int:
    argv = sys.argv[1:]
    try:
        sep = argv.index("--")
    except ValueError:
        print("expected '--' followed by the target argv", file=sys.stderr)
        return 2
    target = argv[sep + 1:]
    if not target:
        print("empty target argv after '--'", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--process-count", type=int, required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--open-files", type=int, required=True)
    args = parser.parse_args(argv[:sep])

    apply_resource_limits(vars(args))
    drop_root_privileges(args.uid)
    os.execvp(target[0], target)  # never returns on success


if __name__ == "__main__":
    raise SystemExit(main())
