"""`HistoryClass`: the clear-text file, the encrypted file, and the migrations.

Every one of these paths has a failure mode that loses a password silently, so
they are asserted rather than assumed:

* the clear-text save must blank the password in the *copy*, never in the live
  namespace (a regression that made every dialog prefill an empty password);
* the write must be atomic and 0600;
* a wrong master password must leave the file byte-for-byte untouched;
* a legacy unsalted file must still open, and be rewritten salted on the next
  save;
* a back_file whose identity no longer matches must be purged, so a replaced
  crypt file cannot inherit another volume's saved password -- the identity
  being the LUKS UUID, or the salt of an encrypted (TrueCrypt/VeraCrypt)
  header.
"""

import base64
import hashlib
import json
import os
import stat
import subprocess

from cryptography.fernet import Fernet

from luks_tray.ContainerType import salt_identity
from luks_tray.History import HistoryClass


def tcrypt_header(salt=b'\x11' * 64):
    """What an encrypted TrueCrypt/VeraCrypt header looks like: salt, then noise.

    Nothing in the rest of it is cleartext -- that is the whole point -- so a
    file like this is also exactly what a *wrong* guess about the format looks
    like. Only the salt can be read, and only the salt is identity.
    """
    return salt + bytes(range(32)) * 4


def blkid_says_nothing(monkeypatch):
    """ blkid's clean "no signature here" answer, which is what a TCRYPT file gets. """
    def no_signature(*_args, **_kwargs):
        raise subprocess.CalledProcessError(2, ['blkid'], output='', stderr='')
    monkeypatch.setattr(subprocess, 'run', no_signature)


def make_history(tmp_path, master_password='', name='history.json'):
    return HistoryClass(str(tmp_path / name), master_password)


def add_vital(history, uuid='1234-abcd', **values):
    vital = history.make_ns(uuid)
    vital.password = values.pop('password', 'secret')
    vital.upon = values.pop('upon', '/home/joe/Vaults/test')
    vital.back_file = values.pop('back_file', '')
    history.vitals[uuid] = vital
    history.dirty = True
    return vital


def legacy_blob(password, entries):
    """The pre-v2 on-disk form: a bare Fernet token, unsalted SHA-256 key."""
    key = base64.urlsafe_b64encode(hashlib.sha256(password.encode()).digest())
    token = Fernet(key).encrypt(json.dumps(entries).encode('utf-8'))
    return HistoryClass.ENCRYPTED_HEADER + token


# --- clear text ------------------------------------------------------------


def test_clear_text_round_trip(tmp_path):
    history = make_history(tmp_path)
    add_vital(history)
    assert history.save() is None
    assert history.status == 'clear_text'

    stored = json.loads((tmp_path / 'history.json').read_text())
    assert stored['1234-abcd']['upon'] == '/home/joe/Vaults/test'
    # The password is never written in clear text.
    assert stored['1234-abcd']['password'] == ''


def test_the_clear_text_save_does_not_touch_the_live_namespace(tmp_path):
    history = make_history(tmp_path)
    vital = add_vital(history, password='keep-me')
    history.save()
    # The file got an empty password; the in-memory entry must keep it, or every
    # dialog prefills nothing and each save looks like a change.
    assert vital.password == 'keep-me'
    assert history.vitals['1234-abcd'] is vital


def test_the_write_is_atomic_and_owner_only(tmp_path):
    history = make_history(tmp_path)
    add_vital(history)
    history.save()
    path = tmp_path / 'history.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # No temp file survives a successful write.
    assert [name for name in os.listdir(tmp_path) if name.endswith('.tmp')] == []


def test_save_skips_a_clean_history_unless_forced(tmp_path):
    history = make_history(tmp_path)
    assert history.save() is None
    assert not (tmp_path / 'history.json').exists()
    history.save(force=True)
    assert (tmp_path / 'history.json').exists()


def test_a_failed_write_is_reported_not_raised(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    add_vital(history)

    def explode(*_args, **_kwargs):
        raise OSError('disk on fire')

    monkeypatch.setattr(history, '_write_atomic', explode)
    message = history.save()
    assert message.startswith('failed saving history:')
    assert history.dirty is True  # still unsaved, so a later attempt retries


# --- encrypted -------------------------------------------------------------


def test_encrypted_round_trip_writes_a_salted_payload(tmp_path):
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history, password='volume-secret')
    assert history.save() is None
    assert history.status == 'unlocked'

    raw = (tmp_path / 'history.json').read_bytes()
    assert raw.startswith(HistoryClass.ENCRYPTED_HEADER)
    body = raw[len(HistoryClass.ENCRYPTED_HEADER):]
    assert body.startswith(f'v{HistoryClass.ENCRYPTED_VERSION}:{HistoryClass.KDF_ITERATIONS}:'.encode())
    assert b'volume-secret' not in raw
    assert b'hunter2' not in raw

    reopened = make_history(tmp_path, master_password='hunter2')
    assert reopened.restore() is True
    assert reopened.status == 'unlocked'
    assert reopened.vitals['1234-abcd'].password == 'volume-secret'


