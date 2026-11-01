"""One version location, one entry point, and a `--version` that agrees.

`./bless` reads `luks_tray/__init__.py` for the release number, tags that
number, and then reads it *back* out of the wheel it built by running the
console script with `--version`.  A second copy of the version in
`pyproject.toml` is how the published metadata drifted from the code before, so
it is asserted absent rather than merely not added.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import luks_tray

ROOT = Path(__file__).resolve().parent.parent
VERSION_LINE = re.compile(r'^__version__ = [\'"]([^\'"]+)[\'"]', re.MULTILINE)


def manifest():
    with open(ROOT / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)


def test_the_version_lives_in_the_package_and_nowhere_else():
    project = manifest()['project']
    assert 'version' not in project, 'the version must not be duplicated in pyproject.toml'
    assert project['dynamic'] == ['version']

    text = (ROOT / 'luks_tray/__init__.py').read_text(encoding='utf-8')
    found = VERSION_LINE.findall(text)
    assert len(found) == 1, f'expected one __version__ line, found {found}'
    assert found[0] == luks_tray.__version__


def test_the_version_is_release_shaped():
    assert re.fullmatch(r'\d+\.\d+\.\d+', luks_tray.__version__), luks_tray.__version__


def test_the_console_script_is_the_entry_point():
    assert manifest()['project']['scripts'] == {'luks-tray': 'luks_tray.Main:main'}


def test_the_package_ships_no_second_metadata_copy():
    # An egg-info or a checked-in .dist-info would let an installed copy win.
    strays = [path.name for path in ROOT.glob('*.egg-info')]
    strays += [path.name for path in ROOT.glob('luks_tray*.dist-info')]
    assert strays == []


def test_version_flag_prints_the_package_version():
    env = dict(os.environ)
    # HOME is the test's temporary directory; nothing writes a config for
    # `--version`, which exits while argparse is parsing.
    result = subprocess.run(
        [sys.executable, '-m', 'luks_tray.Main', '--version'],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    assert luks_tray.__version__ in result.stdout
