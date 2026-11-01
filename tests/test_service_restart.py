"""Restarting and replacing a luks-tray that systemd supervises.

Two behaviours keep a supervised applet from fighting its own supervisor:

- `restart_self()` hands the restart to systemd instead of spawning a child
  that `Restart=always` would then duplicate (or kill with our cgroup);
- `--replace` refuses to signal a holder that systemd supervises, because the
  supervisor brings it straight back and the two would ping-pong.
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
    """The two attributes `restart_self()` touches."""

    def __init__(self):
        self.hidden = 0
        self.tray_icon = types.SimpleNamespace(hide=self._hide)

    def _hide(self):
        self.hidden += 1

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
    supervised.setattr(service, 'detached_restart',
                       lambda env=None, spawn=None: calls.append('restart'))
    supervised.setattr(Utils.SingleInstance, 'release_current',
                       classmethod(lambda cls: calls.append('release')))
    supervised.setattr(Main.os, '_exit', lambda code: calls.append(('exit', code)))
    supervised.setattr(Main.subprocess, 'Popen',
                       lambda *args, **kwargs: pytest.fail('must not spawn a child'))
    tray = FakeTray()
    tray.restart_self()
    assert calls == ['restart', 'release', ('exit', 0)]
    assert tray.hidden == 1


def test_restart_under_systemd_keeps_running_when_the_handoff_fails(supervised):
    def boom(env=None, spawn=None):
        raise service.ServiceError('no unit to restart')

    supervised.setattr(service, 'detached_restart', boom)
    supervised.setattr(Main.os, '_exit', lambda code: pytest.fail('must not exit'))
    tray = FakeTray()
    tray.restart_self()
    assert tray.hidden == 0


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
    runner = _fake_systemd_run(tmp_path, monkeypatch)
    spawned = []
    Utils.spawn_detached(['systemctl', '--user', 'restart', 'luks-tray.service'],
                         spawn=lambda command, start_new_session=False: spawned.append(
                             (command, start_new_session)),
                         env={'INVOCATION_ID': 'abc', 'PATH': '/bin'})
    assert spawned == [([str(runner), '--user', '--scope', '--quiet', '--',
                         'systemctl', '--user', 'restart', 'luks-tray.service'], True)]


def test_spawn_detached_stays_plain_outside_a_unit(tmp_path, monkeypatch):
    _fake_systemd_run(tmp_path, monkeypatch)
    spawned = []
    Utils.spawn_detached(['true'], spawn=lambda command, start_new_session=False:
                         spawned.append((command, start_new_session)), env={'PATH': '/bin'})
    assert spawned == [(['true'], True)]


def test_spawn_detached_does_nothing_for_an_empty_command():
    assert Utils.spawn_detached([], spawn=lambda *a, **k: pytest.fail('spawned')) is None
