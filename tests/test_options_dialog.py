"""`OptionsDialog`: the four `[ui]` settings of `config.ini`.

The dialog is a typing aid for `config.ini`, so the tests are about the two
directions: what it shows (the values the tray is actually using) and what it
writes (only values `IniTool.update_config()` will accept back).  The refused
path matters most -- a bad `auto_mount_folder` must leave the file alone and
keep the dialog open with the reason, which is the failure the raw-ini route
could not report: a hand-edited typo was silently replaced by the default at
mount time.

The master password is no longer part of this form; it has its own dialog and
its own tests (see `test_master_password_dialog.py`). What is pinned here is
that this dialog has *nothing* to do with the history: it never reads its
state and never writes it.
"""

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QDialog, QWidget

from luks_tray.dialogs.Options import OptionsDialog

# Qt takes ownership of the dialog's parent widget; keep the Python wrapper.
KEEP_ALIVE = []


class FakeIniTool:
    """The parts of `IniTool` the dialog uses, with the writes recorded."""

    def __init__(self, values=None, refuse=()):
        self.values = dict(values or {
            'show_passwords_by_default': True,
            'show_anomaly_alerts': True,
            'auto_mount_folder': '~/Vaults',
            'log_viewer': '',
        })
        self.written = []
        self.refuse = list(refuse)

    def get_current_val(self, key, selector='ui'):
        # Unset settings fall back like IniTool's golden table does, so a test
        # can name only the value it cares about.
        return self.values.get(key, '')

    def set_values(self, values, selector='ui'):
        if self.refuse:
            return list(self.refuse)
        self.written.append(dict(values))
        self.values.update(values)
        return []


class ExplodingHistory:
    """A history that fails the test if this dialog so much as looks at it.

    The password half moved out of this form; a dialog that still reached for
    the history would be reading a file it has no business writing.
    """

    def __getattr__(self, name):
        raise AssertionError(f'OptionsDialog touched history.{name}')


def make_tray(qapp, values=None, refuse=(), folder_error=None):
    """A tray stand-in: only what `OptionsDialog` reaches for."""
    parent = QWidget()
    KEEP_ALIVE.append(parent)
    tray = SimpleNamespace(
        dialog_parent=parent,
        history=ExplodingHistory(),
        ini_tool=FakeIniTool(values, refuse),
        menus=[],
    )
    tray.expand_real_user = (
        lambda path: '/home/joe' + path[1:] if path.startswith('~/') else path)
    tray.validate_auto_mount_root = lambda path: folder_error
    tray.update_menu = lambda: tray.menus.append(True)
    return tray


@pytest.fixture
def dialog(qapp):
    """Build an `OptionsDialog` on a fake tray, recording errors not showing them."""
    def build(**kwargs):
        instance = OptionsDialog(make_tray(qapp, **kwargs))
        instance.errors = []
        instance.alert_errors = instance.errors.extend
        return instance
    return build


# --- what it shows ---------------------------------------------------------


def test_the_checkboxes_show_the_configured_values(dialog):
    instance = dialog(values={'show_passwords_by_default': False,
                              'show_anomaly_alerts': True,
                              'auto_mount_folder': '/mnt/vaults'})

    assert instance.inputs['show_passwords_by_default'].isChecked() is False
    assert instance.inputs['show_anomaly_alerts'].isChecked() is True


def test_the_folder_field_shows_the_configured_value(dialog):
    instance = dialog(values={'show_passwords_by_default': True,
                              'show_anomaly_alerts': True,
                              'auto_mount_folder': '/mnt/vaults'})

    assert instance.inputs['auto_mount_folder'].text() == '/mnt/vaults'


def test_the_log_viewer_field_shows_the_configured_prefix(dialog):
    # The prefix is shown verbatim: it is a command line, and expanding or
    # normalizing it here would change what the menu will run.
    instance = dialog(values={'log_viewer': 'xterm -maximized -e'})

    assert instance.inputs['log_viewer'].text() == 'xterm -maximized -e'


def test_the_form_holds_only_the_four_settings(dialog):
    """The password fields are gone, and nothing took their place."""
    instance = dialog()

    assert set(instance.inputs) == {
        'show_passwords_by_default', 'show_anomaly_alerts',
        'auto_mount_folder', 'log_viewer'}


