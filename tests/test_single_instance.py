"""The single-instance lock, the guarded `--replace` takeover, and restarts.

Two applets in one session mean two tray icons and two processes racing over
the same history file, so the lock -- not a process-name scan -- is the
authority.  Killing an incumbent used to be what a plain start did; with a DE
autostart *and* a supervisor that restarts the tray, that ping-pong never ends.
These tests pin the order instead: the lock is taken first, a plain start
refuses, and only `--replace` terminates a holder -- and only after its command
line is confirmed to still be luks-tray.

`restart_self()` is the menu entry: it has to release the lock before spawning
the replacement (``os._exit()`` skips atexit) and give the lock back if the
spawn fails.
"""

import os
import signal
import types

import pytest

import luks_tray.Main as main_module
import luks_tray.Utils as Utils
from luks_tray.Main import LuksTray
from luks_tray.Utils import PyKill, SingleInstance, command_names, script_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def keys(*argv):
    """Comparable names for one process command line."""
    return {script_key(name) for name in command_names(list(argv))}


# --------------------------------------------------------------------------
# command-line recognition
# --------------------------------------------------------------------------


def test_restarted_instance_is_recognised():
    assert 'luks-tray' in keys('/usr/bin/python3', '-m', 'luks_tray.Main')
    # interpreter options before -m must not hide the module
    assert 'luks-tray' in keys('/usr/bin/python3.12', '-u', '-O',
                               '-m', 'luks_tray.Main', '--stdout')


def test_console_script_is_recognised():
    assert 'luks-tray' in keys('/usr/bin/python3', '/home/u/.local/bin/luks-tray')
    assert 'luks-tray' in keys('/usr/bin/python3', 'luks-tray')


def test_script_file_is_recognised_by_its_package_directory():
    # python luks_tray/Main.py -- only the directory names the app
    assert 'luks-tray' in keys('/usr/bin/python3',
                               '/home/u/Projects/luks-tray/luks_tray/Main.py')


def test_the_development_runner_is_recognised():
    # ./runner sits at the project root, whose directory name is the app name.
    assert 'luks-tray' in keys('/usr/bin/python3',
                               '/home/u/Projects/luks-tray/runner')


def test_unrelated_processes_are_not_matched():
    assert 'luks-tray' not in keys('/usr/bin/python3', '-m', 'pytest', 'tests')
    assert 'luks-tray' not in keys('/usr/bin/python3', '-m', 'luks_tools.Main')
    assert 'luks-tray' not in keys('/usr/bin/python3', '-c', 'import luks_tray')
    assert 'luks-tray' not in keys('/usr/bin/ssh', 'host')
    assert 'luks-tray' not in keys()


def test_matching_pids_finds_restarted_instance_only(monkeypatch):
    lines = [
        '    PID COMMAND\n',                                       # ps header
        '    123 /usr/bin/python3 -m luks_tray.Main\n',            # restarted
        '    456 /usr/bin/python3 -m pytest tests\n',              # unrelated
        f'    {os.getpid()} /usr/bin/python3 -m luks_tray.Main\n',  # ourselves
    ]
    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))

    assert PyKill.matching_pids(['luks-tray']) == {
        123: '123 /usr/bin/python3 -m luks_tray.Main'}


def test_kill_pid_is_harmless_when_the_pid_is_no_longer_luks_tray(monkeypatch):
    """A recorded pid may have been recycled: never signal on a name mismatch."""
    lines = ['    123 /usr/sbin/cupsd -l\n']
    killed = []
    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))
    monkeypatch.setattr(Utils.os, 'kill',
                        lambda pid, sig: killed.append((pid, sig)))

    assert PyKill.kill_pid(123, ['luks-tray']) is False
    assert killed == []


def test_kill_pid_refuses_its_own_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(Utils.os, 'kill',
                        lambda pid, sig: killed.append((pid, sig)))

    assert PyKill.kill_pid(os.getpid(), ['luks-tray']) is False
    assert killed == []


def test_kill_pid_terminates_a_verified_instance(monkeypatch):
    state = {'alive': True}

    def fake_popen(command):
        if not state['alive']:
            return iter(['    PID COMMAND\n'])
        return iter(['    123 /usr/bin/python3 -m luks_tray.Main\n'])

    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            state['alive'] = False

    monkeypatch.setattr(Utils.os, 'popen', fake_popen)
    monkeypatch.setattr(Utils.os, 'kill', fake_kill)

    assert PyKill.kill_pid(123, ['luks-tray'], grace=1.0, delay=0) is True
    assert killed == [(123, signal.SIGTERM)]


