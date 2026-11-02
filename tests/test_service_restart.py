"""Restarting, replacing and *leaving* a luks-tray that systemd supervises.

Three behaviours keep a supervised applet from fighting its own supervisor:

- `restart_self()` asks systemd to restart the unit, and waits until the request
  is queued (`systemctl --no-block`, run synchronously) instead of spawning a
  child that the cgroup teardown would kill; detaching the request with
  `systemd-run --scope` does not work and is pinned against below;
- `--replace` refuses to signal a holder that systemd supervises, because the
  supervisor brings it straight back and the two would ping-pong;
- `install_session_shutdown_handler()` quits the event loop when a session
  manager asks for it -- on the Qt builds that answer that request at all.

The third is an extra, not the fix: what keeps a terminated applet down is the
units' `Restart=on-failure` / `RestartPreventExitStatus=` pair, asserted in
`test_service.py`.  A build with no handler still stops cleanly at logout (the
distro Qt ends the session itself), so the slots are asserted here for what
they do -- connect once, quit, set `RestartNever` -- and not as the reason a
logout completes.
"""
import types

import pytest

from luks_tray import Main, Utils, service


class FakeLock:
    """Stands in for `Utils.SingleInstance` during a takeover attempt."""

    def __init__(self, results, path='/tmp/luks-tray-test/luks-tray.lock'):
        self.results = list(results)
        self.path = path
        self.acquired = 0

    def acquire(self, retries=0, delay=0.2):
        self.acquired += 1
        return self.results.pop(0) if self.results else False


class FakeTray:
    """The attributes `restart_self()` touches."""

    def __init__(self):
        self.hidden = 0
        self.quits = 0
        self.tray_icon = types.SimpleNamespace(hide=self._hide)
        self.app = types.SimpleNamespace(quit=self._quit)

    def _hide(self):
        self.hidden += 1

    def _quit(self):
        self.quits += 1

    restart_self = Main.LuksTray.restart_self
    _restart_via_systemd = Main.LuksTray._restart_via_systemd


@pytest.fixture
def supervised(monkeypatch):
    """Pretend a `--user` unit started this process."""
    monkeypatch.setenv('INVOCATION_ID', 'test-invocation')
    monkeypatch.setattr(service, 'supervised_here', lambda: True)
    return monkeypatch


# -- --replace -------------------------------------------------------------

def test_replace_refuses_to_kill_a_supervised_holder(monkeypatch):
    monkeypatch.setattr(service, 'supervised_by', lambda pid: 'luks-tray.service')
    monkeypatch.setattr(Utils.SingleInstance, 'read_pid', staticmethod(lambda path: 4242))

    def refuse(*_args, **_kwargs):
        raise AssertionError('a supervised holder must never be signalled')

    monkeypatch.setattr(Main.PyKill, 'kill_pid', refuse)
    lock = FakeLock([False])
    assert Main.acquire_lock_or_take_over(lock, replace=True) is False
    assert lock.acquired == 1


def test_replace_takes_over_from_an_unsupervised_holder(monkeypatch):
    monkeypatch.setattr(service, 'supervised_by', lambda pid: None)
    monkeypatch.setattr(Utils.SingleInstance, 'read_pid', staticmethod(lambda path: 4242))
    monkeypatch.setattr(Main.PyKill, 'kill_pid', staticmethod(lambda pid, targets: True))
    lock = FakeLock([False, True])
    assert Main.acquire_lock_or_take_over(lock, replace=True) is True
    assert lock.acquired == 2


def test_a_plain_start_never_looks_for_a_holder_to_kill(monkeypatch):
    monkeypatch.setattr(Utils.SingleInstance, 'read_pid', staticmethod(lambda path: 4242))

    def refuse(*_args, **_kwargs):
        raise AssertionError('a plain start must not signal anything')

    monkeypatch.setattr(Main.PyKill, 'kill_pid', refuse)
    monkeypatch.setattr(service, 'supervised_by',
                        lambda pid: pytest.fail('nothing to supervise here'))
    assert Main.acquire_lock_or_take_over(FakeLock([False]), replace=False) is False


# -- restart_self() --------------------------------------------------------

