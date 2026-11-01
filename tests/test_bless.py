"""The release gate itself: the parts of `bless` that are pure decisions.

`bless` is a script, not a module, so it is loaded by path.  Only the stages
that decide something without touching the network, git history or the
filesystem are exercised here -- the identity it derives, the duplicate-check
arithmetic, the branch plan, the prompt, the artifact pick.  What is *not* here
is deliberate: an upload cannot be rehearsed in a test, and the point of the
gate is that the decision it makes is inspectable rather than automatic.

The value of these tests is the refusals.  Everything else in `bless` fails
loudly; a refusal that silently stopped refusing would not.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

import luks_tray

ROOT = Path(__file__).resolve().parent.parent


def load_bless():
    """Load `./bless` by path, the way `bless` loads the inspector."""
    loader = importlib.machinery.SourceFileLoader('bless_under_test', str(ROOT / 'bless'))
    spec = importlib.util.spec_from_loader('bless_under_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BLESS = load_bless()


# --- identity --------------------------------------------------------------


def test_identity_is_read_from_pyproject_not_written_into_the_script():
    assert BLESS.NAME == 'luks-tray'
    assert BLESS.MODULE == 'luks_tray'
    assert BLESS.PROJECT['console'] == 'luks-tray'
    assert BLESS.PROJECT['inspector'] == 'tests/inspect_dist.py'
    assert BLESS.PROJECT['smoke_args'] == []


def test_identity_normalizes_the_project_page_name():
    # PEP 503: the spelling pypi.org uses in URLs, whatever the name does.
    assert BLESS.NORMALIZED == 'luks-tray'
    assert BLESS.PYPI['page'].endswith('/luks-tray/')


def test_the_version_bless_reads_is_the_package_version():
    assert BLESS.read_version() == luks_tray.__version__


def test_read_version_refuses_a_package_without_the_line(tmp_path):
    package = tmp_path / 'luks_tray'
    package.mkdir()
    (package / '__init__.py').write_text('"""no version here"""\n')
    with pytest.raises(BLESS.BlessError, match='no quoted `__version__'):
        BLESS.read_version(root=tmp_path)


def test_version_tuples_for_tool_floors():
    assert BLESS._version_tuple('7.0.1') == (7, 0, 1)
    assert BLESS._version_tuple('26') == (26, 0, 0)
    assert BLESS._version_tuple('nonsense') == (0, 0, 0)


# --- the duplicate check ---------------------------------------------------


def test_published_versions_are_sorted_numerically():
    document = {'releases': {'2.0.10': [], '2.0.9': [], '1.5': []}}
    assert BLESS.published_versions(document) == ['1.5', '2.0.9', '2.0.10']


def test_the_rehearsal_version_starts_at_zero():
    assert BLESS.next_test_version('3.0.0', {'releases': {}}) == '3.0.0.99.0'


def test_the_rehearsal_version_continues_past_what_is_there():
    document = {'releases': {'3.0.0.99.0': [], '3.0.0.99.1': []}}
    assert BLESS.next_test_version('3.0.0', document) == '3.0.0.99.2'


def test_the_rehearsal_version_ignores_other_bases():
    # Rehearsing an older release must not push the next one's counter forward.
    document = {'releases': {'2.0.0.99.7': [], '3.0.0.99.0': []}}
    assert BLESS.next_test_version('3.0.0', document) == '3.0.0.99.1'


def test_check_index_refuses_a_published_version():
    document = {'releases': {'3.0.0': []}}
    with pytest.raises(BLESS.BlessError, match='already on PyPI'):
        BLESS.check_index('3.0.0', document)


def test_check_index_names_what_is_published():
    document = {'releases': {'2.0.4': [], '2.0.5': []}}
    with pytest.raises(BLESS.BlessError) as refusal:
        BLESS.check_index('2.0.5', document)
    assert '2.0.4, 2.0.5' in str(refusal.value)


def test_check_index_accepts_an_unpublished_version():
    document = {'releases': {'2.0.5': []}}
    assert BLESS.check_index('3.0.0', document) == ['2.0.5']


# --- the branch plan -------------------------------------------------------


def is_ancestor(*pairs):
    """A stand-in for `git merge-base --is-ancestor` over an explicit table.

    A commit is always its own ancestor, which is the answer git gives.
    """
    table = set(pairs)
    return lambda older, newer: older == newer or (older, newer) in table


TIPS = {'main': 'aaa', 'dev': 'bbb', 'origin/main': 'aaa', 'origin/dev': 'bbb'}


def test_branch_plan_refuses_a_detached_head():
    plan = BLESS.branch_plan(None, TIPS, is_ancestor())
    assert 'detached HEAD' in plan['refusal']


def test_branch_plan_refuses_any_branch_but_main_and_dev():
    plan = BLESS.branch_plan('feature', TIPS, is_ancestor())
    assert 'publishes only from main or dev' in plan['refusal']


def test_branch_plan_refuses_a_missing_remote_branch():
    tips = dict(TIPS, **{'origin/dev': None})
    plan = BLESS.branch_plan('main', tips, is_ancestor(('aaa', 'bbb')))
    assert plan['refusal'] == 'origin/dev does not exist; push it before blessing'


def test_branch_plan_fast_forwards_main_onto_dev():
    plan = BLESS.branch_plan('dev', TIPS, is_ancestor(('aaa', 'bbb'),
                                                     ('aaa', 'bbb')))
    assert plan == {'align': 'main'}


def test_branch_plan_fast_forwards_dev_onto_main():
    plan = BLESS.branch_plan('main', TIPS, is_ancestor(('bbb', 'aaa'),
                                                       ('bbb', 'aaa')))
    assert plan == {'align': 'dev'}


def test_branch_plan_needs_no_alignment_when_they_match():
    tips = dict(TIPS, dev='aaa', **{'origin/dev': 'aaa'})
    assert BLESS.branch_plan('main', tips, is_ancestor()) == {'align': None}


def test_branch_plan_refuses_diverged_branches():
    plan = BLESS.branch_plan('dev', TIPS, is_ancestor())
    assert 'have diverged' in plan['refusal']


def test_branch_plan_refuses_a_push_that_is_not_a_fast_forward():
    # main can be fast-forwarded, but origin/main holds a commit the aligned
    # branch does not contain, so the push would be rejected *after* the upload
    # -- exactly when it is too late to notice.
    tips = {'main': 'aaa', 'dev': 'bbb',
            'origin/main': 'zzz', 'origin/dev': 'bbb'}
    plan = BLESS.branch_plan('dev', tips, is_ancestor(('aaa', 'bbb')))
    assert 'fast-forward on origin' in plan['refusal']


# --- the prompt ------------------------------------------------------------


def answer_with(monkeypatch, *replies):
    replies = list(replies)
    monkeypatch.setattr('builtins.input', lambda _prompt='': replies.pop(0))
    return BLESS.ask_mode()


def test_the_prompt_accepts_the_two_answers(monkeypatch):
    assert answer_with(monkeypatch, 'test') == 'test'
    assert answer_with(monkeypatch, 'publish') == 'publish'


def test_the_prompt_stops_on_enter(monkeypatch):
    assert answer_with(monkeypatch, '') == ''


def test_the_prompt_stops_on_quit(monkeypatch):
    assert answer_with(monkeypatch, 'quit') == ''


def test_the_prompt_asks_again_rather_than_guessing(monkeypatch, capsys):
    # `t` is one keystroke away from both answers, so nothing is inferred.
    assert answer_with(monkeypatch, 't', 'pub', 'test') == 'test'
    assert 'is not test, publish or quit' in capsys.readouterr().out


def test_the_prompt_treats_ctrl_c_as_stop(monkeypatch):
    def interrupt(_prompt=''):
        raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', interrupt)
    assert BLESS.ask_mode() == ''


# --- artifacts -------------------------------------------------------------


def test_artifacts_requires_exactly_one_of_each(tmp_path, monkeypatch):
    # `_artifacts()` names the directory relative to the tree in its refusal, so
    # the tree is redirected here rather than artifacts being written into the
    # repository.
    monkeypatch.setattr(BLESS, 'ROOT', tmp_path)
    (tmp_path / 'a.whl').write_text('x')
    (tmp_path / 'a.tar.gz').write_text('x')
    wheel, sdist = BLESS._artifacts(tmp_path)
    assert wheel.name == 'a.whl'
    assert sdist.name == 'a.tar.gz'


def test_artifacts_refuses_a_second_wheel(tmp_path, monkeypatch):
    monkeypatch.setattr(BLESS, 'ROOT', tmp_path)
    (tmp_path / 'a.whl').write_text('x')
    (tmp_path / 'b.whl').write_text('x')
    (tmp_path / 'a.tar.gz').write_text('x')
    with pytest.raises(BLESS.BlessError, match='expected exactly one wheel'):
        BLESS._artifacts(tmp_path)


def test_artifacts_refuses_an_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(BLESS, 'ROOT', tmp_path)
    with pytest.raises(BLESS.BlessError, match='expected exactly one wheel'):
        BLESS._artifacts(tmp_path)


# --- credentials -----------------------------------------------------------


def test_pypirc_credentials_reads_the_matching_section(tmp_path):
    pypirc = tmp_path / '.pypirc'
    pypirc.write_text(
        '[distutils]\nindex-servers = pypi\n\n'
        '[pypi]\nrepository = https://upload.pypi.org/legacy/\n'
        'username = __token__\npassword = pypi-secret\n')
    assert BLESS._pypirc_credentials(BLESS.PYPI, pypirc) == ('__token__', 'pypi-secret')


def test_pypirc_credentials_ignores_another_index(tmp_path):
    # A TestPyPI section must not answer for PyPI: the tokens are not the same.
    pypirc = tmp_path / '.pypirc'
    pypirc.write_text('[testpypi]\nrepository = https://test.pypi.org/legacy/\n'
                      'username = __token__\npassword = pypi-test\n')
    assert BLESS._pypirc_credentials(BLESS.PYPI, pypirc) is None


def test_pypirc_credentials_tolerates_a_missing_file(tmp_path):
    assert BLESS._pypirc_credentials(BLESS.PYPI, tmp_path / 'none') is None


# --- index lag -------------------------------------------------------------


def test_index_lag_says_the_release_is_not_listed():
    lag = BLESS._index_lag('3.0.0', Path('x.whl'), {'releases': {'2.0.5': []}})
    assert 'this release is not' in lag
    assert '2.0.5' in lag


def test_index_lag_says_the_info_block_is_behind():
    lag = BLESS._index_lag(
        '3.0.0', Path('x.whl'),
        {'releases': {'3.0.0': []}, 'info': {'version': '2.0.5'}})
    assert "still describes '2.0.5'" in lag


def test_index_lag_is_empty_once_the_index_agrees(monkeypatch):
    # `_metadata_mismatches()` reads the artifact and the inspector, so it is
    # replaced here; the branch under test is the sequencing, not the metadata.
    monkeypatch.setattr(BLESS, '_metadata_mismatches', lambda *_a: [])
    document = {'releases': {'3.0.0': []}, 'info': {'version': '3.0.0'}}
    assert BLESS._index_lag('3.0.0', Path('x.whl'), document) == ''


# --- the command line ------------------------------------------------------


def test_main_takes_no_arguments(capsys):
    # A mode passed as an argument would be a second, silent path to an upload,
    # so it is refused before anything else runs.
    assert BLESS.main(['publish']) == 1
    assert 'takes no arguments' in capsys.readouterr().err


def test_the_smoke_run_has_no_extra_argv():
    # `--check-deps` exits 1 when a system program is missing, which would fail
    # the gate for a packaging reason on a bare CI runner.
    assert BLESS.PROJECT['smoke_args'] == []
    assert BLESS.PROJECT['smoke_expect'] is None


def test_console_script_is_what_the_smoke_stage_runs():
    # bless runs the first [project.scripts] entry with `--version` and expects
    # the built version back; tests/test_metadata.py checks the other half.
    assert BLESS.PROJECT['console'] == 'luks-tray'
    assert isinstance(BLESS.TOOL_FLOORS, dict)
    assert BLESS.TOOL_FLOORS['twine'] >= (7, 0, 0)
    assert BLESS.TOOL_FLOORS['packaging'] >= (26, 0, 0)


def test_copy_excludes_keep_the_gate_output_out_of_a_rehearsal_copy():
    assert 'build' in BLESS.COPY_EXCLUDES
    assert 'dist' in BLESS.COPY_EXCLUDES
    assert '.venv' in BLESS.COPY_EXCLUDES


def test_the_rehearsal_tag_sorts_clear_of_every_real_release():
    # `X.Y.Z.99.N` is above every real `X.Y.Z`, so a rehearsal can never be
    # mistaken for, or block, the release it rehearses.
    assert BLESS.TEST_TAG == 99
    assert BLESS.next_test_version('3.0.0', {'releases': {}}) == '3.0.0.99.0'
    assert BLESS._publication_key('3.0.0.99.0') > BLESS._publication_key('3.0.0')
