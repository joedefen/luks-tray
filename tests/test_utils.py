"""`Utils`: the logging destination, `prt()` and `prt_once()`.

The redirection itself is tested in a subprocess: `configure_log()` and
`prt()` call `os.dup2()`, which would replace the file descriptor pytest
captures on.  A child process is also the only honest way to produce the three
answers `check_stdout()` branches on -- stdout is a pipe, stdout is a terminal,
stdout is a regular file the caller opened -- because pytest's own capture turns
the inherited stdout into a regular file.
"""

import os
import subprocess
import sys
from pathlib import Path

from luks_tray import Utils

ROOT = Path(__file__).resolve().parent.parent


def _where_from_helper():
    """Call `where()` one frame down, so it reports *this* file."""
    return Utils.where()


def test_where_names_the_caller():
    assert _where_from_helper().startswith('[test_utils.py:')


def test_where_walks_to_its_own_frame_and_gives_up_out_of_range():
    # above=-2 lands on `where` itself; anything past the end of the stack is
    # reported as unavailable rather than raising IndexError.
    assert Utils.where(above=-2).startswith('[Utils.py:')
    assert Utils.where(above=-99) == '[n/a]'


def test_prt_stamps_the_line_and_prints_the_message(capsys, quiet_prt):
    Utils.prt('hello', 'world')
    out = capsys.readouterr().out
    assert 'hello world' in out
    assert '[test_utils.py:' in out


def test_prt_honours_sep_and_end(capsys, quiet_prt):
    Utils.prt('a', 'b', sep='|', end='\n')
    assert 'a|b' in capsys.readouterr().out


def test_prt_once_prints_one_message_per_key(capsys, quiet_prt):
    Utils.prt_once('same-key', 'first')
    Utils.prt_once('same-key', 'second')
    out = capsys.readouterr().out
    assert 'first' in out
    assert 'second' not in out


def test_prt_once_still_prints_a_different_key(capsys, quiet_prt):
    Utils.prt_once('key-a', 'one')
    Utils.prt_once('key-b', 'two')
    out = capsys.readouterr().out
    assert 'one' in out and 'two' in out


def test_prt_once_forgets_everything_when_the_set_fills(capsys, quiet_prt):
    Utils._prt_seen.update(f'k{index}' for index in range(Utils.PRT_ONCE_MAX))
    Utils.prt_once('overflow', 'after-clear')
    assert Utils._prt_seen == {'overflow'}
    assert 'after-clear' in capsys.readouterr().out


def test_configure_log_with_a_non_positive_cap_only_records_the_path(
        tmp_path, quiet_prt):
    path = tmp_path / 'debug.log'
    Utils.configure_log(str(path), max_kb=0)
    assert Utils._log.path == str(path)
    assert Utils._log.max_kb == 0
    # "Disabled" means no redirection, so nothing was opened.
    assert Utils._log.opened_path == ''
    assert not path.exists()


def test_configure_log_with_no_path_is_a_no_op(quiet_prt):
    Utils.configure_log('')
    assert Utils._log.path == ''
    assert Utils._log.opened_path == ''


def _run_child(body, *args, stdout=None):
    """Run `body` with this tree importable and stdin/stdout under our control.

    `stdout` defaults to a pipe: the child must not inherit pytest's captured
    stdout, which is a regular file and would put `configure_log()` on its
    "somebody else redirected this" branch.
    """
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    return subprocess.run(
        [sys.executable, '-c', body, *args],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE if stdout is None else stdout,
        stderr=subprocess.PIPE, text=True, timeout=120)


REDIRECTS = """\
import os, sys
from luks_tray import Utils
log = os.path.join(sys.argv[1], 'debug.log')
Utils.configure_log(log, max_kb=1)
Utils.prt('first message')
Utils.prt('x' * 2000)
Utils.prt('after rotation')
"""


def test_configure_log_redirects_a_pipe_onto_the_file_and_rotates_it(tmp_path):
    result = _run_child(REDIRECTS, str(tmp_path))
    assert result.returncode == 0, result.stderr
    # prt() went into the log rather than to the pipe it was handed.
    assert result.stdout == ''
    log = tmp_path / 'debug.log'
    assert log.is_file()
    assert 'after rotation' in log.read_text()
    # The size cap moved the older content aside instead of truncating it.
    assert 'first message' in (tmp_path / 'debug.log1').read_text()


USERS_REDIRECT = """\
import os, sys
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'debug.log'), max_kb=1)
Utils.prt('goes to the caller file')
"""


def test_configure_log_leaves_a_users_redirect_alone(tmp_path):
    theirs = tmp_path / 'theirs.txt'
    with open(theirs, 'w', encoding='utf-8') as handle:
        result = _run_child(USERS_REDIRECT, str(tmp_path), stdout=handle)
    assert result.returncode == 0, result.stderr
    assert 'goes to the caller file' in theirs.read_text()
    # stdout was already a regular file this process did not open, so the log
    # was configured but never taken over.
    assert not (tmp_path / 'debug.log').exists()


def test_a_second_configure_log_moves_the_log(tmp_path):
    body = """\
import os, sys
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'one.log'))
Utils.prt('in the first log')
Utils.configure_log(os.path.join(sys.argv[1], 'two.log'))
Utils.prt('in the second log')
"""
    result = _run_child(body, str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert 'in the first log' in (tmp_path / 'one.log').read_text()
    assert 'in the second log' in (tmp_path / 'two.log').read_text()