def test_kill_pid_escalates_to_sigkill_when_sigterm_is_ignored(monkeypatch):
    state = {'alive': True}
    killed = []

    def fake_popen(command):
        if not state['alive']:
            return iter(['    PID COMMAND\n'])
        return iter(['    123 /usr/bin/python3 -m luks_tray.Main\n'])

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGKILL:
            state['alive'] = False

    monkeypatch.setattr(Utils.os, 'popen', fake_popen)
    monkeypatch.setattr(Utils.os, 'kill', fake_kill)

    assert PyKill.kill_pid(123, ['luks-tray'], grace=0, delay=0) is True
    assert killed == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


# --------------------------------------------------------------------------
# development launcher
# --------------------------------------------------------------------------


def test_runner_still_calls_the_package_entry_point():
    with open(os.path.join(ROOT, 'runner'), encoding='utf-8') as handle:
        text = handle.read()
    assert 'from luks_tray.Main import main' in text


# --------------------------------------------------------------------------
# single-instance lock
# --------------------------------------------------------------------------


def test_second_instance_cannot_take_the_lock(tmp_path):
    path = str(tmp_path / 'luks-tray.lock')
    first, second = SingleInstance(path), SingleInstance(path)
    assert first.acquire() is True
    try:
        assert SingleInstance.read_pid(path) == os.getpid()
        assert second.acquire() is False
    finally:
        first.release()
    # Once released (as restart_self does) the replacement may take it.
    assert second.acquire() is True
    second.release()
    assert SingleInstance.read_pid(path) == os.getpid()


def test_release_current_frees_the_lock(tmp_path):
    path = str(tmp_path / 'luks-tray.lock')
    first = SingleInstance(path)
    assert first.acquire() is True
    try:
        assert SingleInstance.current is first
        SingleInstance.release_current()
        assert SingleInstance.current is None
        assert SingleInstance(path).acquire() is True
    finally:
        SingleInstance.release_current()


def test_lock_file_without_a_holder_does_not_block(tmp_path):
    """A crashed instance leaves a pid file, but the kernel dropped the lock."""
    path = tmp_path / 'luks-tray.lock'
    path.write_text('999999\n', encoding='utf-8')
    lock = SingleInstance(str(path))
    assert lock.acquire() is True
    lock.release()


# --------------------------------------------------------------------------
# the lock is taken first; killing is an explicit takeover
# --------------------------------------------------------------------------


class FakeLock:
    """Stands in for SingleInstance without touching the filesystem."""

    def __init__(self, results, path='/tmp/luks-tray-test/luks-tray.lock'):
        self.path = path
        self.results = list(results)
        self.attempts = 0

    def acquire(self, retries=0, delay=0.2):
        self.attempts += 1
        return self.results.pop(0) if self.results else False

    def release(self):
        pass


def install_fake_killer(monkeypatch, gone=True):
    """Replace the module's PyKill with one that records every signal request."""
    calls = []

    class FakeKill:
        @staticmethod
        def kill_pid(pid, targets, **kwargs):
            calls.append((pid, list(targets)))
            return gone

    monkeypatch.setattr(main_module, 'PyKill', FakeKill)
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    return calls


def install_holder(monkeypatch, pid=4242):
    monkeypatch.setattr(main_module.Utils.SingleInstance, 'read_pid',
                        staticmethod(lambda path: pid))


def test_plain_start_refuses_instead_of_killing_the_incumbent(monkeypatch):
    """The regression: without --replace, a second launch must not signal."""
    lock = FakeLock([False])
    calls = install_fake_killer(monkeypatch)
    install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock) is False
    assert calls == []            # the incumbent was left alone
    assert lock.attempts == 1     # and the failed start did not spin


def test_replace_kills_the_verified_holder_then_takes_the_lock(monkeypatch):
    lock = FakeLock([False, True])
    calls = install_fake_killer(monkeypatch)
    install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is True
    assert calls == [(4242, ['luks-tray'])]
    assert lock.attempts == 2


def test_replace_does_not_kill_a_pid_that_is_not_luks_tray(monkeypatch):
    lock = FakeLock([False, False])
    calls = install_fake_killer(monkeypatch, gone=False)
    install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is False
    assert calls == [(4242, ['luks-tray'])]   # asked, but no takeover happened
    assert lock.attempts == 1


