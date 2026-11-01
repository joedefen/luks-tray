"""`LuksOps`: the command contract, and the LUKS primitives built on it.

Two contracts are pinned here because everything else depends on them:

* `run_cmd()`/`sudo_cmd()` return `None` on success and a `'FAIL: ...'` string
  otherwise, and they append to `errs` when one is *passed* -- testing against
  `None` rather than truthiness, because callers routinely pass a fresh `[]`;
* `run_cmd_capture()` returns the triple and converts a deadline into
  `TIMEOUT_RC`, so a hung `cryptsetup` cannot freeze the tray.

The mount path is exercised with fake commands: the assertions are about the
order of operations (create the mount point as the user, unlock, make the
filesystem, then mount) and about what a failure leaves behind, not about
running `cryptsetup` on the test machine.
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from luks_tray import LuksOps
from luks_tray.ContainerType import LUKS, TCRYPT
from luks_tray.LuksOps import (
    _as_text,
    _mount_manual,
    _setup_loop_device,
    _unlock_luks,
    ensure_dir,
    generate_uuid_for_file_path,
    get_container_identity,
    get_luks_uuid_for_file,
    mount_luks_container,
    run_cmd,
    run_cmd_capture,
    run_unmount,
    sudo_cmd,
    sudo_run,
    unlock_container,
)
from luks_tray.Utils import TIMEOUT_RC


# --- process execution -----------------------------------------------------


def test_generate_uuid_for_file_path_is_stable_and_uuid_shaped():
    first = generate_uuid_for_file_path('/vaults/a.luks')
    assert first == generate_uuid_for_file_path('/vaults/a.luks')
    assert first != generate_uuid_for_file_path('/vaults/b.luks')
    groups = first.split('-')
    assert [len(group) for group in groups] == [8, 4, 4, 4, 12]
    assert all(int(group, 16) >= 0 for group in groups)


def test_get_luks_uuid_for_file_does_not_run_a_command_for_an_empty_path(
        monkeypatch):
    monkeypatch.setattr(LuksOps, 'run_cmd_capture',
                        lambda *a, **k: pytest.fail('should not run blkid'))
    assert get_luks_uuid_for_file('') == ''


def test_get_luks_uuid_for_file_reads_the_uuid(monkeypatch):
    monkeypatch.setattr(LuksOps, 'run_cmd_capture',
                        lambda *a, **k: (0, '  abcd-1234 \n', ''))
    assert get_luks_uuid_for_file('/vaults/a.luks') == 'abcd-1234'


def test_get_luks_uuid_for_file_reports_nothing_when_blkid_fails(monkeypatch):
    monkeypatch.setattr(LuksOps, 'run_cmd_capture',
                        lambda *a, **k: (2, '', 'bad file'))
    assert get_luks_uuid_for_file('/vaults/a.luks') == ''


def test_the_identity_of_a_luks_file_is_its_uuid(monkeypatch):
    monkeypatch.setattr(LuksOps, 'get_luks_uuid_for_file', lambda _path: 'UUID-1')
    monkeypatch.setattr(LuksOps, 'salt_identity', lambda _path: 'salt-x')
    assert get_container_identity('/vaults/a.luks') == 'UUID-1'


def test_the_identity_of_an_encrypted_header_is_its_salt(monkeypatch):
    monkeypatch.setattr(LuksOps, 'get_luks_uuid_for_file', lambda _path: '')
    monkeypatch.setattr(LuksOps, 'salt_identity', lambda _path: 'salt-x')
    assert get_container_identity('/vaults/a.crypt') == 'salt-x'


def test_the_identity_falls_back_to_the_path(monkeypatch):
    monkeypatch.setattr(LuksOps, 'get_luks_uuid_for_file', lambda _path: '')
    monkeypatch.setattr(LuksOps, 'salt_identity', lambda _path: '')
    assert get_container_identity('/vaults/a.bin') == (
        generate_uuid_for_file_path('/vaults/a.bin'))


def test_the_identity_of_an_empty_path_is_empty(monkeypatch):
    monkeypatch.setattr(LuksOps, 'salt_identity',
                        lambda _path: pytest.fail('nothing to read'))
    assert get_container_identity('') == ''


def test_run_cmd_returns_none_on_success():
    assert run_cmd(['true']) is None


def test_run_cmd_renders_a_failure_with_the_status():
    err = run_cmd(['false'])
    assert err.startswith('FAIL: false:')
    assert '[rc=1]' in err


def test_run_cmd_appends_to_a_passed_error_list():
    errs = []
    assert run_cmd(['false'], errs) is not None
    assert len(errs) == 1


def test_run_cmd_treats_an_empty_error_list_as_a_list():
    # The old code tested `if errs:`, which skipped a freshly created [].
    errs = []
    run_cmd(['false'], errs)
    assert errs != []


def test_run_cmd_capture_returns_the_triple():
    rc, out, err = run_cmd_capture(['echo', 'hi'])
    assert (rc, out, err) == (0, 'hi\n', '')


def test_run_cmd_capture_feeds_stdin():
    rc, out, _err = run_cmd_capture(['cat'], 'secret')
    assert rc == 0
    assert out == 'secret'


def test_run_cmd_capture_turns_a_deadline_into_timeout_rc():
    sleeper = [sys.executable, '-c', 'import time; time.sleep(5)']
    rc, _out, err = run_cmd_capture(sleeper, timeout=0.05)
    assert rc == TIMEOUT_RC
    assert 'timed out after' in err


def test_as_text_handles_none_bytes_and_text():
    assert _as_text(None) == ''
    assert _as_text(b'raw\xff').startswith('raw')
    assert _as_text('already text') == 'already text'


def test_sudo_cmd_never_prompts(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'run_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append((args, errs, input_str)))
    sudo_cmd(['mount', '/dev/mapper/x', '/mnt'], ['acc'], 'pw')
    assert seen == [(['sudo', '-n', 'mount', '/dev/mapper/x', '/mnt'], ['acc'], 'pw')]


def test_sudo_run_never_prompts(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'run_cmd_capture',
                        lambda args, input_str=None, timeout=None:
                        seen.append((args, input_str)) or (0, '', ''))
    sudo_run(['losetup', '-f', '--show', '/v'], 'pw')
    assert seen == [(['sudo', '-n', 'losetup', '-f', '--show', '/v'], 'pw')]


# --- ensure_dir ------------------------------------------------------------


def test_ensure_dir_is_a_no_op_for_an_existing_directory(tmp_path):
    assert ensure_dir(str(tmp_path)) is None


def test_ensure_dir_creates_the_whole_tree(tmp_path):
    target = tmp_path / 'a' / 'b' / 'c'
    assert ensure_dir(str(target)) is None
    assert target.is_dir()


def test_ensure_dir_falls_back_to_sudo_when_the_user_cannot_create_it(
        tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError('read-only parent')

    monkeypatch.setattr(os, 'makedirs', denied)
    monkeypatch.setattr(LuksOps, 'sudo_cmd', lambda args: f'sudo {" ".join(args)}')
    assert ensure_dir(str(tmp_path / 'below')) == 'sudo mkdir -p ' + str(tmp_path / 'below')


def test_ensure_dir_reports_any_other_os_error(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError('bad path component')

    monkeypatch.setattr(os, 'makedirs', broken)
    message = ensure_dir(str(tmp_path / 'x'))
    assert message.startswith('FAIL: cannot create')


# --- unmount ---------------------------------------------------------------


class FakeMessageBox:
    """Stands in for QMessageBox so a popup cannot block a test run."""

    shown = []

    class Icon:
        Warning = 'warning'

    class StandardButton:
        Ok = 'ok'

    def setIcon(self, _icon):
        pass

    def setWindowTitle(self, title):
        pass

    def setText(self, text):
        FakeMessageBox.shown.append(text)

    def setStandardButtons(self, _buttons):
        pass

    def exec(self):
        FakeMessageBox.shown.append('exec')


@pytest.fixture(autouse=True)
def no_popups():
    FakeMessageBox.shown = []
    yield FakeMessageBox.shown


def _umount_result(rc, stdout='', stderr=''):
    return subprocess.CompletedProcess(['sudo', 'umount'], rc, stdout, stderr)


def test_run_unmount_succeeds_silently(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _umount_result(0))
    busy = set()
    assert run_unmount('/mnt/x', busy) is None
    assert busy == set()


def test_run_unmount_reports_a_non_busy_failure_without_a_popup(
        monkeypatch, no_popups):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _umount_result(
        32, '', 'umount: /mnt/x: not mounted.'))
    busy = set()
    err = run_unmount('/mnt/x', busy)
    assert err.startswith('FAIL: umount /mnt/x:')
    assert no_popups == []
    assert busy == set()


def test_run_unmount_explains_a_busy_mount_once(monkeypatch, no_popups):
    monkeypatch.setattr(LuksOps, 'QMessageBox', FakeMessageBox)
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[1] == 'umount':
            return _umount_result(32, '', 'umount: /mnt/x: target is busy.')
        # The shape `fuser -vm` actually prints: a header, one line naming the
        # mount point, then one line per holding process.
        return _umount_result(0, '', '                     USER        PID '
                                    'ACCESS COMMAND\n'
                                    '/mnt/x:              root     kernel '
                                    'mount /mnt/x\n'
                                    '                     joe       4242 '
                                    '..c.. bash\n')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    busy = set()
    err = run_unmount('/mnt/x', busy)
    assert 'busy' in err
    assert busy == {'/mnt/x'}
    # The holding process is named (fuser pads its columns, so only the pid is
    # worth matching on), and the mount line is not offered as an "app to close".
    assert any('4242' in text for text in no_popups)
    assert not any('kernel mount' in text for text in no_popups)
    # The second attempt is not worth another popup: the warning is per mount
    # point, so the caller does not get the same dialog every refresh.
    run_unmount('/mnt/x', busy)
    assert len([text for text in no_popups if '4242' in text]) == 1


def test_run_unmount_says_so_when_fuser_fails(monkeypatch, no_popups):
    monkeypatch.setattr(LuksOps, 'QMessageBox', FakeMessageBox)

    def fake_run(args, **_kwargs):
        if args[1] == 'umount':
            return _umount_result(32, '', 'busy')
        raise OSError('fuser is not installed')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert 'busy' in run_unmount('/mnt/y', set())
    assert any('Could not get process info' in text for text in no_popups)


def test_run_unmount_says_so_when_no_user_process_is_listed(
        monkeypatch, no_popups):
    monkeypatch.setattr(LuksOps, 'QMessageBox', FakeMessageBox)

    def fake_run(args, **_kwargs):
        if args[1] == 'umount':
            return _umount_result(32, '', 'busy')
        # Only the header and the mount line: nothing a user could close.
        return _umount_result(0, '', 'USER PID ACCESS COMMAND\n/mnt/z: kernel mount\n')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    run_unmount('/mnt/z', set())
    assert any('No user-space processes' in text for text in no_popups)


def test_run_unmount_reports_a_timeout(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(['umount'], 60)

    monkeypatch.setattr(subprocess, 'run', timeout)
    err = run_unmount('/mnt/x', set())
    assert err.startswith('FAIL: umount /mnt/x: timed out after')


def test_run_unmount_reports_an_unexpected_exception(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError('no fork')

    monkeypatch.setattr(subprocess, 'run', explode)
    err = run_unmount('/mnt/x', set())
    assert err.startswith('FAIL: umount /mnt/x: Exception:')


# --- LUKS primitives -------------------------------------------------------


def test_unlock_passes_the_password_on_stdin(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append((args, input_str)) or None)
    assert _unlock_luks('/dev/sdb1', 'hunter2', 'sdb1-luks') is None
    args, input_str = seen[0]
    assert args == ['cryptsetup', 'open', '--type', 'luks',
                    '--key-file', '-', '/dev/sdb1', 'sdb1-luks']
    assert input_str == 'hunter2'


def test_unlock_can_be_readonly(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append(args) or None)
    _unlock_luks('/dev/sdb1', 'pw', 'sdb1-luks', readonly=True)
    assert '--readonly' in seen[0]


def test_unlock_of_a_tcrypt_header_asks_for_that_type_and_a_zero_pim(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append(args) or None)
    assert _unlock_luks('/vaults/a.crypt', 'pw', 'a.crypt-luks',
                        meta_type=TCRYPT) is None
    assert seen[0] == ['cryptsetup', 'open', '--type', 'tcrypt',
                       '--veracrypt-pim', '0', '--key-file', '-',
                       '/vaults/a.crypt', 'a.crypt-luks']


# --- choosing the header type ----------------------------------------------


def fake_open(monkeypatch, answers):
    """ Answer successive `cryptsetup open` runs from `{type: (rc, stderr)}`.

        Returns the list of types that were actually asked for, which is the
        whole point of these tests: which type gets a turn, and how many.
    """
    calls = []

    def fake(argv, input_str=None, timeout=None):
        kind = argv[argv.index('--type') + 1]
        calls.append(kind)
        rc, stderr = answers[kind]
        return rc, '', stderr

    monkeypatch.setattr(LuksOps, 'run_cmd_capture', fake)
    return calls


def test_unlock_container_opens_luks_first_when_the_type_is_unknown(monkeypatch):
    calls = fake_open(monkeypatch, {'luks': (0, '')})
    assert unlock_container('/vaults/a.luks', 'pw', 'a-luks') == (None, LUKS)
    assert calls == ['luks']


def test_not_a_luks_device_is_what_makes_it_try_tcrypt(monkeypatch):
    calls = fake_open(monkeypatch,
            {'luks': (1, 'Device /vaults/a.crypt is not a valid LUKS device.'),
             'tcrypt': (0, '')})
    assert unlock_container('/vaults/a.crypt', 'pw', 'a-luks') == (None, TCRYPT)
    assert calls == ['luks', 'tcrypt']


def test_a_wrong_password_is_not_retried_as_the_other_type(monkeypatch):
    calls = fake_open(monkeypatch,
            {'luks': (2, 'No key available with this passphrase.'),
             'tcrypt': (0, '')})
    err, kind = unlock_container('/vaults/a.luks', 'pw', 'a-luks')
    assert kind == LUKS
    assert err.startswith('FAIL: sudo -n cryptsetup open --type luks ')
    assert '[rc=2]' in err
    assert calls == ['luks']


def test_a_remembered_type_is_asked_for_first(monkeypatch):
    calls = fake_open(monkeypatch, {'tcrypt': (0, '')})
    assert unlock_container('/vaults/a.crypt', 'pw', 'a-luks',
                            meta_type=TCRYPT) == (None, TCRYPT)
    assert calls == ['tcrypt']


def test_an_unrecognised_meta_type_falls_back_to_luks(monkeypatch):
    calls = fake_open(monkeypatch, {'luks': (0, '')})
    assert unlock_container('/vaults/a.luks', 'pw', 'x',
                            meta_type='bitlk') == (None, LUKS)
    assert calls == ['luks']


def test_both_types_failing_reports_the_last_attempt(monkeypatch):
    calls = fake_open(monkeypatch,
            {'luks': (1, 'not a valid LUKS device'),
             'tcrypt': (2, 'No device header detected with this passphrase.')})
    err, kind = unlock_container('/vaults/random.bin', 'pw', 'x')
    assert kind == TCRYPT
    assert 'No device header detected' in err
    assert calls == ['luks', 'tcrypt']


def test_two_wrong_type_answers_end_the_attempts(monkeypatch):
    # tcrypt answering rc=1 is not the expected shape, but the loop has to
    # stop either way: the user gets the last failure, not a third attempt.
    calls = fake_open(monkeypatch, {'luks': (1, 'not a valid LUKS device'),
                                    'tcrypt': (1, 'no TCRYPT header')})
    err, kind = unlock_container('/vaults/x.bin', 'pw', 'x')
    assert kind == TCRYPT
    assert 'no TCRYPT header' in err
    assert calls == ['luks', 'tcrypt']


def test_mount_manual_mounts_readonly_when_asked(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append(args) or None)
    assert _mount_manual('tray', '/dev/mapper/x', '/mnt/x', readonly=True) is None
    assert seen == [['mount', '-o', 'ro', '/dev/mapper/x', '/mnt/x']]


def test_mount_manual_adds_bindfs_for_file_containers(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append(args) or None)
    tray = SimpleNamespace(uid=1000, gid=1000)
    _mount_manual(tray, '/dev/mapper/x', '/mnt/x', do_bindfs=True)
    assert seen[0] == ['mount', '/dev/mapper/x', '/mnt/x']
    assert seen[1] == ['bindfs', '-u', '1000', '-g', '1000', '/mnt/x', '/mnt/x']


def test_mount_manual_skips_bindfs_after_a_failed_mount(monkeypatch):
    seen = []
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, errs=None, input_str=None, timeout=None:
                        seen.append(args) or 'FAIL: no')
    tray = SimpleNamespace(uid=1000, gid=1000)
    assert _mount_manual(tray, '/dev/mapper/x', '/mnt/x', do_bindfs=True) == 'FAIL: no'
    assert len(seen) == 1


def test_setup_loop_device_names_the_container(monkeypatch):
    monkeypatch.setattr(LuksOps, 'sudo_run',
                        lambda args, input_str=None, timeout=None:
                        (0, '/dev/loop7\n', ''))
    container = SimpleNamespace(back_file='/vaults/a.luks', name='a.luks')
    err, device = _setup_loop_device(container)
    assert err is None
    assert device == '/dev/loop7'
    assert container.name == 'loop7'


def test_setup_loop_device_reports_a_failed_losetup(monkeypatch):
    monkeypatch.setattr(LuksOps, 'sudo_run',
                        lambda args, input_str=None, timeout=None:
                        (1, '', 'no free loop device'))
    container = SimpleNamespace(back_file='/vaults/a.luks', name='a.luks')
    err, device = _setup_loop_device(container)
    assert err.startswith('FAIL: losetup -f --show /vaults/a.luks:')
    assert device is None
    # Nothing is renamed when there is no loop device to point at.
    assert container.name == 'a.luks'


def test_setup_loop_device_reports_an_empty_answer(monkeypatch):
    monkeypatch.setattr(LuksOps, 'sudo_run',
                        lambda args, input_str=None, timeout=None: (0, '', ''))
    container = SimpleNamespace(back_file='/vaults/a.luks', name='a.luks')
    err, device = _setup_loop_device(container)
    assert err == 'FAIL: losetup -f --show /vaults/a.luks: no loop device returned'
    assert device is None


# --- mount_luks_container --------------------------------------------------


def test_mount_luks_container_needs_a_mount_point():
    assert mount_luks_container(None, None, 'pw', upon='') == (
        'ERR: cannot mount without a mount point')


def test_mount_luks_container_stops_when_the_mount_point_cannot_be_made(
        monkeypatch, tmp_path):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: 'FAIL: no dir')
    monkeypatch.setattr(LuksOps, '_unlock_luks',
                        lambda *a, **k: pytest.fail('must not unlock'))
    err = mount_luks_container(
        None, SimpleNamespace(name='sdb1'), 'pw', upon=str(tmp_path / 'mnt'))
    assert err == 'FAIL: no dir'


def test_mount_luks_container_mounts_a_device(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: order.append('mkdir') or None)
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda device, password, name, readonly=False, meta_type='':
                        order.append(('unlock', device, name)) or (None, LUKS))
    monkeypatch.setattr(LuksOps, '_mount_manual',
                        lambda tray, mapper, upon, do_bindfs=False, readonly=False:
                        order.append(('mount', mapper, upon)) or None)
    tray = SimpleNamespace(uid=1000, gid=1000)
    container = SimpleNamespace(name='sdb1')
    upon = str(tmp_path / 'mnt')
    assert mount_luks_container(tray, container, 'pw', upon=upon) is None
    assert order == ['mkdir', ('unlock', '/dev/sdb1', 'sdb1-luks'),
                     ('mount', '/dev/mapper/sdb1-luks', upon)]


def test_mount_luks_container_sets_up_a_loop_device_first(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, '_setup_loop_device',
                        lambda container: order.append('loop') or (None, '/dev/loop3'))
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda device, password, name, readonly=False, meta_type='':
                        order.append(('unlock', device)) or (None, LUKS))
    monkeypatch.setattr(LuksOps, '_mount_manual', lambda *a, **k: None)
    container = SimpleNamespace(name='a.luks', back_file='/vaults/a.luks')
    mount_luks_container(None, container, 'pw', upon=str(tmp_path / 'mnt'))
    assert order == ['loop', ('unlock', '/dev/loop3')]


def test_mount_luks_container_records_which_type_opened_the_file(
        monkeypatch, tmp_path):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'unlock_container', lambda *a, **k: (None, TCRYPT))
    monkeypatch.setattr(LuksOps, '_mount_manual', lambda *a, **k: None)
    container = SimpleNamespace(name='', meta_type='')
    crypt = tmp_path / 'a.crypt'
    crypt.write_bytes(b'\x00' * 200)
    mount_luks_container(None, container, 'pw', upon=str(tmp_path / 'mnt'),
                         luks_file=str(crypt))
    # This is how the dialog learns the answer: the container carries it.
    assert container.meta_type == TCRYPT


def test_mount_luks_container_asks_for_the_declared_type_first(
        monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda device, password, name, readonly=False, meta_type='':
                        seen.append(meta_type) or (None, meta_type))
    monkeypatch.setattr(LuksOps, '_mount_manual', lambda *a, **k: None)
    container = SimpleNamespace(name='', meta_type=TCRYPT)
    mount_luks_container(None, container, 'pw', upon=str(tmp_path / 'mnt'),
                         luks_file=str(tmp_path / 'a.crypt'))
    assert seen == [TCRYPT]


def test_mount_luks_container_returns_a_loop_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, '_setup_loop_device',
                        lambda container: ('FAIL: losetup', None))
    container = SimpleNamespace(name='a.luks', back_file='/vaults/a.luks')
    assert mount_luks_container(None, container, 'pw', upon=str(tmp_path)) == 'FAIL: losetup'


def test_mount_luks_container_returns_an_unlock_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda *a, **k: ('FAIL: wrong password', LUKS))
    monkeypatch.setattr(LuksOps, '_mount_manual',
                        lambda *a, **k: pytest.fail('must not mount'))
    container = SimpleNamespace(name='sdb1', meta_type='')
    assert mount_luks_container(None, container, 'pw', upon=str(tmp_path)) == (
        'FAIL: wrong password')
    assert container.meta_type == ''   # nothing opened it, so nothing to record
    assert container.meta_type == ''   # nothing opened it, so nothing to record


def test_mount_luks_container_creates_a_new_file_container(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'run_cmd',
                        lambda args, *a, **k: order.append(tuple(args)) or None)
    monkeypatch.setattr(LuksOps, 'sudo_cmd',
                        lambda args, *a, **k: order.append(tuple(args)) or None)
    monkeypatch.setattr(LuksOps, 'sudo_run',
                        lambda args, *a, **k: order.append(tuple(args)) or (0, '', ''))
    monkeypatch.setattr(LuksOps, 'unlock_container', lambda *a, **k: (None, LUKS))
    monkeypatch.setattr(LuksOps, '_mount_manual',
                        lambda tray, mapper, upon, do_bindfs=False, readonly=False:
                        order.append(('bindfs', do_bindfs)) or None)
    tray = SimpleNamespace(uid=1000, gid=1000)
    container = SimpleNamespace(name='')
    new_file = tmp_path / '.Vaults' / 'new.luks'
    mount_luks_container(tray, container, 'pw', upon=str(tmp_path / 'mnt'),
                         luks_file=str(new_file), size=10,
                         dot_vault_dir=str(tmp_path / '.Vaults'))
    names = [step[0] for step in order if isinstance(step, tuple) and step]
    assert ('touch', str(new_file)) in order
    assert ('truncate', '-s', '10M', str(new_file)) in order
    assert any(isinstance(step, tuple) and step[:2] == ('cryptsetup', 'luksFormat')
               for step in order)
    assert ('mkfs.ext4', '/dev/mapper/new.luks-luks') in order
    # A new file container is bindfs'd so the user owns what is mounted.
    assert ('bindfs', True) in order
    assert 'mkdir' in names or ('mkdir', '-p', str(tmp_path / '.Vaults')) in order


def test_mount_luks_container_reports_a_failed_format(monkeypatch, tmp_path):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'run_cmd', lambda args, *a, **k: None)
    monkeypatch.setattr(LuksOps, 'sudo_cmd', lambda args, *a, **k: None)
    monkeypatch.setattr(LuksOps, 'sudo_run',
                        lambda args, *a, **k: (1, '', 'device is too small'))
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda *a, **k: pytest.fail('must not unlock'))
    err = mount_luks_container(
        None, SimpleNamespace(name=''), 'pw', upon=str(tmp_path / 'mnt'),
        luks_file=str(tmp_path / 'new.luks'), size=1,
        dot_vault_dir=str(tmp_path))
    assert err.startswith('FAIL: cryptsetup luksFormat --type luks2')


def test_mount_luks_container_logs_and_wraps_an_unexpected_exception(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(LuksOps, 'ensure_dir', lambda _path: None)
    monkeypatch.setattr(LuksOps, 'unlock_container',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    err = mount_luks_container(None, SimpleNamespace(name='sdb1'), 'pw',
                               upon=str(tmp_path))
    assert err == 'An error occurred: boom'
    # The traceback reaches the log, which is what makes this diagnosable.
    assert 'ERROR: mount_luks_container: boom' in capsys.readouterr().out
