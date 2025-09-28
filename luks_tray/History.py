#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=line-too-long,too-many-instance-attributes
# pylint: disable=too-many-return-statements

import os
import time
import json
import subprocess
from types import SimpleNamespace
import hashlib
import base64
from cryptography.fernet import Fernet
from luks_tray.Utils import prt

class HistoryClass:
    """
    Manages the history of LUKS-encrypted volumes, including passwords and mount points.
    The history can be stored in a clear-text JSON format or an encrypted file
    using a master password.
    """
    ENCRYPTED_HEADER = b'{{{ENCRYPTED}}}'

    def __init__(self, path, master_password=''):
        self.status = None # 'clear_text', 'unlocked', 'locked'
        self.master_password = master_password
        self.path = path
        self.dirty = False
        self.vitals = {}
        self.last_mtime = None
        self.file_existed = False
        self.upons = set() # all known mounts
        # self._load_initial_state()


    def _load_initial_state(self):
        """Initial check for file existence to set up the mtime."""
        if os.path.exists(self.path):
            self.last_mtime = os.path.getmtime(self.path)
            self.file_existed = True


    @staticmethod
    def make_ns(uuid):
        """
        Creates a new SimpleNamespace object to represent a LUKS volume's vital information.

        Args:
            uuid (str): The unique identifier for the LUKS volume.
        """
        return SimpleNamespace(
                uuid=uuid, # can be full path
                password='',
                upon='', # "primary" mount only
                back_file='', # backing file if any
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
        # do not save auto-mounts by file managers or gnome-disks
        upon = container.upon
        # upon = '' if upon.startswith(('/run/', '/media/')) else upon
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
        """Converts internal vital namespaces to a JSON-serializable dictionary."""
        entries = {}
        for uuid, vital in self.vitals.items():
            legit = vars(vital)
            if not self.master_password:
                legit['password'] = '' # zap password w/o master password
            entries[uuid] = vars(vital)
        return entries

    def _password_to_fernet_key(self) -> bytes:
        """Derive a Fernet-compatible key directly from a password using SHA256."""
        # Hash the password to create a 32-byte key
        key = hashlib.sha256(self.master_password.encode()).digest()
        # Base64 encode the key to make it suitable for Fernet
        fernet_key = base64.urlsafe_b64encode(key)
        return fernet_key

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
                # Save Encrypted with Header
                cipher = Fernet(self._password_to_fernet_key())
                json_data = json.dumps(entries).encode('utf-8')
                encrypted_data = cipher.encrypt(json_data)

                # Write header and encrypted data
                with open(self.path, 'wb') as file:
                    file.write(self.ENCRYPTED_HEADER)
                    file.write(encrypted_data)
            else:
                # Save Clear-Text
                with open(self.path, 'w', encoding='utf-8') as file:
                    json.dump(entries, file, indent=4)
        except Exception as e:
            prt(f'Error saving history: {e}')
            return f'failed saving history: {e}'

        # Update state and mtime upon successful save
        self.dirty = False
        self.last_mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
        self.file_existed = os.path.exists(self.path)
        self.status = 'unlocked' if self.master_password else 'clear_text'
        return None

    def _json_data_to_namespaces(self, entries):
        """
        Converts a JSON-serializable dictionary back into internal vital namespaces.
        Also validates `back_file` entries and purges invalid ones.
        """
        def get_luks_uuid(path):
            try:
                # Run blkid on the file and capture the output
                result = subprocess.run(['blkid', '-o', 'value', '-s', 'UUID', path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, check=True)
                return result.stdout.strip()  # Return the UUID as a string
            except subprocess.CalledProcessError:
                return ''

        self.vitals = {}
        purges = []
        if not isinstance(entries, dict):
            self.status = 'locked'
            return False
        for uuid, entry in entries.items():
            legit = vars(self.make_ns(uuid))
            for key in legit.keys():
                if key in entry:
                    legit[key] = entry[key]
            ns = SimpleNamespace(**legit)
            if ns.back_file and get_luks_uuid(ns.back_file) != uuid:
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
                cipher = Fernet(self._password_to_fernet_key())
                decrypted_str = cipher.decrypt(encrypted_data).decode('utf-8')
                decrypted_data = json.loads(decrypted_str)
                self._json_data_to_namespaces(decrypted_data)

                # State 3: Successfully Decrypted (unlocked)
                self.status = 'unlocked'
                return True
            except Exception:
                # Decryption failed (wrong password or corrupt encrypted data)
                prt("Warning: History file appears encrypted, but decryption failed.")
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
