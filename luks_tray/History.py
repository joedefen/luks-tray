#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" The persisted container history for LUKS TRAY.

    One "vital" per container: its UUID, mount point, backing file and the
    password that unlocks it. It is stored as JSON at the path `IniTool`
    computes, either in clear text, encrypted with Fernet/PBKDF2 under a
    master password, or unreadable until that password is supplied -- which is
    what `status` reports ('clear_text', 'unlocked', 'locked').

    Writes are atomic (temp file + rename) and 0600. On restore the entries are
    re-checked against the system: a file container whose identity no longer
    matches the file on disk is dropped, so a replaced or deleted crypt file
    cannot silently inherit another volume's saved password. "Identity" is the
    LUKS UUID, or the salt of a TrueCrypt/VeraCrypt header, which has no UUID
    anything can read (see ContainerType.py).
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=line-too-long,too-many-instance-attributes
# pylint: disable=too-many-return-statements

import os
import time
import json
import subprocess
import tempfile
from types import SimpleNamespace
import hashlib
import base64
from cryptography.fernet import Fernet
from luks_tray.ContainerType import salt_identity
from luks_tray.Utils import prt, prt_once, PROBE_TIMEOUT

class HistoryClass:
    """
    Manages the history of LUKS-encrypted volumes, including passwords and mount points.
    The history can be stored in a clear-text JSON format or an encrypted file
    using a master password.
    """
    ENCRYPTED_HEADER = b'{{{ENCRYPTED}}}'
    # What follows the header is self-describing:
    #     b'v2:<iterations>:' + <salt> + <Fernet token>
    # Files written by older versions carry a bare Fernet token keyed by an
    # unsalted single SHA-256 of the password; those are still readable, and
    # they are re-written in the v2 format on the next save. The salt lives
    # *inside* the existing header, so an older luks-tray opening a v2 file
    # fails to decrypt and reports "locked" instead of treating the file as
    # corrupt and re-initialising it.
    ENCRYPTED_VERSION = 2
    KDF_ITERATIONS = 600_000  # OWASP-style floor for PBKDF2-HMAC-SHA256
    KDF_SALT_BYTES = 16
    # Upper bound for the identity cache keyed on (path, mtime, size), see
    # _probe_back_file_identity(). One entry per backing file the history
    # refers to is the normal occupancy; the cap only stops a long-lived tray
    # from accumulating paths that were renamed or deleted.
    IDENTITY_CACHE_MAX = 64

    def __init__(self, path, master_password=''):
        self.status = None # 'clear_text', 'unlocked', 'locked'
        self.master_password = master_password
        self.path = path
        self.dirty = False
        self.vitals = {}
        self.last_mtime = None
        self.file_existed = False
        self.upons = set() # all known mounts
        # path -> ((mtime_ns, size), identity). Filled by
        # _probe_back_file_identity(); survives _json_data_to_namespaces() so
        # that re-parsing the history does not shell out to blkid (or re-read
        # a header salt) again for unchanged backing files.
        self._back_file_identity_cache = {}

    @staticmethod
    def make_ns(uuid):
        """
        Creates a new SimpleNamespace object to represent a LUKS volume's vital information.

        Args:
            uuid (str): The unique identifier for the volume -- the LUKS UUID,
                or the salt identity of a TrueCrypt/VeraCrypt file.

        `meta_type` is '' until a container has been opened once: the header
        type cannot be read out of a file, only asked for (see ContainerType.py
        and `LuksOps.unlock_container()`), and the answer is remembered here so
        that the next unlock asks for the right one first.
        """
        return SimpleNamespace(
                uuid=uuid, # can be full path
                password='',
                upon='', # "primary" mount only
                back_file='', # backing file if any
                meta_type='', # 'luks', 'tcrypt', or '' if not known yet
                when=0,  # last update
            )

    def _has_file_changed(self):
        """
        Checks if the history file has been modified or removed since the last check.

        Returns:
            bool: True if the file has changed or if it's the first check. False otherwise.
        """
        file_exists_now = os.path.exists(self.path)
        if not file_exists_now:
            return True  # Needs init
        if self.master_password and self.status == 'locked':
            return True # need to try password unlock
        if file_exists_now:
            current_mtime = os.path.getmtime(self.path)
            if self.last_mtime is None or self.last_mtime != current_mtime:
                self.last_mtime = current_mtime
                self.file_existed = True
                return True  # Changed
        if not file_exists_now and self.file_existed:
            self.file_existed = False
            self.last_mtime = None
            return True  # File was removed and needs re-init
        return False  # Unchanged


    def get_vital(self, uuid):
        """
        Retrieves the vital information for a specific LUKS volume.

        Args:
            uuid (str): The UUID of the volume.

        Returns:
            SimpleNamespace: The object containing vital info, or a new empty one if not found.
        """
        vital = self.vitals.get(uuid, None)
        if not vital: # should not happen
            vital = self.make_ns(uuid)
        return vital

    def put_vital(self, vital):
        """
        Updates the vital information for a LUKS volume and marks the history as dirty.

        Args:
            vital (SimpleNamespace): The vital object to be saved.
        """
        self.vitals[vital.uuid] = vital
        vital.when = time.time()
        return self.save(force=True)

    def ensure_container(self, container):
        """
        Ensures a discovered container exists in the history.
        Updates the container's details if a change is detected.

        Args:
            container (SimpleNamespace): The container object to check and add.
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
        """Converts internal vital namespaces to a JSON-serializable dictionary.

            Each vital is *copied* into a plain dict first. Without a master
            password the copy's password is blanked (it must never be written
            in clear text), and that blanking used to happen on
            `vital.__dict__` itself -- the live object in `self.vitals` -- so
            every clear-text save erased the remembered password. Dialogs then
            prefilled an empty password and `update_history()` compared against
            '' and considered the entry changed on every call.
        """
        entries = {}
        for uuid, vital in self.vitals.items():
            legit = dict(vars(vital))  # copy, never the live namespace
            if not self.master_password:
                legit['password'] = ''  # never persist it in clear text
            entries[uuid] = legit
        return entries

    def _password_to_fernet_key(self, salt=None, iterations=None) -> bytes:
        """Derive a Fernet-compatible key from the master password.

            With `salt`, PBKDF2-HMAC-SHA256 (>= KDF_ITERATIONS rounds) is used
            -- that is what every new/updated file gets. Without one the legacy
            derivation is reproduced (a single unsalted SHA-256 of the
            password), which exists only so older encrypted files stay
            readable.
        """
        password = self.master_password.encode()
        if salt is None:
            key = hashlib.sha256(password).digest()
        else:
            key = hashlib.pbkdf2_hmac('sha256', password, salt,
                    iterations or self.KDF_ITERATIONS)
        # Base64 encode the key to make it suitable for Fernet
        return base64.urlsafe_b64encode(key)

    def _encrypt_payload(self, json_data):
        """Encrypt `json_data` with a fresh random salt.

            Salt and iteration count travel with the payload, so the work
            factor can be raised later without breaking existing files.
        """
        salt = os.urandom(self.KDF_SALT_BYTES)
        cipher = Fernet(self._password_to_fernet_key(salt))
        header = f'v{self.ENCRYPTED_VERSION}:{self.KDF_ITERATIONS}:'.encode()
        return header + salt + cipher.encrypt(json_data)

    def _decrypt_payload(self, payload):
        """Decrypt an encrypted payload.

            Returns (json_str, used_legacy_kdf) so callers can schedule a
            rewrite of legacy files.
        """
        if payload.startswith(f'v{self.ENCRYPTED_VERSION}:'.encode()):
            _version, iterations, rest = payload.split(b':', 2)
            salt = rest[:self.KDF_SALT_BYTES]
            token = rest[self.KDF_SALT_BYTES:]
            cipher = Fernet(self._password_to_fernet_key(salt, int(iterations)))
            return cipher.decrypt(token).decode('utf-8'), False
        # Legacy: bare Fernet token keyed by the unsalted SHA-256 derivation
        cipher = Fernet(self._password_to_fernet_key())
        return cipher.decrypt(payload).decode('utf-8'), True

    def _write_atomic(self, data, binary):
        """ Write `data` to self.path atomically, owner-readable/writable only.

            A temp file is created in the *same* directory (so os.replace() is
            guaranteed to be a same-filesystem rename), written, fsync'd and
            chmod'd 0600, then swapped in. A crash or short write can therefore
            never leave a truncated or world-readable history file behind.
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
                os.fsync(file.fileno())  # flush before the rename
            os.chmod(tmp_path, 0o600)    # mkstemp's default, made explicit
            os.replace(tmp_path, self.path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as exc:
                    # A stray 0600 temp file next to the history is harmless,
                    # but it means the write failed *and* its cleanup failed.
                    prt(f'WARN: cannot remove leftover {tmp_path!r}: {exc}')

    def save(self, force=False):
        """
        Saves the history file. Encrypts with the master password if set,
        otherwise saves as plain text.
        """
        if not self.dirty and not force:
            return None
        try:
            entries = self._namespaces_to_json_data()
            if self.master_password:
                # Save Encrypted with Header + salted PBKDF2 payload
                json_data = json.dumps(entries).encode('utf-8')
                payload = self.ENCRYPTED_HEADER + self._encrypt_payload(json_data)
                self._write_atomic(payload, binary=True)
            else:
                # Save Clear-Text
                self._write_atomic(json.dumps(entries, indent=4), binary=False)
        except Exception as e:
            prt(f'Error saving history: {e}')
            return f'failed saving history: {e}'

        # Update state and mtime upon successful save
        self.dirty = False
        self.last_mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
        self.file_existed = os.path.exists(self.path)
        self.status = 'unlocked' if self.master_password else 'clear_text'
        return None

    @staticmethod
    def _file_stamp(path):
        """(mtime, size) of `path`, or None if it cannot be stat'ed.

            A cheap identity for "has this backing file changed since we last
            looked?": no content hashing, no reads, one stat call.
        """
        try:
            info = os.stat(path)
        except OSError as exc:
            # None is a usable answer (the caller then does not cache), but a
            # back_file that cannot be stat'ed is also the first sign that the
            # history entry is about to be purged -- say so once per path.
            prt_once(f'stat back_file {path}', f'note: cannot stat {path!r}: {exc}')
            return None
        return (info.st_mtime_ns, info.st_size)

    def _blkid_uuid(self, path):
        """UUID of the LUKS header in `path`, or None when blkid had no answer.

            `''` is a *clean* answer -- "there is no LUKS signature here" --
            while None means the probe itself failed (a timeout, an unreadable
            path, no `blkid` installed) and says nothing about the contents.
            The difference matters to the caller: a disk that was busy for a
            moment must not cost the user a history entry.
        """
        try:
            # The deadline matters here because this runs while the tray is
            # loading history: a back_file on hung media would otherwise stall
            # the caller for as long as the mount is wedged.
            result = subprocess.run(['blkid', '-o', 'value', '-s', 'UUID', path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, check=True, timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Unknown -- not "no UUID". Caching this would permanently purge a
            # perfectly valid entry because a disk was briefly busy.
            prt(f'blkid timed out after {PROBE_TIMEOUT}s: {path}')
            return None
        except subprocess.CalledProcessError as exce:
            complaint = (exce.stderr or '').strip()
            if complaint:
                # "cannot open ...: Permission denied", I/O errors and the
                # like: no verdict about the contents, so no caching.
                prt(f'blkid failed for {path}: {complaint}')
                return None
            # blkid exits 2 with no complaint when the file simply has no
            # signature -- a real answer, just an empty one.
            return ''
        except OSError as exce:  # blkid missing, fork failure, ...
            prt(f'blkid could not run for {path}: {exce}')
            return None
        return result.stdout.strip()  # the UUID as a string

    def _probe_back_file_identity(self, path):
        """ Identity of the container stored in `path`, None if unknown.

            There are two identities, one per header type:

            - LUKS: the UUID `blkid` reads out of the cleartext header -- the
              same value lsblk reports for the loop device;
            - TrueCrypt/VeraCrypt: the salt its encrypted header starts with,
              hashed. blkid answers "no signature" for such a file, which is
              exactly how that fallback is reached (see ContainerType.py).

            Both change when the container is re-created, which is what the
            purge check in `_json_data_to_namespaces()` needs, and neither
            needs the passphrase.

            `blkid` is an external program and this runs for every history
            entry with a back_file. `restore()` re-parses the whole history
            whenever its mtime moves -- which any mount, unmount or newly
            discovered container causes -- so an uncached probe meant one
            `blkid` per entry per re-parse, on the Qt main thread.

            The answer can only change when the file's contents change, so it
            is remembered against (mtime, size): writing, reformatting or
            truncating the file always moves at least one of them, which is
            why a stale identity cannot outlive the data it describes.

            Negative answers are cached too (a back_file that legitimately
            carries no header should stop being probed), but *only* when the
            probe answered cleanly. A timeout, an unstattable path or an
            I/O/permission complaint is not an answer: None is returned
            uncached, and the caller keeps the entry.
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
                # Rebuilt lazily, so dropping everything is cheaper than
                # maintaining an LRU: worst case one probe per live entry.
                self._back_file_identity_cache.clear()
            self._back_file_identity_cache[path] = (stamp, identity)
        return identity

    def _json_data_to_namespaces(self, entries):
        """
        Converts a JSON-serializable dictionary back into internal vital namespaces.
        Also validates `back_file` entries and purges invalid ones.
        """

        self.vitals = {}
        purges = []
        if not isinstance(entries, dict):
            self.status = 'locked'
            return False
        for uuid, entry in entries.items():
            # Earlier versions wrote uuid-less updates under the JSON key
            # "null" (uuid=None was passed to update_history). Such entries
            # carry no usable identity or back_file -- drop them on load and
            # rewrite the file.
            if uuid is None or str(uuid).strip().lower() in ('', 'none', 'null'):
                self.dirty = True
                continue
            legit = vars(self.make_ns(uuid))
            for key in legit.keys():
                if key in entry:
                    legit[key] = entry[key]
            ns = SimpleNamespace(**legit)
            if ns.back_file:
                # None means the probe could not answer; only a real, different
                # identity is a reason to forget a password.
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

# ... (inside HistoryClass)

    def restore(self):
        """
        Loads the history file from disk. It handles:
        1. Missing/Corrupt files (re-init to clear_text).
        2. Clear-text JSON (status='clear_text').
        3. Encrypted with correct password (status='unlocked').
        4. Encrypted with wrong/missing password (status='locked').
        """
        if not self._has_file_changed():
            return True  # No changes, no need to reload

        # --- Helper for Corrupt/Missing/Uninitialized ---
        def re_init_history(reason="Missing or corrupt history file."):
            """Re-initializes history to an empty clear_text state."""
            prt(f"Warning: {reason} Recreating an empty history.")
            self._json_data_to_namespaces({})
            self.save(force=True)  # This will save as clear_text if master_password is not set
            self.status = 'clear_text'
            return False

        # --- State 1: File Missing/Uninitialized ---
        if not self.file_existed:
            self.status = None # The initial state you mentioned
            return re_init_history("History file is missing.")

        # --- Read the File Content ---
        try:
            with open(self.path, 'rb') as file:
                data = file.read()
        except IOError as e:
            return re_init_history(f"Failed to read file: {e}")

        # --- Check for Encrypted Header ---
        if data.startswith(self.ENCRYPTED_HEADER):
            encrypted_data = data[len(self.ENCRYPTED_HEADER):]

            if not self.master_password:
                # State 4: Encrypted but no master_password provided (locked)
                prt("Warning: History file is encrypted, but no master password was provided.")
                self.status = 'locked'
                return False

            # Try to decrypt
            try:
                decrypted_str, used_legacy_kdf = self._decrypt_payload(encrypted_data)
                decrypted_data = json.loads(decrypted_str)
                self._json_data_to_namespaces(decrypted_data)

                # State 3: Successfully Decrypted (unlocked)
                self.status = 'unlocked'
                if used_legacy_kdf:
                    # upgrade to the salted PBKDF2 format on the next save
                    self.dirty = True
                return True
            except Exception as exc:                    # pylint: disable=broad-except
                # Decryption failed (wrong password or corrupt encrypted data).
                # The exception is part of the message because "failed" alone
                # cannot tell a typo (InvalidToken) from a truncated file.
                prt("Warning: History file appears encrypted, but decryption"
                    f" failed: {exc}")
                self.status = 'locked'
                return False

        # --- Attempt Clear-Text JSON Load ---
        else:
            try:
                decrypted_str = data.decode('utf-8')
                decrypted_data = json.loads(decrypted_str)
                self._json_data_to_namespaces(decrypted_data)

                # State 2: Valid Clear-Text JSON
                self.status = 'clear_text'
                return True
            except json.JSONDecodeError:
                # Corrupt file that's not encrypted
                return re_init_history("History file is present but not valid JSON (corrupt).")
            except Exception as e:
                # Other error (e.g., unexpected encoding)
                return re_init_history(f"Error reading clear-text history file: {e}")