def test_restart_under_systemd_asks_the_supervisor(supervised):
    calls = []

    def request_restart(env=None, run=None):
        calls.append('restart')
        return 0

    supervised.setattr(service, 'request_restart', request_restart)
    # Leaving through the event loop is what lets the normal exit path release
    # the lock (and write the stop line, unless systemd's stop signal gets
    # there first); os._exit() would skip that, and spawning a child would put
    # it in the cgroup the restart is about to kill.
    supervised.setattr(Main.os, '_exit',
                       lambda code: pytest.fail('must leave through the event loop'))
    supervised.setattr(Main.subprocess, 'Popen',
                       lambda *args, **kwargs: pytest.fail('must not spawn a child'))
    tray = FakeTray()
    tray.restart_self()
    assert calls == ['restart']
    assert tray.hidden == 1
    assert tray.quits == 1


def test_restart_under_systemd_keeps_running_when_the_handoff_fails(supervised):
    def boom(env=None, run=None):
        raise service.ServiceError('no unit to restart')

    supervised.setattr(service, 'request_restart', boom)
    supervised.setattr(Main.os, '_exit', lambda code: pytest.fail('must not exit'))
    tray = FakeTray()
    tray.restart_self()
    assert tray.hidden == 0
    assert tray.quits == 0


def test_restart_under_systemd_stays_up_when_systemd_refuses(supervised):
    # A refused request leaves us with a working tray and an unchanged menu
    # row; emptying the cgroup "anyway" would leave neither.
    supervised.setattr(service, 'request_restart', lambda env=None, run=None: 5)
    tray = FakeTray()
    tray.restart_self()
    assert tray.hidden == 0
    assert tray.quits == 0


def test_restart_outside_systemd_still_spawns_a_child(monkeypatch):
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    monkeypatch.setattr(service, 'supervised_here', lambda: False)
    spawned, exits = [], []
    monkeypatch.setattr(Main.subprocess, 'Popen',
                        lambda args, env=None: spawned.append((args, env)))
    monkeypatch.setattr(Main.os, '_exit', exits.append)
    monkeypatch.setattr(Utils.SingleInstance, 'release_current', classmethod(lambda cls: None))
    tray = FakeTray()
    tray.restart_self()
    assert spawned[0][0][1:3] == ['-m', 'luks_tray.Main']
    assert exits == [0]


# -- the helpers behind both ----------------------------------------------

def test_in_systemd_service_reads_invocation_id():
    assert Utils.in_systemd_service({'INVOCATION_ID': 'abc'}) is True
    assert Utils.in_systemd_service({'INVOCATION_ID': ''}) is False
    assert Utils.in_systemd_service({}) is False


def _fake_systemd_run(tmp_path, monkeypatch):
    runner = tmp_path / 'systemd-run'
    runner.write_text('#!/bin/sh\n')
    runner.chmod(0o755)
    monkeypatch.setattr(Utils.shutil, 'which', lambda name, path=None: str(runner))
    return runner


def test_spawn_detached_moves_the_command_out_of_our_cgroup(tmp_path, monkeypatch):
    # For viewers (the log window), not for control requests: a restart asks
    # systemd synchronously, because a detached helper is created out of our
    # cgroup and dies with the stop it is trying to request.
    runner = _fake_systemd_run(tmp_path, monkeypatch)
    spawned = []
    Utils.spawn_detached(['konsole', '-e', 'less'],
                         spawn=lambda command, start_new_session=False: spawned.append(
                             (command, start_new_session)),
                         env={'INVOCATION_ID': 'abc', 'PATH': '/bin'})
    assert spawned == [([str(runner), '--user', '--scope', '--quiet', '--',
                         'konsole', '-e', 'less'], True)]


def test_spawn_detached_stays_plain_outside_a_unit(tmp_path, monkeypatch):
    _fake_systemd_run(tmp_path, monkeypatch)
    spawned = []
    Utils.spawn_detached(['true'], spawn=lambda command, start_new_session=False:
                         spawned.append((command, start_new_session)), env={'PATH': '/bin'})
    assert spawned == [(['true'], True)]


def test_spawn_detached_does_nothing_for_an_empty_command():
    assert Utils.spawn_detached([], spawn=lambda *a, **k: pytest.fail('spawned')) is None


