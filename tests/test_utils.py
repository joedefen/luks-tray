"""`Utils`: the logging destination, `prt()` and `prt_once()`.

The redirection itself is tested in a subprocess: `configure_log()` and
`prt()` call `os.dup2()`, which would replace the file descriptor pytest
captures on.  A child process is also the only honest way to produce the three
answers `check_stdout()` branches on -- stdout is a pipe, stdout is a terminal,
stdout is a regular file the caller opened -- because pytest's own capture turns
the inherited stdout into a regular file.
"""

import contextlib
import io
import os
import signal
import subprocess
import sys
import time
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
    # stdout was already a regular file this process did not open, so it was
    # never taken over: the caller's file still has the line, once.
    assert len(theirs.read_text().splitlines()) == 1
    # ... and prt() mirrored the line into the log anyway, because the log is
    # the default destination even when stdout goes somewhere else.
    assert 'goes to the caller file' in (tmp_path / 'debug.log').read_text()


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
    # Pin the terminal too: on a runner with none installed the auto-detection
    # would answer [] for a reason that has nothing to do with the rotated file.
    monkeypatch.setattr(Utils, '_less_follow_name', False)
    monkeypatch.setattr(Utils, '_TERMINALS', (('xterm', '-e', 'argv'),))
    monkeypatch.setattr(Utils.shutil, 'which', lambda name: f'/usr/bin/{name}')
    log = tmp_path / 'debug.log'
    argv = Utils.log_viewer_argv(str(log), str(tmp_path / 'debug.log.old'), '')
    assert argv == ['/usr/bin/xterm', '-e', 'less', '+F', str(log)]
    assert '--follow-name' not in argv


# --- the log is the default, even when started from a terminal --------------


def test_prt_mirrors_the_line_into_the_log_from_a_terminal(tmp_path, monkeypatch,
                                                           capsys):
    log = tmp_path / 'debug.log'
    # A terminal is the one stdout prt() must not take over: the person is
    # watching it. The line goes to both places instead.
    monkeypatch.setattr(Utils, '_is_tty', lambda: True)
    monkeypatch.setattr(Utils, '_is_reg', lambda: False)
    Utils.configure_log(str(log))
    try:
        Utils.prt('hello from a terminal')
    finally:
        Utils.configure_log('', max_kb=0)
    assert 'hello from a terminal' in capsys.readouterr().out
    assert 'hello from a terminal' in log.read_text()


def test_keep_stdout_does_not_mirror_anything(tmp_path, monkeypatch, capsys):
    log = tmp_path / 'debug.log'
    monkeypatch.setattr(Utils, '_is_tty', lambda: True)
    monkeypatch.setattr(Utils, '_is_reg', lambda: False)
    Utils.configure_log(str(log), keep_stdout=True)
    try:
        Utils.prt('terminal only, please')
    finally:
        Utils.configure_log('', max_kb=0)
    assert 'terminal only, please' in capsys.readouterr().out
    # -o/--stdout is the documented opt-out: nothing is written to the log.
    assert not log.exists()


def test_the_mirror_rotates_the_log_at_the_cap(tmp_path, monkeypatch):
    log = tmp_path / 'debug.log'
    log.write_text('x' * 4096 + '\n')
    monkeypatch.setattr(Utils, '_is_tty', lambda: True)
    monkeypatch.setattr(Utils, '_is_reg', lambda: False)
    Utils.configure_log(str(log), max_kb=1)
    try:
        Utils.prt('after the cap')
    finally:
        Utils.configure_log('', max_kb=0)
    assert 'after the cap' in log.read_text()
    assert 'x' * 4096 in (tmp_path / 'debug.log.old').read_text()


def test_a_stdout_that_is_already_the_log_is_not_written_twice(tmp_path,
                                                               monkeypatch):
    log = tmp_path / 'debug.log'
    monkeypatch.setattr(Utils, '_is_tty', lambda: False)
    Utils.configure_log(str(log))
    try:
        # `luks-tray >> debug.log`: same file, so the mirror must notice before
        # it appends a second copy of every line.
        with open(log, 'a', encoding='utf-8') as handle:
            with contextlib.redirect_stdout(handle):
                Utils.prt('said once')
    finally:
        Utils.configure_log('', max_kb=0)
    assert log.read_text().count('said once') == 1


# --- what the session reported, and where it began and ended ---------------


def test_only_failure_lines_are_counted(capsys, quiet_prt):
    Utils.prt('WARN: skipping auto-mount cleanup: not empty')
    assert Utils.session_problems() == 0
    Utils.prt('ERROR: no terminal emulator found')
    assert Utils.session_problems() == 1
    Utils.prt('exception:', 'boom')
    assert Utils.session_problems() == 2
    Utils.prt('FAIL: cannot restart the applet: no such file')
    assert Utils.session_problems() == 3
    assert 'FAIL: cannot restart' in Utils._problems.last


