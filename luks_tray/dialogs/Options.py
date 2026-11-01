#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Options dialog: the `[ui]` settings, and the way to the master password.

    `config.ini` stays the single source of truth -- this dialog is a typing aid
    for it, not a second store -- so the one thing it must get right is that
    what it writes is what [`IniTool.update_config()`](luks_tray/IniTool.py)
    accepts on the next read. The checks therefore run *before* the file is
    touched, and a refused value leaves the dialog open with the reason in a
    message box; a hand-typed typo used to be replaced silently by the default
    at mount time, which is exactly the failure a form can prevent.

    The master password keeps its own dialog
    ([`MasterPasswordDialog`](luks_tray/dialogs/MasterPassword.py)): that class
    knows the three history states and how to move between them, and it is also
    reachable from the menu's "Click to enter master password" line when the
    history is locked. Moving the menu entry here only means this dialog has to
    answer "is my history encrypted?" itself, which is what the state line under
    the fields is for.
"""
# pylint: disable=broad-exception-caught, invalid-name

import os

from PyQt6.QtWidgets import QLabel

from luks_tray.dialogs.Common import CommonDialog
from luks_tray.dialogs.MasterPassword import MasterPasswordDialog


class OptionsDialog(CommonDialog):
    """ Edit the `[ui]` settings; reach the master-password dialog.

        The three settings are the whole of `config.ini`. Two are checkboxes and
        one is a path, so `add_input_field()` covers both shapes and the dialog
        stays a thin layer over the file: `read_settings()` is the form as a
        dict, and the same dict is what `IniTool.set_values()` writes back.
    """
    #: `HistoryClass.status` in the words of this dialog. Anything else (a
    #: future state, or a stub in a test) is shown verbatim rather than hidden.
    HISTORY_STATES = {
        'clear_text': 'History: stored in clear text (no master password)',
        'unlocked': 'History: unlocked with a master password',
        'locked': 'History: locked until the master password is entered',
    }

    def __init__(self, tray):
        super().__init__(tray)
        self.set_title('Options [luks-tray]')

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

        self.history_label = QLabel(self.history_state_text())
        self.main_layout.addWidget(self.history_label)

        self.add_push_button('Master Password…', self.open_master_password)
        self.add_push_button('Cancel', self.cancel)
        self.add_push_button('Save', self.save)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def history_state_text(self):
        """ The one-line description of the history state shown in the dialog. """
        status = self.tray.history.status
        return self.HISTORY_STATES.get(status, f'History: unknown state {status!r}')

    def open_master_password(self, _=None):
        """ Open the master-password dialog on top, then show the new state.

            Only an accepted dialog can have changed anything, and the change
            reaches the menu (the "Click to enter master password" line, the
            container list and the shield icon) through `update_menu()`.
        """
        if MasterPasswordDialog(self.tray).exec():
            self.history_label.setText(self.history_state_text())
            self.tray.update_menu()

    def read_settings(self):
        """ The form as `{key: value}`, in the shape `set_values()` expects. """
        return {
            'auto_mount_folder': self.inputs['auto_mount_folder'].text().strip(),
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
        """
        errors = []
        configured = values['auto_mount_folder']
        if not configured:
            errors.append('auto-mount folder may not be empty')
            return errors
        expanded = self.tray.expand_real_user(configured)
        if not os.path.isabs(expanded):
            errors.append(f'auto-mount folder {configured!r} must be an absolute '
                          f'path, or start with ~/')
            return errors
        problem = self.tray.validate_auto_mount_root(expanded)
        if problem:
            errors.append(problem)
        return errors

    def save(self, _=None):
        """ Validate, persist, rebuild the menu and close.

            Errors come from two places -- the checks above and the write itself
            -- and both keep the dialog open: the values the user just typed are
            the only copy of their intent until the file accepts them.
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
