"""The two master-password dialogs: unlocking the history, and setting its key.

`MasterPasswordDialog` decrypts nothing itself -- `HistoryClass.restore()`
decides the outcome -- so what is pinned here is the wiring: an empty field is
refused, a wrong password leaves the history exactly as it was and empties the
field, and only a real unlock closes the dialog and keeps the password in
memory. The wrong-password answer is a notice that closes itself, not a box that
must be clicked away before the second attempt.

`MasterPasswordSetupDialog` is the only place a master password is written, and
the tests are about the two invariants that replace the old Save-time decision
table: the buttons describe the two fields (nothing can be submitted that would
be refused, and nothing is written unless a button was enabled for it), and the
two writes cost different things -- a change re-encrypts the file and keeps every
remembered container password, a clear stores it in clear text and forgets them.
Both are exercised against a real file, so "keeps" and "forgets" are read back
from disk rather than inferred from a fake.
"""

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QDialog, QLabel, QWidget

from luks_tray.History import HistoryClass
from luks_tray.dialogs.MasterPassword import (MasterPasswordDialog,
                                              MasterPasswordSetupDialog)

# Qt takes ownership of the dialog's parent widget; keep the Python wrapper.
KEEP_ALIVE = []

MASTER = 'correct-horse'
CONTAINER = 'vault-pass'
UUID = '1234-abcd'


class FakeIni:
    """Only `add_input_field()`'s "start with the password visible?" question."""

    def get_current_val(self, key, selector='ui'):
        return False


def make_tray(qapp, history):
    """A tray stand-in carrying `history`, and recording the menu rebuilds."""
    parent = QWidget()
    KEEP_ALIVE.append(parent)
    tray = SimpleNamespace(dialog_parent=parent, history=history,
                           ini_tool=FakeIni(), menus=[])
    tray.update_menu = lambda: tray.menus.append(True)
    return tray


def add_container(history, password=CONTAINER):
    """Put one remembered container in `history`, as if it had been mounted."""
    vital = history.make_ns(UUID)
    vital.password = password
    vital.upon = '/home/joe/Vaults/test'
    history.vitals[UUID] = vital
    history.dirty = True
    return vital


def read_back(path, password):
    """Open `path` with `password` in a fresh object: what the next run sees."""
    reader = HistoryClass(str(path), password)
    reader.restore()
    return reader


def button_labels(instance):
    """The labels of the dialog's own button row.

    Not `findChildren(QPushButton)`: the masked fields carry their own eye
    buttons, and the options form carries a "Browse…", so the question "what can
    this dialog do?" is answered by the row the dialog builds and nothing else.
    """
    return [instance.button_layout.itemAt(index).widget().text()
            for index in range(instance.button_layout.count())]


def record_notices(instance):
    """Capture `show_timed_message()` instead of running a 3 s box."""
    instance.notices = []

    def record(text, success=True, informative=''):
        instance.notices.append({'text': text, 'success': success,
                                 'informative': informative})

    instance.show_timed_message = record
    return instance


# --- unlocking a locked history --------------------------------------------


@pytest.fixture
def locked(qapp, tmp_path):
    """A tray whose history file is encrypted on disk and currently locked."""
    writer = HistoryClass(str(tmp_path / 'history.json'), MASTER)
    add_container(writer)
    assert writer.save() is None

    tray = make_tray(qapp, HistoryClass(str(tmp_path / 'history.json')))
    assert tray.history.restore() is False
    assert tray.history.status == 'locked'
    return tray


@pytest.fixture
def dialog(locked):
    """A dialog on the locked tray, recording errors instead of showing them."""
    instance = MasterPasswordDialog(locked)
    instance.errors = []
    instance.alert_errors = instance.errors.extend
    return record_notices(instance)


def test_an_empty_field_is_refused_and_nothing_is_restored(dialog, locked):
    dialog.unlock()

    assert dialog.errors == ['enter the master password']
    assert locked.history.status == 'locked'
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_a_wrong_password_is_a_notice_that_keeps_the_history_locked(
        dialog, locked):
    dialog.inputs['password'].setText('wrong-horse')

    dialog.unlock()

    assert locked.history.status == 'locked'
    assert locked.history.master_password == ''
    assert dialog.inputs['password'].text() == ''
    assert 'did not open' in dialog.notices[0]['text']
    assert dialog.notices[0]['success'] is False
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_the_right_password_unlocks_and_closes(dialog, locked):
    dialog.inputs['password'].setText(MASTER)

    dialog.unlock()

    assert locked.history.status == 'unlocked'
    assert locked.history.master_password == MASTER
    # The field is emptied on the way out; the password lives in the history.
    assert dialog.inputs['password'].text() == ''
    assert dialog.notices == []
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_the_unlock_dialog_offers_no_way_to_drop_the_password(qapp, tmp_path):
    # The buttons are "Unlock" and "Cancel" and nothing else: clearing the
    # password needs the decrypted history, which is what this state lacks.
    instance = MasterPasswordDialog(make_tray(qapp, HistoryClass(
        str(tmp_path / 'history.json'))))

    assert button_labels(instance) == ['Unlock', 'Cancel']