def test_a_wrong_password_locks_the_file_and_leaves_it_untouched(tmp_path):
    history = make_history(tmp_path, master_password='right')
    add_vital(history)
    history.save()
    before = (tmp_path / 'history.json').read_bytes()

    wrong = make_history(tmp_path, master_password='wrong')
    assert wrong.restore() is False
    assert wrong.status == 'locked'
    assert (tmp_path / 'history.json').read_bytes() == before


def test_an_encrypted_file_without_a_password_is_locked(tmp_path):
    history = make_history(tmp_path, master_password='right')
    add_vital(history)
    history.save()

    locked = make_history(tmp_path)
    assert locked.restore() is False
    assert locked.status == 'locked'


def test_a_clear_text_save_cannot_overwrite_an_encrypted_file(tmp_path):
    """The wipe that used to be one button away from a locked history.

    A save with no password while the file on disk is encrypted is the single
    combination in which a write destroys data nobody can read back. It has to
    refuse, whatever the caller believes about the state it is in.
    """
    history = make_history(tmp_path, master_password='right')
    add_vital(history)
    history.save()
    before = (tmp_path / 'history.json').read_bytes()

    locked = make_history(tmp_path)
    assert locked.restore() is False
    locked.dirty = True
    message = locked.save(force=True)

    assert message.startswith('refusing to save clear text')
    assert (tmp_path / 'history.json').read_bytes() == before


def test_dropping_the_encryption_has_to_be_asked_for(tmp_path):
    # What "Remove Master Password" does once the history is unlocked: the
    # caller holds the decrypted content, and the flag is what makes the
    # clear-text write a decision instead of an accident.
    history = make_history(tmp_path, master_password='right')
    add_vital(history)
    history.save()

    opened = make_history(tmp_path, master_password='right')
    assert opened.restore() is True
    opened.master_password = ''
    assert opened.save(force=True, drop_encryption=True) is None
    assert opened.status == 'clear_text'

    raw = (tmp_path / 'history.json').read_bytes()
    assert not raw.startswith(HistoryClass.ENCRYPTED_HEADER)
    stored = json.loads(raw.decode('utf-8'))
    assert stored['1234-abcd']['upon'] == '/home/joe/Vaults/test'


def test_the_read_back_checks_the_password_that_is_on_disk(tmp_path):
    # `restore()` short-cuts on an unchanged file stamp, so it cannot answer
    # "does the file I just wrote open with this password?".
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history)
    history.save()

    assert history.verify_password_on_disk() is True

    history.master_password = 'a-typo'
    assert history.verify_password_on_disk() is False


def test_a_legacy_file_opens_and_is_scheduled_for_rewrite(tmp_path):
    entries = {HistoryClass.make_ns('legacy-uuid').uuid: {
        'uuid': 'legacy-uuid', 'password': 'old-secret', 'upon': '/mnt/old',
        'back_file': '', 'when': 1}}
    (tmp_path / 'history.json').write_bytes(legacy_blob('right', entries))

    history = make_history(tmp_path, master_password='right')
    assert history.restore() is True
    assert history.status == 'unlocked'
    assert history.vitals['legacy-uuid'].password == 'old-secret'
    # Marked dirty so the next save upgrades it to the salted format.
    assert history.dirty is True


def test_the_salted_key_is_pbkdf2_and_differs_per_salt(tmp_path):
    history = make_history(tmp_path, master_password='pw')
    salt = b'0' * HistoryClass.KDF_SALT_BYTES
    derived = history._password_to_fernet_key(salt)
    expected = base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac('sha256', b'pw', salt, HistoryClass.KDF_ITERATIONS))
    assert derived == expected
    assert history._password_to_fernet_key(os.urandom(16)) != derived
    # Without a salt the legacy unsalted derivation is reproduced exactly.
    assert history._password_to_fernet_key() == base64.urlsafe_b64encode(
        hashlib.sha256(b'pw').digest())


# --- restore() states ------------------------------------------------------


def test_a_missing_file_is_re_initialised_in_clear_text(tmp_path):
    history = make_history(tmp_path)
    assert history.restore() is False
    assert history.status == 'clear_text'
    assert (tmp_path / 'history.json').exists()
    assert json.loads((tmp_path / 'history.json').read_text()) == {}


