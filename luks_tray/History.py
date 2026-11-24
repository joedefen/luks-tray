#!/usr/bin/env python3
"""Persisted container history for LUKS TRAY."""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=line-too-long,too-many-instance-attributes
# pylint: disable=too-many-return-statements

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace

from cryptography.fernet import Fernet, InvalidToken

from luks_tray.ContainerType import salt_identity
from luks_tray.Utils import PROBE_TIMEOUT, prt, prt_once


def _describe(exc):
    """Return `exc` as a non-empty phrase for a log line.

    Args:
        exc: Exception to describe.

    Returns:
        str: The exception type and message, or just the type.
    """
    if str(exc).strip():
        return f'{type(exc).__name__}: {exc}'
    if isinstance(exc, InvalidToken):
        return 'InvalidToken (wrong master password, or a damaged file)'
    return type(exc).__name__


class HistoryClass:
    """Manage the stored container passwords, mount points and backing files."""

    ENCRYPTED_HEADER = b'{{{ENCRYPTED}}}'
    ENCRYPTED_VERSION = 2
    KDF_ITERATIONS = 600_000
    KDF_SALT_BYTES = 16
    IDENTITY_CACHE_MAX = 64

    def __init__(self, path, master_password=''):
        """Load state for the history file at `path`.

        Args:
            path: History file path.
            master_password: Master password, when one is known.
        """
        self.status = None # 'clear_text', 'unlocked', 'locked'
        self.master_password = master_password
        self.path = path
        self.dirty = False
        self.vitals = {}
        self.last_mtime = None
        self.file_existed = False
        self.upons = set()
        self._back_file_identity_cache = {}

    @staticmethod
    def make_ns(uuid):
        """Return a blank vital record for `uuid`.

        Args:
            uuid: History key for the container.

        Returns:
            SimpleNamespace: The blank record.
        """
        return SimpleNamespace(
                uuid=uuid,
                password='',
                upon='',
                back_file='',
                meta_type='',
                when=0,
            )

    def _has_file_changed(self):
        """Return True when the history file changed or disappeared.

        Returns:
            bool: True when a reload is needed.
        """
        file_exists_now = os.path.exists(self.path)
        if not file_exists_now:
            return True
        if self.master_password and self.status == 'locked':
            return True
        if file_exists_now:
            current_mtime = os.path.getmtime(self.path)
            if self.last_mtime is None or self.last_mtime != current_mtime:
                self.last_mtime = current_mtime
                self.file_existed = True
                return True
        if not file_exists_now and self.file_existed:
            self.file_existed = False
            self.last_mtime = None
            return True
        return False

    def get_vital(self, uuid):
        """Return the vital record for `uuid`, creating a blank one if needed.

        Args:
            uuid: History key for the container.

        Returns:
            SimpleNamespace: The stored or new record.
        """
        vital = self.vitals.get(uuid, None)
        if not vital:
            vital = self.make_ns(uuid)
        return vital

    def put_vital(self, vital):
        """Store `vital` and save the history.

        Args:
            vital: The record to store.

        Returns:
            None | str: The save error, if any.
        """
        self.vitals[vital.uuid] = vital
        vital.when = time.time()
        return self.save(force=True)

    def ensure_container(self, container):
        """Add `container` to the history, or update its details.

        Args:
            container: Container record to check and add.
        """
        upon = container.upon
        if upon:
            self.upons.add(upon)
        uuid = container.uuid
        if uuid not in self.vitals:
            ns = self.make_ns(uuid)
            ns.uuid = uuid
            ns.upon = upon
            ns.back_file = container.back_file
            self.vitals[uuid] = ns
            self.dirty = True
        elif self.vitals[uuid].upon != upon and upon:
            self.vitals[uuid].upon = upon
            self.dirty = True
        elif self.vitals[uuid].back_file != container.back_file:
            self.vitals[uuid].back_file = container.back_file

    def _namespaces_to_json_data(self):
        """Return copies of the vitals as JSON-serializable dictionaries.

        Returns:
            dict: The vital data keyed by container identity.
        """
        entries = {}
        for uuid, vital in self.vitals.items():
            legit = dict(vars(vital))
            if not self.master_password:
                legit['password'] = ''
            entries[uuid] = legit
        return entries

    def _password_to_fernet_key(self, salt=None, iterations=None) -> bytes:
        """Derive a Fernet key from the master password.

        Args:
            salt: PBKDF2 salt; without one the legacy derivation is used.
            iterations: PBKDF2 iteration count.

        Returns:
            bytes: The URL-safe Fernet key.
        """
        password = self.master_password.encode()
        if salt is None:
            key = hashlib.sha256(password).digest()
        else:
            key = hashlib.pbkdf2_hmac('sha256', password, salt,
                    iterations or self.KDF_ITERATIONS)
        return base64.urlsafe_b64encode(key)

    def _encrypt_payload(self, json_data):
        """Encrypt `json_data` with a fresh random salt.

        Args:
            json_data: Encoded JSON payload.

        Returns:
            bytes: Version header, salt and Fernet token.
        """
        salt = os.urandom(self.KDF_SALT_BYTES)
        cipher = Fernet(self._password_to_fernet_key(salt))
        header = f'v{self.ENCRYPTED_VERSION}:{self.KDF_ITERATIONS}:'.encode()
        return header + salt + cipher.encrypt(json_data)

    def _decrypt_payload(self, payload):
        """Decrypt an encrypted payload.

        Args:
            payload: Encrypted bytes after the header.

        Returns:
            tuple: The decoded JSON text and whether the legacy KDF was used.
        """
        if payload.startswith(f'v{self.ENCRYPTED_VERSION}:'.encode()):
            _version, iterations, rest = payload.split(b':', 2)
            salt = rest[:self.KDF_SALT_BYTES]
            token = rest[self.KDF_SALT_BYTES:]
            cipher = Fernet(self._password_to_fernet_key(salt, int(iterations)))
            return cipher.decrypt(token).decode('utf-8'), False
        cipher = Fernet(self._password_to_fernet_key())
        return cipher.decrypt(payload).decode('utf-8'), True

    def _write_atomic(self, data, binary):
        """Write `data` to `self.path` atomically with mode `0600`.

        Args:
            data: Bytes or text to write.
            binary: True to write bytes, false to write UTF-8 text.
        """
        tmp_path = None
        try:
            dirname = os.path.dirname(os.path.abspath(self.path)) or '.'
            fd, tmp_path = tempfile.mkstemp(dir=dirname,
                    prefix=os.path.basename(self.path) + '.', suffix='.tmp')
            with os.fdopen(fd, 'wb' if binary else 'w',
                           encoding=None if binary else 'utf-8') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as exc:
                    prt(f'WARN: cannot remove leftover {tmp_path!r}: {exc}')

    def save(self, force=False, drop_encryption=False):
        """Save the history, encrypted when a master password is set.

        Args:
            force: Save even when the history is not marked dirty.
            drop_encryption: Allow a clear-text write over an encrypted file.

        Returns:
            None | str: The error text, or None on success.
        """
        if not self.dirty and not force:
            return None
        if not self.master_password and not drop_encryption and self.disk_is_encrypted():
            return ('refusing to save clear text over the encrypted history '
                    f'file {self.path!r}')
        try:
            entries = self._namespaces_to_json_data()
            if self.master_password:
                json_data = json.dumps(entries).encode('utf-8')
                payload = self.ENCRYPTED_HEADER + self._encrypt_payload(json_data)
                self._write_atomic(payload, binary=True)
            else:
                self._write_atomic(json.dumps(entries, indent=4), binary=False)
        except Exception as e:
            prt(f'Error saving history: {e}')
            return f'failed saving history: {e}'

        self.dirty = False
        self.last_mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
        self.file_existed = os.path.exists(self.path)
        self.status = 'unlocked' if self.master_password else 'clear_text'
        return None

    def disk_is_encrypted(self):
        """Return True when the file on disk starts with the encrypted header.

        Returns:
            bool: Whether a clear-text write would destroy readable data.
        """
        try:
            with open(self.path, 'rb') as file:
                return file.read(len(self.ENCRYPTED_HEADER)) == self.ENCRYPTED_HEADER
        except OSError:
            return False

    def verify_password_on_disk(self):
        """Return True when the file on disk decrypts with the master password.

        Returns:
            bool: Whether the file reads back successfully.
        """
        try:
            with open(self.path, 'rb') as file:
                data = file.read()
            if not data.startswith(self.ENCRYPTED_HEADER):
                return False
            decrypted, _used_legacy_kdf = self._decrypt_payload(
                data[len(self.ENCRYPTED_HEADER):])
            json.loads(decrypted)
        except Exception as exc:                        # pylint: disable=broad-except
            prt(f'Warning: cannot read back {self.path!r}: {_describe(exc)}')
            return False
        return True

    @staticmethod
    def _file_stamp(path):
        """Return `(mtime, size)` of `path`, or None when it cannot be read.

        Args:
            path: File path.

        Returns:
            tuple | None: The file stamp.
        """
        try:
            info = os.stat(path)
        except OSError as exc:
            prt_once(f'stat back_file {path}', f'note: cannot stat {path!r}: {exc}')
            return None
        return (info.st_mtime_ns, info.st_size)

    def _blkid_uuid(self, path):
        """Return the LUKS UUID of `path`, or None when the probe failed.

        Args:
            path: Container file path.

        Returns:
            str | None: The UUID, `''` for no signature, or None on failure.
        """
        try:
            result = subprocess.run(['blkid', '-o', 'value', '-s', 'UUID', path], # noqa: UP022
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, check=True, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            prt(f'blkid timed out after {PROBE_TIMEOUT}s: {path}')
            return None
        except subprocess.CalledProcessError as exce:
            complaint = (exce.stderr or '').strip()
            if complaint:
                prt(f'blkid failed for {path}: {complaint}')
                return None
            return ''
        except OSError as exce:
            prt(f'blkid could not run for {path}: {exce}')
            return None
        return result.stdout.strip()

    def _probe_back_file_identity(self, path):
        """Return the identity of the container stored in `path`.

        Args:
            path: Backing file path.

        Returns:
            str | None: The UUID or salt identity, or None when unknown.
        """
        stamp = self._file_stamp(path)
        if stamp is not None:
            cached = self._back_file_identity_cache.get(path)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        identity = self._blkid_uuid(path)
        if identity is None:
            return None
        if not identity:
            identity = salt_identity(path)
        if stamp is not None:
            if len(self._back_file_identity_cache) >= self.IDENTITY_CACHE_MAX:
                self._back_file_identity_cache.clear()
            self._back_file_identity_cache[path] = (stamp, identity)
        return identity

    def _json_data_to_namespaces(self, entries):
        """Replace the vitals with `entries`, purging stale backing files.

        Args:
            entries: Decoded history mapping.

        Returns:
            bool: True when `entries` was a mapping.
        """
        self.vitals = {}
        purges = []
        if not isinstance(entries, dict):
            self.status = 'locked'
            return False
        for uuid, entry in entries.items():
            if uuid is None or str(uuid).strip().lower() in ('', 'none', 'null'):
                self.dirty = True
                continue
            legit = vars(self.make_ns(uuid))
            for key in legit.keys():
                if key in entry:
                    legit[key] = entry[key]
            ns = SimpleNamespace(**legit)
            if ns.back_file:
                identity = self._probe_back_file_identity(ns.back_file)
                if identity is not None and identity != uuid:
                    purges.append(uuid)
            self.vitals[uuid] = ns
            if ns.upon:
                self.upons.add(ns.upon)
        for uuid in purges:
            del self.vitals[uuid]
            self.dirty = True
        return True

    def lock(self):
        """Forget the master password and the decrypted vitals.

        Returns:
            bool: True when this call locked an unlocked history.
        """
        if self.status != 'unlocked':
            return False
        self.master_password = ''
        self.vitals = {}
        self.dirty = False
        self.status = 'locked'
        return True

    def restore(self):
        """Load the history file from disk.

        Returns:
            bool: True when the history was loaded or was already current.
        """
        if not self._has_file_changed():
            return True

        def re_init_history(reason="Missing or corrupt history file."):
            """Replace the history with an empty clear-text one."""
            prt(f"Warning: {reason} Recreating an empty history.")
            self._json_data_to_namespaces({})
            self.save(force=True)
            self.status = 'clear_text'
            return False

        if not self.file_existed:
            self.status = None
            return re_init_history("History file is missing.")

        try:
            with open(self.path, 'rb') as file:
                data = file.read()
        except OSError as e:
            return re_init_history(f"Failed to read file: {e}")

        if data.startswith(self.ENCRYPTED_HEADER):
            encrypted_data = data[len(self.ENCRYPTED_HEADER):]

            if not self.master_password:
                prt("Note: History file is encrypted, but no master password was provided.")
                self.status = 'locked'
                return False

            try:
                decrypted_str, used_legacy_kdf = self._decrypt_payload(encrypted_data)
                decrypted_data = json.loads(decrypted_str)
                self._json_data_to_namespaces(decrypted_data)
                self.status = 'unlocked'
                if used_legacy_kdf:
                    self.dirty = True
                return True
            except Exception as exc:                    # pylint: disable=broad-except
                prt("Warning: History file appears encrypted, but decryption"
                    f" failed: {_describe(exc)}")
                self.status = 'locked'
                return False

        try:
            decrypted_str = data.decode('utf-8')
            decrypted_data = json.loads(decrypted_str)
            self._json_data_to_namespaces(decrypted_data)
            self.status = 'clear_text'
            return True
        except json.JSONDecodeError:
            return re_init_history("History file is present but not valid JSON (corrupt).")
        except Exception as e:
            return re_init_history(f"Error reading clear-text history file: {e}")