def test_spawn_detached_can_keep_a_child_out_of_our_log(tmp_path, monkeypatch):
    # The log viewer is spawned while fds 1 and 2 are debug.log, and konsole
    # (the `x-terminal-emulator` alternative) is a Qt6 application: without
    # this, Qt's raw palette dump is appended to our log, where the Qt message
    # handler cannot reach it because the lines are another process's.
    _fake_systemd_run(tmp_path, monkeypatch)
    seen = {}

    def fake_spawn(command, **kwargs):
        seen['command'] = command
        seen.update(kwargs)

    Utils.spawn_detached(['konsole', '-e', 'less'], spawn=fake_spawn,
                         env={'PATH': '/bin'}, quiet=True)

    assert seen['command'] == ['konsole', '-e', 'less']
    assert seen['start_new_session'] is True
    assert seen['stdin'] == Utils.subprocess.DEVNULL
    assert seen['stdout'] == Utils.subprocess.DEVNULL
    assert seen['stderr'] == Utils.subprocess.DEVNULL


def test_spawn_detached_leaves_the_applet_s_own_streams_alone(tmp_path,
                                                             monkeypatch):
    # The restart spawn must keep writing into the session's log; only viewers
    # are quiet.
    _fake_systemd_run(tmp_path, monkeypatch)
    seen = {}
    Utils.spawn_detached(['true'],
                         spawn=lambda command, **kw: seen.update(kw, command=command),
                         env={'PATH': '/bin'})

    assert set(seen) == {'command', 'start_new_session'}


# -- a session that is ending ---------------------------------------------

class FakeSignal:
    """The one signal of `QApplication` the session handler connects to."""

    def __init__(self, refuse=False):
        self.slots = []
        self.refuse = refuse

    def connect(self, slot):
        if self.refuse:
            raise TypeError('QObject::connect: no such signal')
        self.slots.append(slot)


class FakeApp:
    """The `QApplication` surface `install_session_shutdown_handler()` uses."""

    def __init__(self, refuse_signal=False):
        self.commitDataRequest = FakeSignal(refuse=refuse_signal)
        self.quits = 0

    def quit(self):
        self.quits += 1


class FakeSessionManager:
    """Stands in for the `QSessionManager` Qt hands to `commitDataRequest`."""

    def __init__(self, refuse=False):
        self.restart_hints = []
        self.refuse = refuse

    def setRestartHint(self, hint):                 # noqa: N802 - Qt's spelling
        if self.refuse:
            raise RuntimeError('no session to hint at')
        self.restart_hints.append(hint)


class SessionTray:
    """A tray with only what the session-shutdown handlers touch."""

    install_session_shutdown_handler = Main.LuksTray.install_session_shutdown_handler
    _on_commit_data_request = Main.LuksTray._on_commit_data_request

    def __init__(self, app=None):
        self.app = FakeApp() if app is None else app


def test_a_session_manager_shutdown_quits_the_event_loop():
    tray = SessionTray()
    tray.install_session_shutdown_handler()
    assert len(tray.app.commitDataRequest.slots) == 1
    manager = FakeSessionManager()
    tray.app.commitDataRequest.slots[0](manager)
    # Quitting here returns exec() with 0; that the applet *stays* down is the
    # unit's doing (`Restart=on-failure`), not this slot's.  The hint is the
    # protocol's own way of saying "do not restore this client".
    assert tray.app.quits == 1
    assert manager.restart_hints == [Main.QSessionManager.RestartHint.RestartNever]


def test_a_qt_without_session_management_still_starts():
    # A PyQt wheel ships a Qt with no XSMP at all, and a session without a
    # manager (Wayland, a bare X session) never emits the signal: in both cases
    # it is inert.  It must not keep the tray from starting, either.
    tray = SessionTray(app=FakeApp(refuse_signal=True))
    tray.install_session_shutdown_handler()
    assert tray.app.commitDataRequest.slots == []


def test_a_manager_that_refuses_the_restart_hint_still_quits():
    tray = SessionTray()
    tray.install_session_shutdown_handler()
    tray.app.commitDataRequest.slots[0](FakeSessionManager(refuse=True))
    assert tray.app.quits == 1