def test_note_problem_returns_the_running_count(quiet_prt):
    assert Utils.note_problem('ERR: mount failed') == 1
    assert Utils.note_problem('ERR: unlock failed') == 2
    assert Utils.session_problems() == 2


def test_clear_session_problems_forgets_the_tally(quiet_prt):
    Utils.note_problem('ERR: whatever')
    Utils.clear_session_problems()
    assert Utils.session_problems() == 0
    assert Utils._problems.last == ''


def test_log_session_start_names_the_build_and_the_pid(capsys, quiet_prt):
    Utils.note_problem('ERR: from the run before')
    Utils.log_session_start('luks-tray', '9.9.9', note=' (systemd)')
    out = capsys.readouterr().out
    assert '=== luks-tray v9.9.9 started, pid ' in out
    assert f'pid {os.getpid()}' in out
    assert '(systemd)' in out
    # The banner has no [file:line] footer: it is not about a line of code.
    assert out.rstrip().endswith('===')
    # A new session starts with a clean tally, whatever the file holds.
    assert Utils.session_problems() == 0


def test_log_session_stop_reports_how_many_failures_happened(capsys, quiet_prt):
    Utils.note_problem('ERR: one')
    Utils.note_problem('ERR: two')
    Utils.log_session_stop('luks-tray', '9.9.9', note=' (signal 15)')
    out = capsys.readouterr().out
    assert '=== luks-tray v9.9.9 stopped, pid ' in out
    assert '(signal 15)' in out
    assert '2 errors' in out


def test_a_clean_session_stop_does_not_mention_errors(capsys, quiet_prt):
    Utils.log_session_stop('luks-tray', '9.9.9')
    out = capsys.readouterr().out
    assert 'stopped, pid ' in out
    assert 'error' not in out


def test_a_termination_logs_a_stop_line_and_reprises_the_signal(monkeypatch,
                                                                quiet_prt):
    installed, killed, stopped = {}, [], []

    def fake_signal(number, handler):
        installed[number] = handler

    monkeypatch.setattr(Utils.signal, 'signal', fake_signal)
    monkeypatch.setattr(Utils.os, 'kill', lambda pid, number: killed.append(number))
    monkeypatch.setattr(Utils, 'log_session_stop',
                        lambda name, version, note='': stopped.append(note))
    Utils.install_termination_log('luks-tray', '9.9.9', signals=(signal.SIGTERM,))
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stopped == [' (signal 15)']
    # Re-raised, so the exit status systemd sees is the one it expects.
    assert killed == [signal.SIGTERM]
    assert installed[signal.SIGTERM] is not None


TERMINATES = """\
import os, sys, time
from luks_tray import Utils
Utils.configure_log(os.path.join(sys.argv[1], 'debug.log'))
Utils.install_termination_log('luks-tray', '9.9.9')
Utils.log_session_start('luks-tray', '9.9.9')
print('ready', flush=True)
time.sleep(60)
"""


def test_a_real_sigterm_leaves_the_stop_line_and_the_signal_status(tmp_path):
    # The end-to-end version of the test above: a real process, a real signal,
    # and a supervisor's view of the exit status (death *by* the signal, not a
    # quiet exit 0 that a unit's Restart=always would misread).
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    child = subprocess.Popen([sys.executable, '-c', TERMINATES, str(tmp_path)],
                             cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    log = tmp_path / 'debug.log'
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if log.exists() and 'ready' in log.read_text():
                break
            time.sleep(0.05)
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    text = log.read_text()
    assert 'started' in text
    assert 'stopped' in text
    assert '(signal 15)' in text
    assert child.returncode == -signal.SIGTERM


def test_a_termination_log_that_cannot_be_installed_is_a_no_op(monkeypatch):
    def refuse(_number, _handler):
        raise ValueError('signal only works in main thread')

    monkeypatch.setattr(Utils.signal, 'signal', refuse)
    # No exception escapes: a signal handler that cannot be installed must not
    # be the reason the applet fails to start.
    Utils.install_termination_log('luks-tray', '9.9.9', signals=(signal.SIGTERM,))


def test_prt_qt_keeps_qt_s_own_file_and_line(capsys, quiet_prt):
    Utils.prt_qt('bad paint', '[painter.cpp:42]')
    out = capsys.readouterr().out
    assert 'qt: bad paint' in out
    assert '[painter.cpp:42]' in out
    assert '[Utils.py' not in out      # the footer is Qt's, not this module's
