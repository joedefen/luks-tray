#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=invalid-name,broad-exception-caught


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
    """ TBD """
    def __init__(self, path, master_password=''):
        self.status = None # 'clear_text', 'unlocked', 'locked'
        self.master_password = master_password
        self.path = path
        self.dirty = False
        self.vitals = {}
        self.last_mtime = None
        self.file_existed = False


    @staticmethod
    def make_ns(uuid):
        """ TBD """
        return SimpleNamespace(
                uuid=uuid, # can be full path
                password='',
                upon='', # "primary" mount only
                back_file='', # backing file if any
                when=0,  # last update
            )

    def _has_file_changed(self):
        file_exists_now = os.path.exists(self.path)
        if not file_exists_now and not self.file_existed:
            return False  # Unchanged
        if file_exists_now:
            current_mtime = os.path.getmtime(self.path)
            if self.last_mtime is None or self.last_mtime != current_mtime:
                self.last_mtime = current_mtime
                self.file_existed = True
                return True  # Changed
        if not file_exists_now and self.file_existed:
            self.file_existed = False
            self.last_mtime = None
            return True  # File was removed
        return False  # Unchanged


    def get_vital(self, uuid):
        """ Get vital one specific entry """
        vital = self.vitals.get(uuid, None)
        if not vital: # should not happen
            vital = self.make_ns(uuid)
        return vital

    def put_vital(self, vital):
        """ Put vitals """
        self.vitals[vital.uuid] = vital
        vital.when = time.time()
        self.dirty = True
        return self.save()

    def ensure_container(self, container):
        """Ensure a discovered container is in the history"""
        # do not save auto-mounts by file managers or gnome-disks
        upon = container.upon
        upon = '' if upon.startswith(('/run/', '/media/')) else upon
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
        """ TBD """
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

    def save(self):
        """Save the history file with or without a master password (encryption)."""
        if not self.dirty:
            return None
        try:
            entries = self._namespaces_to_json_data()
            if self.master_password:
                cipher = Fernet(self._password_to_fernet_key())
                encrypted_data = cipher.encrypt(json.dumps(entries).encode())
                with open(self.path, 'wb') as file:
                    file.write(encrypted_data)
            else:
                with open(self.path, 'w', encoding='utf-8') as file:
                    json.dump(entries, file, indent=4)
        except Exception as e:
            return f'failed saving history: {e}'
        self.dirty = False
        return None

    def is_encrypted_history(self, file_path):  # FIXME
        """Check if the history file is encrypted or plain text."""
        if not os.path.exists(file_path):
            return False  # No file means it's not encrypted yet.

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                json.load(file)  # Try to load as JSON
            return False  # If JSON loads, it's plain text
        except (json.JSONDecodeError, UnicodeDecodeError):
            return True  # If we can't load JSON, it's likely encrypted

    def old_restore(self): # TODO: remove
        """ TBD """
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                entries = json.load(handle)
            self.vitals = {}
            for uuid, entry in entries.items():
                self.vitals[uuid] = SimpleNamespace(**entry)

            self.dirty = False
            return True

        except Exception as e:
            prt(f'restored picks FAILED: {e}')
            return True

    def _json_data_to_namespaces(self, entries):
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
        for uuid in purges:
            del self.vitals[uuid]
            self.dirty = True
        return True

    def restore(self):  # TODO: only if unchanged
        """Load the history file, decrypting if necessary."""
        if not os.path.exists(self.path):
            self.status = 'clear_text'
            return self._json_data_to_namespaces({})

        if self.master_password:
            try:
                cipher = Fernet(self._password_to_fernet_key())
                with open(self.path, 'rb') as file:
                    encrypted_data = file.read()
                    decrypted_str = cipher.decrypt(encrypted_data).decode()
                    decrypted_data = json.loads(decrypted_str)
                    self.status = 'unlocked'
                    return self._json_data_to_namespaces(decrypted_data)
            except Exception:
                self._json_data_to_namespaces(None)
                return False
        else:
            try:
                with open(self.path, 'r', encoding='utf-8') as file:
                    decrypted_data = json.load(file)
                    self.status = 'clear_text'
                    return self._json_data_to_namespaces(decrypted_data)
            except Exception:
                self._json_data_to_namespaces(None)
                return False
