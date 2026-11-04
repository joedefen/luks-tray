#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" The master password: opening the history, and setting the key that guards it.

    Two dialogs, because they answer two different questions and one of them is
    asked in a state where the other cannot run.

    `MasterPasswordDialog` is asked by a locked history: the file is encrypted
    and this process holds no decrypted content, so the only thing that can
    happen is an unlock. Setting, changing and clearing the password used to
    live here too, in one dialog with one button row for all of it -- which put
    a "Remove Password" button in front of a user whose history the applet could
    not read, where an empty password plus `save(force=True)` wrote `{}` over
    the encrypted file.

    `MasterPasswordSetupDialog` is asked by a history that can already be read,
    and is one dialog in two modes: 'create' when there is no password yet,
    'update' when there is. Both write through `HistoryClass.save()`, whose own
    guard refuses a clear-text write over an encrypted file -- the backstop that
    makes a wrong caller loud instead of destructive.
"""
# pylint: disable=broad-exception-caught, invalid-name

from luks_tray.dialogs.Common import CommonDialog


class MasterPasswordDialog(CommonDialog):
    """ Unlock the history with its master password.

        `restore()` decides the outcome: it sets 'unlocked' on success and
        'locked' on a wrong password, so this dialog reads the state back
        instead of decrypting anything itself. An empty field is refused rather
        than read as "forget the password": clearing the password needs the
        decrypted history in memory, which is exactly what this state does not
        have.

        The applet offers this dialog from the single menu line a locked history
        has, and `handle_options_click()` falls back to it if that menu is stale.
    """
    def __init__(self, tray):
        super().__init__(tray)
        self.set_title('Master Password')
        self.add_input_field('password', "Master Password", '', 24, add_on='password')
        self.add_line('The container list stays hidden until the history can be read.')
        self.add_push_button('Unlock', self.unlock)
        self.add_push_button('Cancel', self.cancel)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def unlock(self, _=None):
        """ Unlock with the typed password; report and stay open on failure.

            A wrong password leaves the history exactly as it was -- `restore()`
            only reads -- and the field is emptied, because the next thing the
            user does is type the password again. That answer is a notice, not a
            question: it closes on its own, and the dialog stays open for the
            second attempt.
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
    """ Set, change or clear the master password of the history.

        One dialog in two modes, because the fields are the same and only their
        meaning changes: 'create' fills an empty slot, 'update' replaces the key
        that is already there. The buttons -- not a Save button reading the
        fields at the end -- carry the state:

        - both entries empty       -> `Clear Password` (update mode only);
        - equal and long enough    -> `Apply` / `Change Password`;
        - anything else            -> only `Cancel` is enabled.

        That choice is what removes the old decision table. The dialog cannot
        submit a password it would have to refuse (too short, or the two entries
        differ), so there is no "the fields do not match" box to write and no
        path from an empty box to a write; and `Clear Password` sits behind two
        empty entries rather than being inferred from them.

        `Change` is a re-key, not a remove followed by an add: `save()` copies
        the namespaces it holds and encrypts them under the new password, so
        every remembered container password survives. `Clear` is the lossy one
        and says so in the body: `_namespaces_to_json_data()` never writes a
        container password without a master password over it, so the file
        forgets them (this session keeps using the ones it already has).

        Both writes leave the history 'locked': the password was just proved to
        be known, and asking for it again on the next interaction checks that
        the file really opens with it.
    """

    #: Shortest password the dialog will accept. Not a crackability floor --
    #: 600,000 rounds of PBKDF2 and a local file decide that -- but a typo
    #: floor: this is a password the user has to reproduce exactly, years
    #: later, with nothing to check it against.
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
        super().__init__(tray)
        self.create = create
        self.set_title('Create Master Password' if create
                       else 'Update Master Password')

        # Two masked fields, each with its own eye button: the password is never
        # read back from anywhere, so an empty pair means "nothing typed", and
        # an untouched dialog can only be closed.
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
        """ The new password and its repetition, as typed and stripped.

            Stripped because a leading or trailing space is invisible here and
            would have to be reproduced exactly at the next unlock.
        """
        return (self.inputs['new_master_password'].text().strip(),
                self.inputs['repeat_master_password'].text().strip())

    def fields_are_empty(self):
        """ True when neither field holds anything: the only state Clear allows. """
        return self.typed_password() == ('', '')

    def password_is_usable(self):
        """ True when the two entries agree and are long enough. """
        password, repeated = self.typed_password()
        return (len(password) >= self.MIN_PASSWORD_LENGTH
                and password == repeated)

    def refresh_buttons(self, _text=''):
        """ Point the buttons at the state of the two fields.

            Connected to both `textChanged` signals, so the state on screen is
            never one keystroke behind what a click would do.
        """
        usable = self.password_is_usable()
        self.apply_button.setEnabled(usable)
        if self.clear_button is not None:
            self.clear_button.setEnabled(self.fields_are_empty())

    def write_password(self, password, drop_encryption=False):
        """ Write the history with `password`; return the error lines.

            An empty `password` is only ever written with `drop_encryption`, and
            `HistoryClass.save()` refuses that combination over a file it cannot
            read -- it is the one write that can lose data, so it is asked for
            explicitly and checked twice.

            A non-empty password is written and then read back before the state
            changes: `restore()` short-cuts on an unchanged file, so the read
            back is `verify_password_on_disk()`. If that fails, the file is not
            the one the user thinks they just made, and the state is left alone.
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
        """ Re-key the history with the typed password, confirm it, and close. """
        password, _repeated = self.typed_password()
        if not self.password_is_usable():
            return
        errors = self.write_password(password)
        if errors:
            self.alert_errors(errors)
            return
        self.clear_passwords()
        self.tray.update_menu()     # the menu entry reads the new state
        self.show_timed_message(
            'The master password was set. The history is locked.' if self.create
            else 'The master password was changed. The history is locked.',
            informative='Enter it from the tray menu to see the containers '
                        'again.')
        self.accept()

    def clear_password(self, _=None):
        """ Store the history in clear text, discarding the key, and close.

            Only reachable with both fields empty, and the body of the dialog
            says what it costs: the container passwords this file remembered are
            not written without a master password over them, so they are gone at
            the next start. This session keeps using the ones it already holds.
        """
        if not self.fields_are_empty():
            return
        errors = self.write_password('', drop_encryption=True)
        if errors:
            self.alert_errors(errors)
            return
        self.tray.update_menu()     # the menu entry reads the new state
        self.show_timed_message(
            'The master password was cleared. The history is stored in clear '
            'text.',
            informative='The remembered container passwords were forgotten; '
                        'they have to be entered again after a restart.')
        self.accept()
