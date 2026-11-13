"""The suite never uses the checkout's own installation (conftest.py).

In a checkout the installation root is the repository, whose .env holds the
developer's real API keys and broker settings. Loading it leaked those values
into os.environ and into Billy workers started by tests.
"""

import os
import subprocess
import sys

from conftest import ROOT, TEST_INSTALL_ROOT

from billy_runtime import paths


def test_installation_root_is_a_temporary_one():
    assert TEST_INSTALL_ROOT.resolve() == paths.INSTALL_ROOT
    assert ROOT.resolve() != paths.INSTALL_ROOT
    assert not paths.INSTALL_ROOT.is_relative_to(ROOT.resolve())
    assert ROOT.resolve() == paths.CODE_ROOT
    assert (paths.INSTALL_ROOT / 'persona.ini.example').is_file()


def test_workers_started_by_tests_resolve_their_own_root():
    # The variable is only set while paths is imported, so a worker launched
    # from a temporary installation still finds its own .env there.
    assert 'BILLY_INSTALL_ROOT' not in os.environ or os.environ[
        'BILLY_INSTALL_ROOT'
    ] != str(TEST_INSTALL_ROOT)
    child = subprocess.run(
        [
            sys.executable,
            '-c',
            'import os; print(os.environ.get("BILLY_INSTALL_ROOT", ""))',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() != str(TEST_INSTALL_ROOT)
