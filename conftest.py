"""Shared test setup: no test writes to the real home, and Qt runs headless.

Two pieces of state have to be neutralised before anything else runs:

* `IniTool` derives `~/.config/luks-tray` from `os.path.expanduser("~")`, and
  `LuksTray` derives mount folders from the same place.  The autouse fixture
  points `HOME` at a per-test temporary directory, so a test that constructs a
  config, saves a history file or creates a mount point cannot touch the
  developer's own configuration -- and two tests cannot see each other's files.
* `Utils._log` and `Utils._prt_seen` are module state by design (that is what
  makes `prt()` work from anywhere).  They are reset per test so a test that
  redirects the log cannot change what the next one prints.

Qt is set to the `offscreen` platform at import time, before any test can build
a `QApplication`: the tray and the dialogs need a real Qt, and a CI runner has
no display.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Must happen before PyQt6 creates a QApplication anywhere in the test session.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Give every test its own `$HOME`, and keep the tree importable."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    # Only read by IniTool when the process is root; a stray value from the
    # developer's shell would point the config at somebody else's home.
    monkeypatch.delenv('SUDO_USER', raising=False)
    # The tests run from the repository root too (an installed console script
    # would otherwise shadow the tree under test).
    monkeypatch.syspath_prepend(str(ROOT))
    return home


@pytest.fixture
def vault_root():
    """A dedicated vault area, outside `$HOME` *and* outside the home tree.

    `validate_auto_mount_root()` refuses a path inside any home directory --
    including the parent of this test's `$HOME` -- so the fixture makes its own
    temporary root rather than using `tmp_path`.
    """
    path = tempfile.mkdtemp(prefix='luks-vault-')
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_utils_state(monkeypatch):
    """`prt()`'s destination, one-shot keys and failure tally are process state."""
    from luks_tray import Utils

    monkeypatch.setattr(
        Utils, '_log',
        SimpleNamespace(path='', opened_path='', max_kb=0, keep_stdout=False))
    Utils._prt_seen.clear()
    # The menu says "Open Log [n errors]" from this count, so a test that logged
    # a failure must not make the next test's menu look like it had one.
    Utils.clear_session_problems()
    yield
    Utils._prt_seen.clear()


@pytest.fixture
def quiet_prt(monkeypatch):
    """Silence `prt()` by disabling the log, without touching stdout.

    `Utils._log.max_kb = 0` is the documented switch for "just print", and it
    keeps `prt()` away from `os.dup2()` -- which would replace the file
    descriptor pytest captures on.
    """
    from luks_tray import Utils

    monkeypatch.setattr(
        Utils, '_log',
        SimpleNamespace(path='', opened_path='', max_kb=0, keep_stdout=False))
    return Utils


@pytest.fixture(scope='session')
def qapp():
    """One `QApplication` for the whole session, on the offscreen platform."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def pytest_report_header(config):
    """Say which Qt platform the session is using, so a CI log is unambiguous."""
    return (f'QT_QPA_PLATFORM={os.environ.get("QT_QPA_PLATFORM")} '
            f'python={sys.version.split()[0]}')