def test_a_corrupt_file_is_re_initialised(tmp_path):
    (tmp_path / 'history.json').write_text('this is not json')
    history = make_history(tmp_path)
    assert history.restore() is False
    assert history.status == 'clear_text'
    assert json.loads((tmp_path / 'history.json').read_text()) == {}


def test_json_that_is_not_an_object_loads_empty_rather_than_crashing(tmp_path):
    (tmp_path / 'history.json').write_text('["nope"]')
    history = make_history(tmp_path)
    assert history.restore() is True
    # `_json_data_to_namespaces()` reports this as 'locked', but `restore()` then
    # overwrites that with 'clear_text' for anything without an encrypted
    # header, so the file is read as an empty clear-text history -- and, because
    # nothing is marked dirty, it is left on disk exactly as it is.
    assert history.status == 'clear_text'
    assert history.vitals == {}
    assert (tmp_path / 'history.json').read_text() == '["nope"]'


def test_unreadable_file_is_re_initialised(tmp_path, monkeypatch):
    (tmp_path / 'history.json').write_text('{}')
    history = make_history(tmp_path)

    def explode(*_args, **_kwargs):
        raise IOError('permission denied')

    monkeypatch.setattr('builtins.open', explode)
    assert history.restore() is False
    assert history.status == 'clear_text'


def test_restore_is_a_no_op_while_the_mtime_is_unchanged(tmp_path):
    history = make_history(tmp_path)
    add_vital(history)
    history.save()
    fresh = make_history(tmp_path)
    assert fresh.restore() is True
    assert fresh.status == 'clear_text'
    # A second call sees an unchanged file and does not re-read it.
    fresh.vitals.clear()
    assert fresh.restore() is True
    assert fresh.vitals == {}


def test_a_removed_file_is_noticed_again(tmp_path):
    history = make_history(tmp_path)
    history.save(force=True)
    history._has_file_changed()
    os.remove(tmp_path / 'history.json')
    assert history._has_file_changed() is True


# --- parsing and purging ---------------------------------------------------


def test_uuid_less_entries_are_dropped_and_the_file_marked_dirty(tmp_path):
    (tmp_path / 'history.json').write_text(json.dumps({
        'null': {'password': 'x'},
        'None': {'password': 'x'},
        '': {'password': 'x'},
        'real-uuid': {'password': 'kept', 'upon': '/mnt/x'},
    }))
    history = make_history(tmp_path)
    history.restore()
    assert list(history.vitals) == ['real-uuid']
    assert history.vitals['real-uuid'].password == 'kept'
    assert history.dirty is True


def test_unknown_keys_in_an_entry_are_ignored(tmp_path):
    (tmp_path / 'history.json').write_text(json.dumps({
        'uuid-1': {'password': 'p', 'upon': '/mnt', 'nonsense': 42}}))
    history = make_history(tmp_path)
    history.restore()
    assert history.vitals['uuid-1'].password == 'p'
    assert not hasattr(history.vitals['uuid-1'], 'nonsense')


def test_an_entry_whose_back_file_changed_uuid_is_purged(tmp_path, monkeypatch):
    (tmp_path / 'history.json').write_text(json.dumps({
        'stale-uuid': {'back_file': '/vaults/replaced.luks'}}))
    history = make_history(tmp_path)
    monkeypatch.setattr(history, '_probe_back_file_identity',
                        lambda _path: 'other-uuid')
    history.restore()
    assert history.vitals == {}
    assert history.dirty is True


def test_an_entry_whose_back_file_matches_is_kept(tmp_path, monkeypatch):
    (tmp_path / 'history.json').write_text(json.dumps({
        'good-uuid': {'back_file': '/vaults/good.luks', 'upon': '/mnt/good'}}))
    history = make_history(tmp_path)
    monkeypatch.setattr(history, '_probe_back_file_identity',
                        lambda _path: 'good-uuid')
    history.restore()
    assert list(history.vitals) == ['good-uuid']
    assert history.upons == {'/mnt/good'}


def test_a_probe_that_could_not_answer_keeps_the_entry(tmp_path, monkeypatch):
    (tmp_path / 'history.json').write_text(json.dumps({
        'good-uuid': {'back_file': '/vaults/good.luks'}}))
    history = make_history(tmp_path)
    monkeypatch.setattr(history, '_probe_back_file_identity', lambda _path: None)
    history.restore()
    assert list(history.vitals) == ['good-uuid']
    assert history.dirty is False


