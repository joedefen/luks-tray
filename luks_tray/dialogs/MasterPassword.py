#!/usr/bin/env python3
"""The master password dialogs: unlock, set, change and clear."""
# pylint: disable=broad-exception-caught, invalid-name

from luks_tray.dialogs.Common import CommonDialog


class MasterPasswordDialog(CommonDialog):
    """Unlock the history with its master password."""

    def __init__(self, tray):
        """Build the unlock dialog.

        Args:
            tray: The running tray instance.
        """
        super().__init__(tray)
        self.set_title('Master Password')
        self.add_input_field('password', "Master Password", '', 24, add_on='password')
        self.add_line('The container list stays hidden until the history can be read.')
        self.add_push_button('Unlock', self.unlock)
        self.add_push_button('Cancel', self.cancel)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def unlock(self, _=None):
        """Unlock with the typed password, or report a failure.

        Args:
            _: Optional signal argument.
        """
        history = self.tray.history
        password = self.inputs['password'].text().strip()
        if not password:
            self.alert_errors(['enter the master password'])
            return
        history.master_password = password
        history.restore()
        if history.status != 'unlocked':
            history.master_password = ''
            self.inputs['password'].clear()
            self.show_timed_message(
                'That password did not open the history.',
                success=False,
                informative=f'{history.path} did not open with that password.')
            return
        self.inputs['password'].clear()
        self.accept()


class MasterPasswordSetupDialog(CommonDialog):
    """Set, change or clear the master password of the history."""

    # Shortest password the dialog accepts; this is a typo floor, not a
    # strength requirement.
    MIN_PASSWORD_LENGTH = 4

    NO_RECOVERY = ('There is no recovery. If this password is forgotten, the '
                   'only way back is to delete history.json -- which forgets '
                   'every container the applet remembered.')
    UPDATE_COST = ('Changing re-encrypts the file and keeps every remembered '
                   'container password. Clearing stores it in clear text, '
                   'where those passwords cannot be written, so they are '
                   'forgotten at the next start.')
    LOCKS_AFTERWARDS = ('The history locks as soon as the password is set: the '
                        'container list stays hidden until it is entered again.')

    def __init__(self, tray, create=False):
        """Build the setup dialog.

        Args:
            tray: The running tray instance.
            create: True when no master password exists yet.
        """
        super().__init__(tray)
        self.create = create
        self.set_title('Create Master Password' if create
                       else 'Update Master Password')

        self.add_input_field('new_master_password', 'New master password', '',
                             24, add_on='password')
        self.add_input_field('repeat_master_password', 'Repeat new password', '',
                             24, add_on='password')

        self.add_line(self.NO_RECOVERY, wrap=True)
        if not create:
            self.add_line(self.UPDATE_COST, wrap=True)
        self.add_line(self.LOCKS_AFTERWARDS, wrap=True)

        self.clear_button = None
        if not create:
            self.clear_button = self.add_push_button('Clear Password',
                                                     self.clear_password)
        self.add_push_button('Cancel', self.cancel)
        self.apply_button = self.add_push_button(
            'Apply' if create else 'Change Password', self.change_password)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

        for key in ('new_master_password', 'repeat_master_password'):
            self.inputs[key].textChanged.connect(self.refresh_buttons)
        self.refresh_buttons()

    def typed_password(self):
        """Return the new password and its repetition, stripped.

        Returns:
            tuple: The two typed values.
        """
        return (self.inputs['new_master_password'].text().strip(),
                self.inputs['repeat_master_password'].text().strip())

    def fields_are_empty(self):
        """Return True when neither field holds anything.

        Returns:
            bool: Whether both fields are empty.
        """
        return self.typed_password() == ('', '')

    def password_is_usable(self):
        """Return True when the two entries agree and are long enough.

        Returns:
            bool: Whether the value may be submitted.
        """
        password, repeated = self.typed_password()
        return (len(password) >= self.MIN_PASSWORD_LENGTH
                and password == repeated)

    def refresh_buttons(self, _text=''):
        """Enable the buttons that match the state of the two fields.

        Args:
            _text: Optional `textChanged` argument.
        """
        usable = self.password_is_usable()
        self.apply_button.setEnabled(usable)
        if self.clear_button is not None:
            self.clear_button.setEnabled(self.fields_are_empty())

    def write_password(self, password, drop_encryption=False):
        """Write the history with `password`.

        Args:
            password: New master password; empty to clear it.
            drop_encryption: Allow a clear-text write over an encrypted file.

        Returns:
            list: Error lines; empty on success.
        """
        history = self.tray.history
        previous = history.master_password
        history.master_password = password
        error = history.save(force=True, drop_encryption=drop_encryption)
        if error:
            history.master_password = previous
            return [error]
        if password and not history.verify_password_on_disk():
            return [f'{history.path} did not read back with the new master '
                    f'password']
        history.status = 'locked' if password else 'clear_text'
        return []

    def change_password(self, _=None):
        """Re-key the history with the typed password and close.

        Args:
            _: Optional signal argument.
        """
        password, _repeated = self.typed_password()
        if not self.password_is_usable():
            return
        errors = self.write_password(password)
        if errors:
            self.alert_errors(errors)
            return
        self.clear_passwords()
        self.tray.update_menu()
        self.show_timed_message(
            'The master password was set. The history is locked.' if self.create
            else 'The master password was changed. The history is locked.',
            informative='Enter it from the tray menu to see the containers '
                        'again.')
        self.accept()

    def clear_password(self, _=None):
        """Store the history in clear text and close.

        Args:
            _: Optional signal argument.
        """
        if not self.fields_are_empty():
            return
        errors = self.write_password('', drop_encryption=True)
        if errors:
            self.alert_errors(errors)
            return
        self.tray.update_menu()
        self.show_timed_message(
            'The master password was cleared. The history is stored in clear '
            'text.',
            informative='The remembered container passwords were forgotten; '
                        'they have to be entered again after a restart.')
        self.accept()