# --- setting the first password --------------------------------------------


@pytest.fixture
def clear_tray(qapp, tmp_path):
    """A tray whose history holds one container and no master password."""
    history = HistoryClass(str(tmp_path / 'history.json'))
    add_container(history)
    assert history.save() is None
    assert history.status == 'clear_text'
    return make_tray(qapp, history)


@pytest.fixture
def create_dialog(clear_tray):
    instance = MasterPasswordSetupDialog(clear_tray, create=True)
    instance.errors = []
    instance.alert_errors = instance.errors.extend
    return record_notices(instance)


def type_password(instance, password, repeated=None):
    """Type into the two fields, the way a user would."""
    instance.inputs['new_master_password'].setText(password)
    instance.inputs['repeat_master_password'].setText(
        password if repeated is None else repeated)


def test_the_create_dialog_offers_apply_and_cancel(create_dialog):
    # No Clear button: there is nothing to clear yet.
    assert button_labels(create_dialog) == ['Cancel', 'Apply']


def test_the_create_dialog_explains_that_there_is_no_recovery(create_dialog):
    body = ' '.join(label.text() for label in create_dialog.findChildren(QLabel)
                    if label.text())

    assert 'no recovery' in body
    assert 'delete history.json' in body


@pytest.mark.parametrize('password, repeated, usable', [
    ('', '', False),            # nothing typed
    ('abc', 'abc', False),      # shorter than the floor
    ('abcd', 'abce', False),    # the two fields disagree
    ('abcd', '', False),        # only half typed
    ('abcd', 'abcd', True),     # the one state that may be applied
])
def test_the_apply_button_describes_the_two_fields(create_dialog, password,
                                                  repeated, usable):
    type_password(create_dialog, password, repeated)

    assert create_dialog.apply_button.isEnabled() is usable


def test_the_apply_button_follows_the_typing(create_dialog):
    """The button is wired to the fields, not computed once at build time."""
    assert create_dialog.apply_button.isEnabled() is False

    type_password(create_dialog, 'abcd', 'abcde')
    assert create_dialog.apply_button.isEnabled() is False

    create_dialog.inputs['repeat_master_password'].setText('abcd')
    assert create_dialog.apply_button.isEnabled() is True

    create_dialog.inputs['new_master_password'].clear()
    assert create_dialog.apply_button.isEnabled() is False


def test_a_short_password_cannot_be_written_even_by_calling_the_handler(
        create_dialog, clear_tray):
    """The disabled button is a convenience; the handler refuses on its own."""
    type_password(create_dialog, 'abc')

    create_dialog.change_password()

    assert clear_tray.history.status == 'clear_text'
    assert clear_tray.history.master_password == ''
    assert create_dialog.errors == []
    assert create_dialog.result() != QDialog.DialogCode.Accepted


def test_setting_the_password_encrypts_the_file_and_locks_the_history(
        create_dialog, clear_tray, tmp_path):
    type_password(create_dialog, 'abcd')

    create_dialog.change_password()

    assert clear_tray.history.status == 'locked'
    assert create_dialog.result() == QDialog.DialogCode.Accepted
    # Nothing typed survives the dialog.
    assert create_dialog.inputs['new_master_password'].text() == ''
    assert create_dialog.inputs['repeat_master_password'].text() == ''

    reader = read_back(tmp_path / 'history.json', 'abcd')
    assert reader.status == 'unlocked'
    # Encrypting a clear-text history keeps the passwords it was holding: they
    # go into the payload instead of being blanked by `_namespaces_to_json_data`.
    assert reader.vitals[UUID].password == CONTAINER


def test_the_create_notice_names_the_new_state(create_dialog, clear_tray):
    type_password(create_dialog, 'abcd')

    create_dialog.change_password()

    assert 'set' in create_dialog.notices[0]['text']
    assert 'locked' in create_dialog.notices[0]['text']
    # The menu entry flips from Create to Update in the same breath.
    assert clear_tray.menus == [True]


# --- changing and clearing an existing password -----------------------------


@pytest.fixture
def unlocked_tray(qapp, tmp_path):
    """A tray whose history is encrypted with MASTER and unlocked."""
    writer = HistoryClass(str(tmp_path / 'history.json'), MASTER)
    add_container(writer)
    assert writer.save() is None

    history = HistoryClass(str(tmp_path / 'history.json'), MASTER)
    assert history.restore() is True
    assert history.status == 'unlocked'
    return make_tray(qapp, history)


