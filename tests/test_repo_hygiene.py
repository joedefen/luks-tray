"""Repository-level invariants the release gate relies on.

`./bless` refuses a dirty tree, runs `ruff check .` with no flags, and never
uploads a distribution it has not inspected.  Each of those is only true while
something else in the tree keeps it true, so the things that could quietly
break it are asserted here: an edited `bless`, a tool cache that is not
ignored, a missing inspector, an unpinned lint selection.
"""

import hashlib
import os
import stat
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Copied verbatim from ../pwr-tray/bless (identical in vappman and my-snaps).
# Project identity is read from pyproject.toml, so the file is never edited per
# project -- adopting a newer upstream means a new copy and a new hash here.
BLESS_SHA256 = '4f756337840b0619fbf6f077b00d4bb3fb9aee95ff18bbeb6a0521d39d52131e'

# What the gate writes and then expects not to see in `git status`.
IGNORED = ('.ruff_cache/', '.pytest_cache/', '.coverage', '*.whl', '*.tar.gz',
           'build/', 'dist/')


def manifest():
    with open(ROOT / 'pyproject.toml', 'rb') as handle:
        return tomllib.load(handle)


def test_bless_is_present_and_executable():
    bless = ROOT / 'bless'
    assert bless.is_file()
    mode = bless.stat().st_mode
    assert mode & stat.S_IXUSR, 'bless must be executable to be run as ./bless'


def test_bless_is_the_verbatim_copy():
    digest = hashlib.sha256((ROOT / 'bless').read_bytes()).hexdigest()
    assert digest == BLESS_SHA256, (
        'bless has been edited; adopt a new upstream copy and update '
        'BLESS_SHA256 (and the provenance note in tests/README-DEV.md §5)')


def test_the_shared_inspector_exists_where_bless_expects_it():
    inspector = manifest().get('tool', {}).get('bless', {}).get(
        'inspect', 'tests/inspect_dist.py')
    assert inspector == 'tests/inspect_dist.py'
    assert (ROOT / inspector).is_file()


def test_the_gate_output_is_gitignored():
    lines = {line.strip() for line in
             (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()}
    missing = [pattern for pattern in IGNORED if pattern not in lines]
    assert missing == [], f'not ignored, so bless would refuse a dirty tree: {missing}'


def test_the_obsolete_deploy_script_is_gone():
    # ./bless replaced it; a second, unchecked install path is what it was.
    assert not (ROOT / 'deploy').exists()


def test_the_sdist_carries_its_own_gate():
    include = manifest()['tool']['flit']['sdist']['include']
    assert 'tests/' in include
    assert 'conftest.py' in include, (
        'an sdist without conftest.py cannot run its own test gate')
    assert (ROOT / 'conftest.py').is_file()


def test_the_lint_selection_is_pinned():
    # bless runs `ruff check .` with no flags; an unpinned selection would let a
    # ruff upgrade change what the release gate enforces.
    lint = manifest()['tool']['ruff']['lint']
    assert lint['select'] == ['E4', 'E7', 'E9', 'F']


def test_no_test_writes_into_the_repository():
    # The suite is run from the repository root; anything it creates there would
    # show up in `git status` and stop the next bless run.
    known = {'.git', '.gitignore', '.github', '.pytest_cache', '.ruff_cache',
             '.coverage', '.venv', 'LICENSE', 'README.md', 'bless', 'conftest.py',
             'images', 'luks_tray', 'pyproject.toml', 'runner',
             'tests', '__pycache__', 'build', 'dist'}
    surprise = sorted(name for name in os.listdir(ROOT) if name not in known)
    assert surprise == [], f'unexpected files at the repository root: {surprise}'


# --- the CI workflow -------------------------------------------------------


def workflow():
    path = ROOT / '.github' / 'workflows' / 'ci.yml'
    assert path.is_file(), 'the CI workflow is missing'
    return path.read_text(encoding='utf-8')


def test_ci_runs_the_same_gates_as_bless():
    text = workflow()
    # The release gate and CI must not be able to disagree about what a valid
    # tree or a valid artifact is, so both name the same commands.
    assert 'ruff check' in text
    assert 'compileall' in text
    assert 'pytest' in text
    assert '--cov=luks_tray' in text
    assert 'tests/inspect_dist.py' in text
    assert 'python -m build' in text


def test_ci_runs_the_supported_python_versions():
    text = workflow()
    for version in ('3.11', '3.12', '3.13'):
        assert f'"{version}"' in text


def test_ci_sets_the_offscreen_qt_platform():
    # A runner has no display; without this the Qt tests cannot start.
    assert 'QT_QPA_PLATFORM: offscreen' in workflow()


def test_ci_cannot_publish():
    # CI is a gate. The upload is irreversible and belongs to a deliberate
    # local action, so no credential may reach the workflow.
    text = workflow()
    for forbidden in ('secrets.', 'TWINE_', 'twine upload', 'password'):
        assert forbidden not in text, f'the CI workflow must not contain {forbidden!r}'


def test_ci_runs_on_the_release_branches():
    text = workflow()
    assert 'branches: [main, dev]' in text
    assert 'pull_request:' in text
    assert 'permissions:' in text and 'contents: read' in text
