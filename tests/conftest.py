import os

import pytest

from pyplaypen_sandbox import Sandbox


@pytest.fixture(autouse=True)
def _lenient_enforcement_gate(request, monkeypatch):
    # Most tests exercise sandbox behavior, not deployment validity, and must
    # construct a Sandbox on any host — including a bare non-root Linux box
    # where the enforcement gate would otherwise refuse (process_count
    # unsupported). Neutralize the gate by default; tests that assert the gate
    # itself opt back in with @pytest.mark.real_enforcement.
    if request.node.get_closest_marker("real_enforcement"):
        return
    monkeypatch.setattr(Sandbox, "_gate_enforcement", lambda self, warn_only: None)


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
