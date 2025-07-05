#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TBD
"""
# pylint: disable=invalid-name, broad-exception-caught
# pylint: disable=too-many-branches,too-many-statements
# pylint: disable=too-many-instance-attributes
# pylint: disable=consider-using-from-import
# pylint: disable=
# pylint: disable=

import os
import pwd
import configparser
from types import SimpleNamespace
import copy
import json
from luks_tray.Utils import prt

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
            }
        }
        self.folder = os.path.join(get_user_home(), ".config/luks-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        self.history_path =  os.path.join(self.folder, "history.json")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.section_params = {'ui': {}, }
        self.params_by_selector = {}
        if not paths_only:
            self.ensure_ini_file()
            os.chdir(self.folder)

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

#   def get_current_vals(self, selector, list_name):
#       """ Expecting a list of two or more non-zero ints """
#       if selector in self.params_by_selector and hasattr(self.params_by_selector[selector], list_name):
#           vals = getattr(self.params_by_selector[selector], list_name)
#           if isinstance(vals, list) and len(vals) >= 2:
#               return vals
#       return self.the_default(selector, list_name) # should not get here

#   def get_rotated_vals(self, selector, list_name, first):
#       """ TBD """
#       vals = self.get_current_vals(selector, list_name)
#       if first in vals:
#           while vals[0] != first:
#               vals = vals[1:] + vals[:1]
#           setattr(self.params_by_selector[selector], list_name, vals)
#       return vals

    def ensure_ini_file(self):
        """Check if the config file exists, create it if not."""
        if not os.path.exists(self.folder):
            os.makedirs(self.folder, exist_ok=True)
        if not os.path.exists(self.ini_path):
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)

    def update_config(self):
        """ Check if the file has been modified since the last read """
        def to_array(val_str):
            # Expecting string of form: "[1,2,...]" or just "20"
            try:
                vals = json.loads(val_str)
            except Exception:
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
        prt('parsing config.ini...')
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
                        params[key] = self.the_default(selector, key)
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
                        params[key] = self.the_default(selector, key)
                        prt(f'skip {selector}.{key}: {value!r} [expecting bool]')
                    continue

                if isinstance(goldens[key], int):
                    try:
                        params[key] = int(value)
                        continue
                    except Exception:
                        params[key] = self.the_default(selector, key)
                        prt(f'skip {selector}.{key}: {value!r} [expecting int repr]')
                        continue

                if isinstance(goldens[key], str):
                    if isinstance(value, str):
                        params[key] = value
                    else:
                        params[key] = self.the_default(selector, key)
                        prt(f'skip {selector}.{key}: {value!r} [expecting string]')
                    continue

                assert False, f'unhandled goldens[{key}]: {value!r}'
            all_params[selector] = SimpleNamespace(**params)

        self.params_by_selector = all_params

        prt(f'DONE parsing config.ini... {all_params=}')

        return True # updated
