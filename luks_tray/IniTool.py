#!/usr/bin/env python3
"""Configuration and path discovery for LUKS TRAY."""
# pylint: disable=invalid-name, broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=consider-using-from-import

import configparser
import contextlib
import copy
import json
import os
import pwd
import tempfile
from types import SimpleNamespace

from luks_tray.Utils import LOG_OLD_SUFFIX, prt


class IniTool:
    """Read, validate and write `~/.config/luks-tray/config.ini`."""

    def __init__(self, paths_only=False):
        """Compute the config paths, creating the files unless `paths_only`.

        Args:
            paths_only: Only compute paths; do not touch the disk.
        """
        def get_user_home():
            """Return the original user's home directory, even under sudo."""
            if os.geteuid() == 0:
                original_user = os.environ.get('SUDO_USER')
                if original_user:
                    return pwd.getpwnam(original_user).pw_dir
            return os.path.expanduser("~")

        self.defaults = {
            'ui': {
                'show_passwords_by_default': True,
                'show_anomaly_alerts': True,
                'auto_mount_folder': '~/Vaults',
                # A command prefix for the menu's "Open Log" entry.
                'log_viewer': '',
            }
        }
        self.folder = os.path.join(get_user_home(), ".config/luks-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        self.log_old_path =  self.log_path + LOG_OLD_SUFFIX
        self.history_path =  os.path.join(self.folder, "history.json")
        self.lock_path =  os.path.join(self.folder, "luks-tray.lock")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.section_params = {'ui': {}, }
        self.params_by_selector = {}
        if not paths_only:
            self.ensure_ini_file()

    @staticmethod
    def get_selectors():
        """Return the configuration selectors in order.

        Returns:
            list: Selector names.
        """
        return 'ui'.split()

    def the_default(self, key, selector='ui'):
        """Return the default value for `selector.key`.

        Args:
            key: Setting name.
            selector: Configuration section.

        Returns:
            The golden default value.
        """
        return self.defaults[selector][key]

    def get_current_val(self, key, selector='ui'):
        """Return the current value for `selector.key`.

        Args:
            key: Setting name.
            selector: Configuration section.

        Returns:
            The configured value, or the default.
        """
        if selector in self.params_by_selector and hasattr(self.params_by_selector[selector], key):
            val = getattr(self.params_by_selector[selector], key)
            return val
        return self.the_default(key, selector)

    def ensure_folder(self):
        """Create the config folder if needed.

        Returns:
            str: The config folder path.
        """
        os.makedirs(self.folder, exist_ok=True)
        return self.folder

    def ensure_ini_file(self):
        """Create `config.ini` with the defaults when it is missing."""
        self.ensure_folder()
        if not os.path.exists(self.ini_path):
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)

    def update_config(self):
        """Re-read `config.ini` when its mtime changed.

        Returns:
            bool: True when the file was re-read.
        """
        def to_array(val_str):
            """Parse a JSON list or scalar into a list of positive ints.

            Args:
                val_str: Raw configuration value.

            Returns:
                list | None: The parsed values, or None when unusable.
            """
            try:
                vals = json.loads(val_str)
            except (TypeError, ValueError) as exc:
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
            if len(rvs) == 1:
                rvs.append(vals[0])
            return rvs

        current_mod_time = os.path.getmtime(self.ini_path)
        if current_mod_time == self.last_mod_time:
            return False
        self.config.read(self.ini_path)
        self.last_mod_time = current_mod_time

        goldens = self.defaults['ui']
        running = goldens
        all_params = {}

        for selector in self.get_selectors():
            all_params[selector] = params = copy.deepcopy(running)
            if selector not in self.config:
                all_params[selector] = SimpleNamespace(**params)
                continue

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

                params[key] = self.the_default(key, selector)
                prt(f'skip {selector}.{key}: {value!r} [unhandled type]')
            all_params[selector] = SimpleNamespace(**params)

        self.params_by_selector = all_params

        return True

    @staticmethod
    def coerce_value(default, value):
        """Convert `value` to the type of `default`.

        Args:
            default: Golden default whose type is wanted.
            value: Value to convert.

        Returns:
            The converted value.

        Raises:
            ValueError: When `value` cannot be converted.
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
        """Persist `values` into `config.ini`.

        Args:
            values: Mapping of setting names to new values.
            selector: Configuration section.

        Returns:
            list: Error lines; empty when the write succeeded.
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
        self.config.read(self.ini_path)
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
            tmp_path = None
        except OSError as exc:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            return [f'cannot write {self.ini_path!r}: {exc}']

        self.last_mod_time = None
        self.update_config()
        return []