def test_replace_without_a_recorded_holder_does_not_kill(monkeypatch):
    lock = FakeLock([False])
    calls = install_fake_killer(monkeypatch)
    install_holder(monkeypatch, pid=None)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is False
    assert calls == []


def test_main_exits_3_without_touching_processes_when_lock_is_held(monkeypatch):
    """main() must not build a killer before it knows it owns the lock."""

    class FakeIniTool:
        folder = '/tmp/luks-tray-test'
        ini_path = os.path.join(folder, 'config.ini')
        log_path = os.path.join(folder, 'debug.log')
        lock_path = os.path.join(folder, 'luks-tray.lock')

        def __init__(self, paths_only=False):
            pass

        def ensure_folder(self):
            return self.folder

        def update_config(self):
            return False

    class RefusingLock:
        def __init__(self, path):
            self.path = path

        def acquire(self, retries=0, delay=0.2):
            return False

        def release(self):
            pass

        @staticmethod
        def read_pid(path):
            return 4242

    class NoKill:
        def __init__(self, *args, **kwargs):
            raise AssertionError('PyKill must not run before the lock is ours')

        @staticmethod
        def kill_pid(*args, **kwargs):
            raise AssertionError('PyKill must not run before the lock is ours')

    reported = []
    fake_utils = types.SimpleNamespace(
        capture_console=lambda: None,
        configure_log=lambda path, keep_stdout=False: None,
        in_systemd_service=lambda: False,
        error_print=reported.append,
        SingleInstance=RefusingLock)

    monkeypatch.setattr(main_module, 'IniTool', FakeIniTool)
    monkeypatch.setattr(main_module, 'Utils', fake_utils)
    monkeypatch.setattr(main_module, 'PyKill', NoKill)
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    monkeypatch.setattr('sys.argv', ['luks-tray'])

    with pytest.raises(SystemExit) as raised:
        main_module.main()

    assert raised.value.code == 3
    assert 'already running' in reported[0]
    assert '4242' in reported[0]
    assert '--replace' in reported[0]


# --------------------------------------------------------------------------
# restart_self()
# --------------------------------------------------------------------------


def bare_tray():
    """A LuksTray with only what restart_self() touches."""
    tray = LuksTray.__new__(LuksTray)
    hidden = []
    tray.tray_icon = types.SimpleNamespace(hide=lambda: hidden.append(True))
    tray.hidden = hidden
    return tray


def test_restart_self_releases_the_lock_and_starts_the_module(monkeypatch, tmp_path):
    lock = SingleInstance(str(tmp_path / 'luks-tray.lock'))
    assert lock.acquire() is True
    tray = bare_tray()
    spawned, exits = [], []
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    monkeypatch.setattr(main_module.subprocess, 'Popen',
                        lambda args, env=None: spawned.append((args, env)))
    monkeypatch.setattr(main_module.os, '_exit', lambda code: exits.append(code))
    monkeypatch.setattr(main_module.sys, 'argv',
                        ['luks-tray', '-D', '--replace'])

    tray.restart_self()

    args, env = spawned[0]
    assert args[:3] == [os.sys.executable, '-m', 'luks_tray.Main']
    # --replace is never inherited: the lock was released, so there is nothing
    # to take over, and a destructive flag must not survive the restart.
    assert '--replace' not in args
    assert '-D' in args
    assert env['PYTHONPATH'].split(os.pathsep)[0] == ROOT
    assert tray.hidden == [True]
    assert Utils.SingleInstance.current is None
    assert exits == [0]


def test_a_failed_spawn_takes_the_lock_back(monkeypatch, tmp_path):
    lock = SingleInstance(str(tmp_path / 'luks-tray.lock'))
    assert lock.acquire() is True
    tray = bare_tray()
    exits, reported = [], []
    monkeypatch.setattr(main_module, 'prt',
                        lambda *args, **kw: reported.append(' '.join(map(str, args))))

    def failing_popen(args, env=None):
        raise OSError('no such interpreter')

    monkeypatch.setattr(main_module.subprocess, 'Popen', failing_popen)
    monkeypatch.setattr(main_module.os, '_exit', lambda code: exits.append(code))
    monkeypatch.setattr(main_module.sys, 'argv', ['luks-tray'])

    try:
        tray.restart_self()
        # The running tray is still alive, so it must still own the lock.
        assert Utils.SingleInstance.current is lock
        assert exits == []
        assert any('cannot restart' in line for line in reported)
    finally:
        SingleInstance.release_current()