def test_a_tcrypt_entry_keyed_by_its_salt_survives_restore(tmp_path, monkeypatch):
    target = tmp_path / 'legacy.crypt'
    target.write_bytes(tcrypt_header())
    key = salt_identity(str(target))
    (tmp_path / 'history.json').write_text(json.dumps({
        key: {'back_file': str(target), 'password': 'p',
              'meta_type': 'tcrypt'}}))
    blkid_says_nothing(monkeypatch)
    history = make_history(tmp_path)
    history.restore()
    assert list(history.vitals) == [key]
    assert history.vitals[key].password == 'p'
    assert history.vitals[key].meta_type == 'tcrypt'


def test_a_re_created_tcrypt_container_is_purged(tmp_path, monkeypatch):
    target = tmp_path / 'legacy.crypt'
    target.write_bytes(tcrypt_header())
    key = salt_identity(str(target))
    (tmp_path / 'history.json').write_text(json.dumps({
        key: {'back_file': str(target), 'password': 'old'}}))
    blkid_says_nothing(monkeypatch)
    first = make_history(tmp_path)
    first.restore()
    assert list(first.vitals) == [key]

    # Same path, new container: the salt is re-drawn, so neither the old key
    # nor the old password may come back.
    target.write_bytes(tcrypt_header(salt=b'\x22' * 64))
    fresh = make_history(tmp_path)
    fresh.restore()
    assert fresh.vitals == {}
    assert fresh.dirty is True


# --- the blkid probe and its cache ----------------------------------------


def test_file_stamp_reports_none_for_an_unstattable_path(tmp_path):
    history = make_history(tmp_path)
    assert history._file_stamp(str(tmp_path / 'missing')) is None
    existing = tmp_path / 'file'
    existing.write_text('x')
    assert history._file_stamp(str(existing)) == (
        existing.stat().st_mtime_ns, existing.stat().st_size)


def _completed(stdout='UUID-1\n'):
    return subprocess.CompletedProcess(['blkid'], 0, stdout, '')


