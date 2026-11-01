#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Configuration and path discovery for LUKS TRAY.

    `IniTool` owns the `~/.config/luks-tray` folder: it creates the folder and
    `config.ini` on first use, parses the file against the `defaults` "golden"
    values -- an unknown key or a value of the wrong type is skipped with a
    message instead of breaking startup -- and exposes the paths the rest of
    the program needs (the history file and the debug log).

    `update_config()` re-reads the file only when its mtime changed, so the 3 s
    refresh can afford to call it and a hand-edited config takes effect without
    a restart.

    `set_values()` is the write side, used by the Options dialog. It validates
    against the same golden table before writing anything, so the file the tray
    reads back is always one `update_config()` accepts: an edit made in the
    dialog and an edit made by hand are parsed by exactly one piece of code.

    Constructing this class changes no process-wide state. Every path it
    exposes is absolute, so there is no reason to `os.chdir()` into the config
    folder -- and doing it used to silently redefine what a *relative* path
    means for the whole process (including every child command): a relative
    `auto_mount_folder`, or a relative path typed into a dialog, resolved
    against `~/.config/luks-tray` instead of being rejected.
"""
# pylint: disable=invalid-name, broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=consider-using-from-import

import contextlib
import os
import pwd
import configparser
import copy
import json
import tempfile
from types import SimpleNamespace
from luks_tray.Utils import LOG_OLD_SUFFIX, prt

class IniTool:
    """ Configured Params for this class"""
    def __init__(self, paths_only=False):
        def get_user_home():
            """Get the original user's home directory, even when running as root"""
            if os.geteuid() == 0:  # Running as root
                # Try to get original user from sudo environment
                original_user = os.environ.get('SUDO_USER')
                if original_user:
                    return pwd.getpwnam(original_user).pw_dir

            # Fallback to current user
            return os.path.expanduser("~")

        self.defaults = {
            'ui': {
                'show_passwords_by_default': True,
                'show_anomaly_alerts': True,
                'auto_mount_folder': '~/Vaults',
                # A command prefix ('foot -e', 'xterm -maximized -e') for the
                # menu's "Open Log" entry. Empty means "pick an installed
                # terminal", which is the right answer on a full desktop but
                # not necessarily on a bare window manager.
                'log_viewer': '',
            }
        }
        self.folder = os.path.join(get_user_home(), ".config/luks-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        # The one rotation generation prt() keeps; the menu's viewer pages it
        # too, so the name lives next to the log it belongs to.
        self.log_old_path =  self.log_path + LOG_OLD_SUFFIX
        self.history_path =  os.path.join(self.folder, "history.json")
        # The single-instance lock main() holds for the life of the process.
        self.lock_path =  os.path.join(self.folder, "luks-tray.lock")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.section_params = {'ui': {}, }
        self.params_by_selector = {}
        if not paths_only:
            self.ensure_ini_file()

    @staticmethod
    def get_selectors():
        """ Returns the in right "order" """
        return 'ui'.split()

    def the_default(self, key, selector='ui'):
        """ return the default value given the selector and key """
        return self.defaults[selector][key]

    def get_current_val(self, key, selector='ui'):
        """ Expecting a list of two or more non-zero ints """
        if selector in self.params_by_selector and hasattr(self.params_by_selector[selector], key):
            val = getattr(self.params_by_selector[selector], key)
            return val
        return self.the_default(key, selector) # should not get here

    def ensure_folder(self):
        """Create the config folder if it does not exist; return its path.

        Split out of ensure_ini_file() so callers that only need the folder
        (e.g. `--follow-log`, which tails debug.log) do not have to create a
        config.ini as a side effect.
        """
        os.makedirs(self.folder, exist_ok=True)
        return self.folder

    def ensure_ini_file(self):
        """Check if the config file exists, create it if not."""
        self.ensure_folder()
        if not os.path.exists(self.ini_path):
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)

    def update_config(self):
        """ Check if the file has been modified since the last read """
        def to_array(val_str):
            """ Parse "[1,2,...]" or "20" into a list of positive ints.

                Returns None for anything that does not hold at least one
                positive int; a single value is repeated, because the callers
                (window geometry) always want two.
            """
            try:
                vals = json.loads(val_str)
            except (TypeError, ValueError) as exc:   # JSONDecodeError is a ValueError
                # None means "not an array, treat it as a scalar" -- correct,
                # but the reason belongs in the log (a bare `20` is fine, a
                # mangled `[1,2` most likely is not).
                prt(f'note: {val_str!r} is not a JSON array: {exc}')
                return None
            if isinstance(vals, int):
                vals = [vals]
            if not isinstance(vals, list):
                return None
            rvs = []
            for val in vals:
                if isinstance(val, int) and val > 0:
                    rvs.append(val)
            if not rvs:
                return None
            if len(rvs) == 1: # always want two
                rvs.append(vals[0])
            return rvs

        current_mod_time = os.path.getmtime(self.ini_path)
        if current_mod_time == self.last_mod_time:
            return False # not updated
        # Re-read the configuration file if it has changed
        self.config.read(self.ini_path)
        self.last_mod_time = current_mod_time

        goldens = self.defaults['ui']
        running = goldens
        all_params = {}

        # Access the configuration values in order
        for selector in self.get_selectors():
            all_params[selector] = params = copy.deepcopy(running)
            if selector not in self.config:
                all_params[selector] = SimpleNamespace(**params)
                continue

            # iterate the candidates
            candidates = dict(self.config[selector])
            for key, value in candidates.items():
                if key not in goldens:
                    prt(f'skip {selector}.{key}: {value!r} [unknown key]')
                    continue

                if key.endswith('_list'):
                    list_value = to_array(value)
                    if not value:
                        params[key] = self.the_default(key, selector)
                        prt(f'skip {selector}.{key}: {value!r} [bad list spec]')
                    else:
                        params[key] = list_value
                    continue

                if isinstance(goldens[key], bool):
                    if isinstance(value, str):
                        if value.lower() == 'true':
                            value = True
                        elif value.lower() == 'false':
                            value = False
                    if isinstance(value, bool):
                        params[key] = value
                    else:
                        params[key] = self.the_default(key, selector)
                        prt(f'skip {selector}.{key}: {value!r} [expecting bool]')
                    continue

                if isinstance(goldens[key], int):
                    try:
                        params[key] = int(value)
                        continue
                    except (TypeError, ValueError):
                        params[key] = self.the_default(key, selector)
                        prt(f'skip {selector}.{key}: {value!r} [expecting int repr]')
                        continue

                if isinstance(goldens[key], str):
                    if isinstance(value, str):
                        params[key] = value
                    else:
                        params[key] = self.the_default(key, selector)
                        prt(f'skip {selector}.{key}: {value!r} [expecting string]')
                    continue

                # Unknown golden type: keep the default like every other
                # mismatch above, instead of relying on `assert` -- which
                # `python -O` strips, silently skipping the key altogether.
                params[key] = self.the_default(key, selector)
                prt(f'skip {selector}.{key}: {value!r} [unhandled type]')
            all_params[selector] = SimpleNamespace(**params)

        self.params_by_selector = all_params

        return True # updated

    @staticmethod
    def coerce_value(default, value):
        """ Return `value` converted to the type of `default`.

            Raises ValueError when it cannot be converted. The type is taken
            from the *golden* default rather than from the caller: a bool field
            that arrives as the string 'false' (from a config file, or from a
            future dialog widget that hands over text) is repaired here, and a
            value that would be thrown away on the next read is refused instead
            of being written.
        """
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
                return value.strip().lower() == 'true'
            raise ValueError(f'{value!r} is not a bool')
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'{value!r} is not an int') from exc
        if isinstance(default, str):
            if isinstance(value, str):
                return value
            raise ValueError(f'{value!r} is not a string')
        raise ValueError(f'{value!r} has no golden type')

    def set_values(self, values, selector='ui'):
        """ Persist `values` (key -> value) into config.ini; return error lines.

            Only keys of the golden table are written, each coerced to the type
            of its default. Nothing at all is written when any value is refused
            -- an unknown key, or one that would not parse back -- so a
            hand-edit is never left half-applied. Settings this version does not
            know about are preserved by re-reading and re-writing the file,
            which does drop comments (the file this class generates has none).

            The write is atomic (temp file + rename), and `update_config()` runs
            afterwards so `get_current_val()` reports the new values
            immediately rather than on the next 3 s tick.
        """
        goldens = self.defaults[selector]
        errors, pending = [], {}
        for key, value in values.items():
            if key not in goldens:
                errors.append(f'{selector}.{key}: unknown setting')
                continue
            try:
                pending[key] = IniTool.coerce_value(goldens[key], value)
            except ValueError as exc:
                errors.append(f'{selector}.{key}: {exc}')
        if errors:
            return errors

        self.ensure_folder()
        self.config.read(self.ini_path)      # keep the settings we do not own
        if not self.config.has_section(selector):
            self.config.add_section(selector)
        for key, value in pending.items():
            self.config.set(selector, key, str(value))

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.folder, prefix='.config-',
                                            suffix='.ini')
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                self.config.write(out)
            os.replace(tmp_path, self.ini_path)
            tmp_path = None                  # renamed into place: nothing to clean
        except OSError as exc:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            return [f'cannot write {self.ini_path!r}: {exc}']

        self.last_mod_time = None            # the file is new, read it again
        self.update_config()
        return []
