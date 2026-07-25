import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _traversable_basetemp(tmp_path_factory):
    # Under root each call drops to a dedicated non-root UID that must traverse
    # down to its workspace. pytest builds tmp_path under 0700 root-owned
    # ancestors; grant them the execute bit (traversal only, not read) so the
    # dropped UID can reach it. No-op unless root — mirrors execute()'s own chmod.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        base = tmp_path_factory.getbasetemp()
        for directory in (base, base.parent):
            os.chmod(directory, os.stat(directory).st_mode | 0o111)
    yield