def test_the_probe_caches_a_clean_answer(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'container.luks'
    target.write_bytes(b'LUKS\xba\xbe' + b'\x00' * 200)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _completed()

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert history._probe_back_file_identity(str(target)) == 'UUID-1'
    assert history._probe_back_file_identity(str(target)) == 'UUID-1'
    assert len(calls) == 1  # the second answer came from the cache


def test_the_probe_retries_when_blkid_did_not_answer(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'container.luks'
    target.write_text('x')
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired(['blkid'], 15)

    monkeypatch.setattr(subprocess, 'run', timeout)
    assert history._probe_back_file_identity(str(target)) is None
    assert history._probe_back_file_identity(str(target)) is None
    assert len(calls) == 2  # a timeout is not an answer, so nothing is cached


def test_the_probe_does_not_cache_a_complaint(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'container.luks'
    target.write_text('x')

    def denied(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            2, ['blkid'], output='', stderr='cannot open: Permission denied')

    monkeypatch.setattr(subprocess, 'run', denied)
    assert history._probe_back_file_identity(str(target)) is None
    assert history._back_file_identity_cache == {}


def test_the_probe_caches_no_signature(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'container.luks'
    target.write_text('x')
    blkid_says_nothing(monkeypatch)
    assert history._probe_back_file_identity(str(target)) == ''
    assert history._back_file_identity_cache[str(target)] == (
        history._file_stamp(str(target)), '')


def test_the_probe_identifies_an_encrypted_header_by_its_salt(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'legacy.crypt'
    target.write_bytes(tcrypt_header())
    blkid_says_nothing(monkeypatch)
    identity = history._probe_back_file_identity(str(target))
    assert identity == salt_identity(str(target))
    assert identity.startswith('salt-')
    # Remembered like any other answer, so a second look re-reads nothing.
    assert history._back_file_identity_cache[str(target)] == (
        history._file_stamp(str(target)), identity)


def test_a_missing_blkid_is_reported_and_not_cached(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    target = tmp_path / 'container.luks'
    target.write_text('x')

    def missing(*_args, **_kwargs):
        raise FileNotFoundError('blkid')

    monkeypatch.setattr(subprocess, 'run', missing)
    assert history._probe_back_file_identity(str(target)) is None
    assert history._back_file_identity_cache == {}


def test_the_probe_cache_is_bounded(tmp_path, monkeypatch):
    history = make_history(tmp_path)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _completed())
    for index in range(HistoryClass.IDENTITY_CACHE_MAX):
        target = tmp_path / f'c{index}.luks'
        target.write_text('x')
        history._probe_back_file_identity(str(target))
    assert len(history._back_file_identity_cache) == HistoryClass.IDENTITY_CACHE_MAX
    extra = tmp_path / 'extra.luks'
    extra.write_text('x')
    history._probe_back_file_identity(str(extra))
    # Full cache is dropped wholesale rather than maintained as an LRU.
    assert list(history._back_file_identity_cache) == [str(extra)]


# --- locking while the applet runs -----------------------------------------
#
# The history used to become 'locked' only at start-up, where nothing had been
# decrypted yet.  A session event can lock it *after* it was read, so these pin
# the two things that decision depends on: the decrypted entries really go, and
# the file on disk is not touched by forgetting them.


def test_lock_forgets_the_password_and_the_decrypted_entries(tmp_path):
    """A lock that kept `vitals` would keep every container password in reach."""
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history, password='volume-secret')
    assert history.save() is None
    assert history.status == 'unlocked'

    assert history.lock() is True

    assert history.status == 'locked'
    assert history.master_password == ''
    assert history.vitals == {}
    assert history.dirty is False


def test_lock_leaves_the_encrypted_file_alone(tmp_path):
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history)
    history.save()
    before = (tmp_path / 'history.json').read_bytes()

    history.lock()

    assert (tmp_path / 'history.json').read_bytes() == before
    assert history.save() is None  # nothing dirty, so nothing is attempted


def test_a_locked_history_cannot_write_clear_text(tmp_path):
    """The save() guard is what makes a lock real rather than a menu state."""
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history)
    history.save()
    history.lock()
    history.dirty = True

    message = history.save(force=True)

    assert message.startswith('refusing to save clear text')


def test_unlocking_after_a_lock_reads_the_file_again(tmp_path):
    """The file is the only copy, so a lock is recoverable and nothing else."""
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history, password='volume-secret')
    history.save()
    history.lock()

    history.master_password = 'hunter2'
    history.restore()

    assert history.status == 'unlocked'
    assert history.vitals['1234-abcd'].password == 'volume-secret'


def test_lock_is_a_no_op_for_a_clear_text_history(tmp_path):
    """There is no key to forget, and the entries are the file's own content."""
    history = make_history(tmp_path)
    add_vital(history)
    history.save()
    assert history.status == 'clear_text'

    assert history.lock() is False
    assert history.status == 'clear_text'
    assert '1234-abcd' in history.vitals


def test_lock_is_idempotent(tmp_path):
    """A screen lock sends two or three events; only the first is a change."""
    history = make_history(tmp_path, master_password='hunter2')
    add_vital(history)
    history.save()

    assert history.lock() is True
    assert history.lock() is False


# --- vitals ----------------------------------------------------------------


def test_get_vital_returns_a_fresh_namespace_for_an_unknown_uuid(tmp_path):
    history = make_history(tmp_path)
    vital = history.get_vital('who')
    assert vital.uuid == 'who'
    assert vital.password == ''
    assert vital.when == 0


def test_put_vital_stamps_the_time_and_saves(tmp_path):
    history = make_history(tmp_path)
    vital = history.make_ns('uuid-2')
    vital.password = 'pw'
    assert history.put_vital(vital) is None
    assert vital.when > 0
    assert json.loads((tmp_path / 'history.json').read_text())['uuid-2']['uuid'] == 'uuid-2'


def test_ensure_container_adds_marks_dirty_and_learns_upons(tmp_path):
    history = make_history(tmp_path)
    container = history.make_ns('new-uuid')
    container.upon = '/mnt/a'
    container.back_file = '/vaults/a.luks'
    history.ensure_container(container)
    assert history.vitals['new-uuid'].upon == '/mnt/a'
    assert history.vitals['new-uuid'].back_file == '/vaults/a.luks'
    assert history.upons == {'/mnt/a'}
    assert history.dirty is True


def test_ensure_container_updates_a_moved_mount_point(tmp_path):
    history = make_history(tmp_path)
    first = history.make_ns('uuid-3')
    first.upon = '/mnt/old'
    history.ensure_container(first)
    history.dirty = False
    moved = history.make_ns('uuid-3')
    moved.upon = '/mnt/new'
    history.ensure_container(moved)
    assert history.vitals['uuid-3'].upon == '/mnt/new'
    assert history.dirty is True


def test_ensure_container_learns_a_back_file_without_a_change_flag(tmp_path):
    history = make_history(tmp_path)
    first = history.make_ns('uuid-4')
    first.upon = '/mnt/x'
    history.ensure_container(first)
    history.dirty = False
    with_file = history.make_ns('uuid-4')
    with_file.upon = '/mnt/x'
    with_file.back_file = '/vaults/x.luks'
    history.ensure_container(with_file)
    assert history.vitals['uuid-4'].back_file == '/vaults/x.luks'
