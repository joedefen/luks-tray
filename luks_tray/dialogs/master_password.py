#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Master-password dialog: set, update or clear the history passphrase. """
# pylint: disable=broad-exception-caught, invalid-name

from luks_tray.dialogs.common import CommonDialog


class MasterPasswordDialog(CommonDialog):
    """ Set, change or remove the passphrase that encrypts the history file.

        The history lives in one of three states ('clear_text', 'unlocked',
        'locked'); this dialog is the only place that moves between them, and
        it re-saves the file with `force=True` so the state change is on disk
        before the menu is rebuilt.
    """
    def __init__(self, tray):
        super().__init__(tray)
        self.set_title('Master Password Dialog [luks-tray]')
        self.add_input_field('password', "Master Password", '', 24, add_on='password')
        self.add_push_button('OK', self.set_master_password)
        self.add_push_button('Cancel', self.cancel)
        self.add_push_button('Remove Password', self.clear_master_password)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)


    def clear_master_password(self, _):
        """ Drop the master password (save the history in clear text). """
        self.set_master_password(_, force_clear=True)

    def set_master_password(self, _, force_clear=False):
        """ Apply the password field to the history and close on success.

            In the 'locked' state a non-empty password unlocks the history
            (`restore()`), while an empty one saves it in clear text. In the
            'unlocked'/'clear_text' states the new password is saved first, so
            a failed write leaves the old state intact. Errors are reported in
            a message box and the dialog stays open.
        """
        tray = self.tray
        field = self.inputs.get('password', None)
        errs = []
        password = '' if not field or force_clear else field.text().strip()
        if tray.history.status == 'locked':
            tray.history.master_password = password
            if password:
                tray.history.restore()
                if tray.history.status != 'unlocked':
                    tray.history.master_password = ''
                    errs.append(f'failed to unlock {repr(tray.history.path)}')
            else:
                err = tray.history.save(force=True)
                if err:
                    errs.append(err)
                else:
                    tray.history.status = 'clear_text'
        elif tray.history.status in ('unlocked', 'clear_text'):
            tray.history.master_password = password
            err = tray.history.save(force=True)
            if err:
                tray.history.master_password = ''
                errs.append(err)
            elif password:
                tray.history.status = 'locked'
            else:
                tray.history.status = 'clear_text'
        if errs:
            self.alert_errors(errs)
        else:
            self.accept()