@pytest.fixture
def update_dialog(unlocked_tray):
    instance = MasterPasswordSetupDialog(unlocked_tray, create=False)
    instance.errors = []
    instance.alert_errors = instance.errors.extend
    return record_notices(instance)


def test_the_update_dialog_offers_clear_beside_change_and_cancel(update_dialog):
    assert button_labels(update_dialog) == ['Clear Password', 'Cancel',
                                            'Change Password']


def test_clear_is_offered_only_while_both_fields_are_empty(update_dialog):
    # This is what replaces the old "an untouched form can never remove the
    # encryption" reasoning: an empty pair means Clear, and only Clear, is
    # possible -- it can no longer happen as a side effect of another Save.
    assert update_dialog.clear_button.isEnabled() is True
    assert update_dialog.apply_button.isEnabled() is False

    update_dialog.inputs['new_master_password'].setText('a')
    assert update_dialog.clear_button.isEnabled() is False

    update_dialog.inputs['new_master_password'].clear()
    assert update_dialog.clear_button.isEnabled() is True


def test_the_update_dialog_spells_out_what_each_button_costs(update_dialog):
    body = ' '.join(label.text() for label in update_dialog.findChildren(QLabel)
                    if label.text())

    assert 'keeps every remembered' in body
    assert 'clear text' in body
    assert 'forgotten at the next start' in body


def test_changing_the_password_keeps_the_remembered_container_passwords(
        update_dialog, unlocked_tray, tmp_path):
    type_password(update_dialog, 'new-horse')

    update_dialog.change_password()

    assert update_dialog.result() == QDialog.DialogCode.Accepted
    assert unlocked_tray.history.status == 'locked'

    reader = read_back(tmp_path / 'history.json', 'new-horse')
    assert reader.status == 'unlocked'
    assert reader.vitals[UUID].password == CONTAINER
    # The old key is gone: this is a re-key, not a second copy.
    assert read_back(tmp_path / 'history.json', MASTER).status == 'locked'


def test_clearing_stores_clear_text_and_forgets_the_container_passwords(
        update_dialog, unlocked_tray, tmp_path):
    update_dialog.clear_password()

    assert update_dialog.result() == QDialog.DialogCode.Accepted
    assert unlocked_tray.history.status == 'clear_text'
    # This session still holds the password it already had...
    assert unlocked_tray.history.vitals[UUID].password == CONTAINER

    reader = read_back(tmp_path / 'history.json', '')
    assert reader.status == 'clear_text'
    # ...but the file never held it without an encryption over it, so the next
    # start has to ask for it again. The notice says so because this is the one
    # write that loses something.
    assert reader.vitals[UUID].password == ''
    assert 'forgotten' in update_dialog.notices[0]['informative']


def test_clearing_is_impossible_with_anything_typed(update_dialog, unlocked_tray):
    """The disabled button is a convenience; the handler refuses on its own."""
    type_password(update_dialog, 'abcd')

    update_dialog.clear_password()

    assert unlocked_tray.history.status == 'unlocked'
    assert update_dialog.result() != QDialog.DialogCode.Accepted


def test_a_password_that_does_not_read_back_is_reported(qapp):
    class Unreadable:
        status = 'clear_text'
        master_password = ''
        path = '/home/joe/.config/luks-tray/history.json'

        def save(self, force=False, drop_encryption=False):
            return None

        def verify_password_on_disk(self):
            return False

    instance = MasterPasswordSetupDialog(make_tray(qapp, Unreadable()),
                                         create=True)
    instance.errors = []
    instance.alert_errors = instance.errors.extend
    record_notices(instance)
    type_password(instance, 'abcd')

    instance.change_password()

    assert any('did not read back' in line for line in instance.errors)
    assert instance.result() != QDialog.DialogCode.Accepted


def test_a_refused_write_keeps_the_password_it_had(qapp):
    class Refusing:
        status = 'unlocked'
        master_password = 'current'
        path = '/home/joe/.config/luks-tray/history.json'

        def save(self, force=False, drop_encryption=False):
            return 'failed saving history: disk on fire'

        def verify_password_on_disk(self):
            return True

    refusing = Refusing()
    instance = MasterPasswordSetupDialog(make_tray(qapp, refusing), create=False)
    instance.errors = []
    instance.alert_errors = instance.errors.extend
    record_notices(instance)
    type_password(instance, 'abcd')

    instance.change_password()

    assert instance.errors == ['failed saving history: disk on fire']
    assert refusing.master_password == 'current'
    assert refusing.status == 'unlocked'
    assert instance.result() != QDialog.DialogCode.Accepted
