"""`Utils`: the logging destination, `prt()` and `prt_once()`.

The redirection itself is tested in a subprocess: `configure_log()` and
`prt()` call `os.dup2()`, which would replace the file descriptor pytest
captures on.  A child process is also the only honest way to produce the three
answers `check_stdout()` branches on -- stdout is a pipe, stdout is a terminal,
stdout is a regular file the caller opened -- because pytest's own capture turns
the inherited stdout into a regular file.
"""

import io
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
    # The size cap moved the older content aside instead of truncating it: one
    # generation, under the name the menu's viewer pages as its second file.
    assert 'first message' in (tmp_path / 'debug.log.old').read_text()


USERS_REDIRECT = """\
import os, sys
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'debug.log'), max_kb=1)
Utils.prt('goes to the caller file')
"""


CREATES_LOG = """\
import os, sys
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'debug.log'))
Utils.prt('one line')
"""


def test_the_log_is_created_owner_only(tmp_path):
    # The log names container paths and mount points, so it is not for other
    # accounts: the umask a desktop hands us must not decide the mode.
    result = _run_child(CREATES_LOG, str(tmp_path))
    assert result.returncode == 0, result.stderr
    mode = (tmp_path / 'debug.log').stat().st_mode
    assert mode & 0o077 == 0


def test_a_wider_log_left_by_an_older_version_is_tightened(tmp_path):
    path = tmp_path / 'debug.log'
    path.write_text('from an older run\n')
    os.chmod(path, 0o666)

    result = _run_child(CREATES_LOG, str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert path.stat().st_mode & 0o077 == 0
    # Tightening is not truncating: the older content is still there.
    assert 'from an older run' in path.read_text()


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


# --- keeping a console for start-up errors ---------------------------------


def test_capture_console_duplicates_stderr_once(monkeypatch):
    monkeypatch.setattr(Utils, '_console_fd', None)
    fd = Utils.capture_console()
    try:
        assert fd >= 0
        # The duplicate is kept, so a second call never leaks another fd.
        assert Utils.capture_console() == fd
    finally:
        os.close(fd)


def test_error_print_writes_to_the_captured_console(tmp_path, monkeypatch):
    path = tmp_path / 'console.txt'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    monkeypatch.setattr(Utils, '_console_fd', fd)
    try:
        Utils.error_print('another luks-tray is already running')
    finally:
        os.close(fd)
    # The newline is added for the caller, so a message and a print() line
    # never run together on the user's terminal.
    assert path.read_text() == 'another luks-tray is already running\n'


def test_error_print_falls_back_when_no_console_was_captured(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(Utils, '_console_fd', -1)
    monkeypatch.setattr(sys, '__stderr__', stream)

    Utils.error_print('boom')

    assert stream.getvalue() == 'boom\n'


# --- rotation, and the second file the log viewer pages --------------------


def test_rotate_log_keeps_a_log_below_the_cap(tmp_path):
    log = tmp_path / 'debug.log'
    log.write_text('small\n')
    Utils.configure_log(str(log), max_kb=1)
    try:
        assert Utils.rotate_log() is False
        assert log.exists()
        assert not (tmp_path / 'debug.log.old').exists()
    finally:
        Utils.configure_log('', max_kb=0)


def test_rotate_log_moves_the_log_aside_atomically(tmp_path):
    log = tmp_path / 'debug.log'
    log.write_text('x' * 4096 + '\n')
    Utils.configure_log(str(log), max_kb=1)
    try:
        assert Utils.rotate_log() is True
    finally:
        Utils.configure_log('', max_kb=0)
    old = tmp_path / 'debug.log.old'
    # The current file is gone (the caller reopens it) and the content is in
    # the one generation we keep.
    assert not log.exists()
    assert old.read_text().startswith('x' * 4096)


def test_rotate_log_discards_a_previous_generation(tmp_path):
    log = tmp_path / 'debug.log'
    old = tmp_path / 'debug.log.old'
    old.write_text('last run\n')
    log.write_text('y' * 4096 + '\n')
    assert Utils.rotate_log(str(log), max_bytes=1024) is True
    assert 'last run' not in old.read_text()


def test_rotate_log_of_a_missing_file_is_a_no_op(tmp_path):
    assert Utils.rotate_log(str(tmp_path / 'absent.log'), max_bytes=1) is False
    assert Utils.rotate_log('', max_bytes=1) is False


def test_rotate_log_reports_a_failed_rename(tmp_path, monkeypatch):
    log = tmp_path / 'debug.log'
    log.write_text('z' * 4096)
    monkeypatch.setattr(Utils.os, 'replace', lambda *args: (_ for _ in ()).throw(
        OSError('read-only filesystem')))
    # A log that cannot be moved stays where it is; prt() must not raise.
    assert Utils.rotate_log(str(log), max_bytes=1) is False
    assert log.exists()


# --- -o/--stdout ----------------------------------------------------------


KEEPS_STDOUT = """\
import os, sys
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'debug.log'), keep_stdout=True)
Utils.prt('went to stdout')
print('still', 'stdout')
"""


def test_configure_log_keeps_a_piped_stdout_when_asked(tmp_path):
    result = _run_child(KEEPS_STDOUT, str(tmp_path))
    assert result.returncode == 0, result.stderr
    # -o means "the shell's stdout is the log", even when it is a pipe that
    # configure_log() would otherwise treat as "nowhere to write".
    assert 'went to stdout' in result.stdout
    assert 'still stdout' in result.stdout
    assert not (tmp_path / 'debug.log').exists()


def test_keep_stdout_is_recorded_even_though_nothing_was_opened(quiet_prt):
    Utils.configure_log('/tmp/lt-untouched.log', keep_stdout=True)
    try:
        assert Utils._log.keep_stdout is True
        assert Utils._log.opened_path == ''
    finally:
        Utils.configure_log('', max_kb=0, keep_stdout=False)


# --- finding a terminal to show the log in --------------------------------


def test_terminal_argv_uses_the_configured_prefix():
    argv = Utils.terminal_argv(['less', '+F', '/tmp/debug.log'],
                               'foot -e')
    assert argv == ['foot', '-e', 'less', '+F', '/tmp/debug.log']


def test_terminal_argv_quotes_for_a_glib_style_terminal(monkeypatch):
    # A terminal that takes the whole command line as one argument needs the
    # command joined, or it would be handed four separate words.
    monkeypatch.setattr(Utils, '_TERMINALS', (('xfce4-terminal', '-e', 'string'),))
    monkeypatch.setattr(Utils.shutil, 'which', lambda name: f'/usr/bin/{name}')
    argv = Utils.terminal_argv(['less', '+F', '/tmp/my log'])
    assert argv == ['/usr/bin/xfce4-terminal', '-e',
                    "less +F '/tmp/my log'"]


def test_terminal_argv_falls_through_to_the_first_installed_terminal(monkeypatch):
    installed = {'konsole': '/usr/bin/konsole'}
    monkeypatch.setattr(Utils, '_TERMINALS',
                        (('kitty', '-e', 'argv'), ('konsole', '-e', 'argv')))
    monkeypatch.setattr(Utils.shutil, 'which', installed.get)
    assert Utils.terminal_argv(['less', '/tmp/debug.log']) == [
        '/usr/bin/konsole', '-e', 'less', '/tmp/debug.log']


def test_terminal_argv_reports_no_terminal(monkeypatch):
    monkeypatch.setattr(Utils, '_TERMINALS', (('kitty', '-e', 'argv'),))
    monkeypatch.setattr(Utils.shutil, 'which', lambda name: None)
    assert Utils.terminal_argv(['less', '/tmp/debug.log']) == []
    # An empty command is the menu's "can a viewer start at all?" question.
    assert Utils.terminal_argv([]) == []


def test_an_unparseable_log_viewer_falls_back_to_auto_detection(monkeypatch,
                                                               quiet_prt):
    monkeypatch.setattr(Utils, '_TERMINALS', (('xterm', '-e', 'argv'),))
    monkeypatch.setattr(Utils.shutil, 'which', lambda name: '/usr/bin/xterm')
    assert Utils.terminal_argv(['less', 'f'], '"unclosed') == [
        '/usr/bin/xterm', '-e', 'less', 'f']


def test_less_follow_name_is_probed_once(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0,
                                           b'Usage: less --follow-name', b'')

    monkeypatch.setattr(Utils, '_less_follow_name', None)
    monkeypatch.setattr(Utils.subprocess, 'run', fake_run)
    assert Utils.less_follow_name_supported() is True
    assert Utils.less_follow_name_supported() is True
    assert len(calls) == 1


def test_less_without_follow_name_is_still_usable(monkeypatch):
    monkeypatch.setattr(Utils, '_less_follow_name', None)
    monkeypatch.setattr(Utils.subprocess, 'run', lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, b'Usage: less', b''))
    assert Utils.less_follow_name_supported() is False


def test_less_that_cannot_be_run_is_reported_as_unsupported(monkeypatch):
    monkeypatch.setattr(Utils, '_less_follow_name', None)

    def boom(*_args, **_kwargs):
        raise FileNotFoundError('less')

    monkeypatch.setattr(Utils.subprocess, 'run', boom)
    assert Utils.less_follow_name_supported() is False


def test_log_viewer_argv_pages_the_rotated_file_too(monkeypatch, tmp_path):
    monkeypatch.setattr(Utils, '_less_follow_name', True)
    log = tmp_path / 'debug.log'
    old = tmp_path / 'debug.log.old'
    log.write_text('now\n')
    old.write_text('before\n')
    argv = Utils.log_viewer_argv(str(log), str(old), 'foot -e')
    assert argv == ['foot', '-e', 'less', '--follow-name', '+F',
                    str(log), str(old)]


def test_log_viewer_argv_skips_a_rotated_file_that_is_not_there(monkeypatch,
                                                                tmp_path):
    monkeypatch.setattr(Utils, '_less_follow_name', False)
    log = tmp_path / 'debug.log'
    argv = Utils.log_viewer_argv(str(log), str(tmp_path / 'debug.log.old'), '')
    assert argv[-2:] == ['+F', str(log)]
    assert '--follow-name' not in argv
