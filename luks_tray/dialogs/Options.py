#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Options dialog: the `[ui]` settings of `config.ini`, and nothing else.

    `config.ini` stays the single source of truth -- this dialog is a typing aid
    for it, not a second store -- so the one thing it must get right is that
    what it writes is what [`IniTool.update_config()`](luks_tray/IniTool.py)
    accepts on the next read. The checks therefore run *before* the file is
    touched, and a refused value leaves the dialog open with the reason in a
    message box; a hand-typed typo used to be replaced silently by the default
    at mount time, which is exactly the failure a form can prevent.

    The master password used to be the second half of this form, with its own
    status line, two masked fields and a "Remove Master Password" button. It is
    now a menu entry of its own ([`MasterPasswordSetup`](luks_tray/dialogs/MasterPassword.py)),
    because it is not a setting: it guards the whole file rather than one key in
    it, and its buttons describe a state ("nothing typed" / "a password typed
    twice") that has nothing to do with the four settings here. Keeping it out
    of this dialog also keeps Save meaning one thing.
"""
# pylint: disable=broad-exception-caught, invalid-name

import os
import shlex

from luks_tray.dialogs.Common import CommonDialog


class OptionsDialog(CommonDialog):
    """ Edit the four `[ui]` settings of `config.ini`.

        The four settings are the whole file. Two are checkboxes, one is a path
        and one is a command prefix, so `add_input_field()` covers every shape
        and the dialog stays a thin layer over `config.ini`:
        `read_settings()` is the form as a dict, and the same dict is what
        `IniTool.set_values()` writes back.
    """
    def __init__(self, tray):
        super().__init__(tray)
        self.set_title('Options')

        ini_tool = tray.ini_tool
        self.add_input_field(
            ['show_passwords_by_default', 'show_anomaly_alerts'],
            ['Show passwords by default', 'Show anomaly alerts'],
            '', field_type='checkbox')
        for key in ('show_passwords_by_default', 'show_anomaly_alerts'):
            self.inputs[key].setChecked(bool(ini_tool.get_current_val(key)))

        # The value is shown as configured (`~/Vaults`, not the expanded path):
        # the folder is relative to whichever user the tray acts for, and
        # replacing the user's `~` with an absolute home would bake in a guess
        # the moment they press Save on an unrelated setting.
        auto_root = ini_tool.get_current_val('auto_mount_folder')
        self.add_input_field('auto_mount_folder', 'Auto-mount folder', auto_root,
                             char_width=32, add_on='folder')

        # Same idea: the prefix is stored and shown verbatim (`foot -e`), and an
        # empty field means "find an installed terminal".  The whole command
        # line matters -- a bare `foot` would start a shell instead of the
        # viewer -- so the field is a command prefix, not a program name.
        log_viewer = ini_tool.get_current_val('log_viewer')
        self.add_input_field('log_viewer', 'Log viewer command', log_viewer,
                             char_width=32)

        self.add_push_button('Cancel', self.cancel)
        self.add_push_button('Save', self.save)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def read_settings(self):
        """ The form as `{key: value}`, in the shape `set_values()` expects. """
        return {
            'auto_mount_folder': self.inputs['auto_mount_folder'].text().strip(),
            'log_viewer': self.inputs['log_viewer'].text().strip(),
            'show_passwords_by_default':
                self.inputs['show_passwords_by_default'].isChecked(),
            'show_anomaly_alerts': self.inputs['show_anomaly_alerts'].isChecked(),
        }

    def validate(self, values):
        """ Return the error lines for `values`; empty when they may be saved.

            `auto_mount_folder` is the one setting with consequences: the
            start-up cleanup removes empty directories below it, so it has to
            be a dedicated, absolute, user-owned area. The expanded path is what
            the policy checks, because that is the path the cleanup will use;
            the unexpanded text is what gets saved.

            Only a *changed* folder is re-checked. The stored one has already
            been accepted, and re-validating it blocked unrelated saves (a
            checkbox, the log viewer) because of a directory the app itself had
            created under the ambient umask; the cleanup and mount paths
            validate it where it is used, and tighten a group-writable root in
            place. A newly typed value is still refused here, where the reason
            can be shown before it is written.

            `log_viewer` is split with `shlex` when the menu builds the command,
            so a value that does not parse there would silently disable the
            "Open Log" entry: refuse it here, where the reason can be shown.
        """
        errors = []
        configured = values['auto_mount_folder']
        stored = self.tray.ini_tool.get_current_val('auto_mount_folder')
        if configured != (stored.strip() if isinstance(stored, str) else stored):
            if not configured:
                return ['auto-mount folder may not be empty']
            expanded = self.tray.expand_real_user(configured)
            if not os.path.isabs(expanded):
                return [f'auto-mount folder {configured!r} must be an absolute '
                        f'path, or start with ~/']
            problem = self.tray.validate_auto_mount_root(expanded)
            if problem:
                errors.append(problem)
        viewer = values['log_viewer']
        try:
            shlex.split(viewer)
        except ValueError as exc:
            errors.append(f'log viewer command {viewer!r} cannot be parsed: {exc}')
        return errors

    def save(self, _=None):
        """ Validate, persist, rebuild the menu and close.

            Errors come from two places -- the settings checks and the write
            itself -- and both keep the dialog open: the values the user just
            typed are the only copy of their intent until the file accepts them.
        """
        values = self.read_settings()
        errors = self.validate(values)
        if not errors:
            errors = self.tray.ini_tool.set_values(values)
        if errors:
            self.alert_errors(errors)
            return
        self.tray.update_menu()     # the alert setting changes the shield icon
        self.accept()