def test_there_is_no_password_action_left_in_the_form(dialog):
    # The button row itself, not `findChildren()`: the folder field carries its
    # own "Browse…" and the count would hide a stray password button.
    instance = dialog()
    labels = [instance.button_layout.itemAt(index).widget().text()
              for index in range(instance.button_layout.count())]

    assert labels == ['Cancel', 'Save']


def test_the_dialog_never_touches_the_history(dialog):
    # `ExplodingHistory` raises on any attribute access, so building the
    # dialog and saving it is the assertion.
    instance = dialog()

    instance.save()

    assert instance.result() == QDialog.DialogCode.Accepted


# --- what it writes --------------------------------------------------------


def test_save_writes_every_setting_and_closes(dialog):
    instance = dialog()
    instance.inputs['show_anomaly_alerts'].setChecked(False)
    instance.inputs['auto_mount_folder'].setText('/mnt/vaults')
    instance.inputs['log_viewer'].setText('foot -e')

    instance.save()

    assert instance.tray.ini_tool.written == [{
        'auto_mount_folder': '/mnt/vaults',
        'log_viewer': 'foot -e',
        'show_passwords_by_default': True,
        'show_anomaly_alerts': False,
    }]
    assert instance.result() == QDialog.DialogCode.Accepted
    # The alert setting is one of the inputs of the shield icon.
    assert instance.tray.menus == [True]


def test_cancel_writes_nothing(dialog):
    instance = dialog()
    instance.inputs['show_anomaly_alerts'].setChecked(False)

    instance.cancel()

    assert instance.tray.ini_tool.written == []
    assert instance.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize('typed, expected', [
    ('', 'may not be empty'),
    ('Vaults', 'must be an absolute path'),
])
def test_a_folder_that_is_not_absolute_is_refused(dialog, typed, expected):
    instance = dialog()
    instance.inputs['auto_mount_folder'].setText(typed)

    instance.save()

    assert instance.tray.ini_tool.written == []
    assert any(expected in line for line in instance.errors)
    assert instance.result() != QDialog.DialogCode.Accepted


def test_a_log_viewer_command_that_cannot_be_parsed_is_refused(dialog):
    # The menu splits this string with shlex; an unbalanced quote there would
    # silently disable "Open Log", so it is caught while the user can still see
    # what they typed.
    instance = dialog()
    instance.inputs['log_viewer'].setText('"unclosed -e')

    instance.save()

    assert instance.tray.ini_tool.written == []
    assert any('cannot be parsed' in line for line in instance.errors)
    assert instance.result() != QDialog.DialogCode.Accepted


def test_an_empty_log_viewer_means_auto_detect(dialog):
    instance = dialog(values={'log_viewer': 'foot -e',
                              'auto_mount_folder': '~/Vaults',
                              'show_passwords_by_default': True,
                              'show_anomaly_alerts': True})
    instance.inputs['log_viewer'].setText('')

    instance.save()

    assert instance.tray.ini_tool.written == [{
        'auto_mount_folder': '~/Vaults',
        'log_viewer': '',
        'show_passwords_by_default': True,
        'show_anomaly_alerts': True,
    }]


def test_a_folder_the_policy_refuses_is_reported(dialog):
    problem = "auto_mount_folder '/etc' is a system directory"
    instance = dialog(folder_error=problem)
    instance.inputs['auto_mount_folder'].setText('/etc')

    instance.save()

    assert instance.tray.ini_tool.written == []
    assert instance.errors == [problem]
    assert instance.result() != QDialog.DialogCode.Accepted


def test_an_unchanged_folder_is_not_revalidated(dialog):
    # The app may have created the folder itself under the ambient umask, so a
    # policy complaint about the *stored* value must not block an unrelated
    # save; only a newly typed path is checked here.
    problem = ("auto_mount_folder '/home/joe/Vaults' is writable by other "
               "(shared directory)")
    instance = dialog(folder_error=problem)
    instance.inputs['show_anomaly_alerts'].setChecked(False)

    instance.save()

    assert instance.tray.ini_tool.written == [{
        'auto_mount_folder': '~/Vaults',
        'log_viewer': '',
        'show_passwords_by_default': True,
        'show_anomaly_alerts': False,
    }]
    assert instance.errors == []
    assert instance.result() == QDialog.DialogCode.Accepted


def test_a_refused_write_keeps_the_dialog_open(dialog):
    instance = dialog(refuse=['cannot write config.ini'])

    instance.save()

    assert instance.errors == ['cannot write config.ini']
    assert instance.result() != QDialog.DialogCode.Accepted
