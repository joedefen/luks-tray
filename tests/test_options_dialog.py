"""`OptionsDialog`: the `[ui]` settings, and the way to the master password.

The dialog is a typing aid for `config.ini`, so the tests are about the two
directions: what it shows (the values the tray is actually using) and what it
writes (only values `IniTool.update_config()` will accept back).  The refused
path matters most -- a bad `auto_mount_folder` must leave the file alone and
keep the dialog open with the reason, which is the failure the raw-ini route
could not report: a hand-edited typo was silently replaced by the default at
mount time.

The password half is a hand-off, not a re-implementation: the dialog opens
`MasterPasswordDialog` (which owns the three history states) and then shows the
state that came back.
"""

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QDialog, QWidget

from luks_tray.dialogs import Options
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


class FakePasswordDialog:
    """Stands in for `MasterPasswordDialog`: records the call, returns `result`."""

    def __init__(self, opened, result=True):
        self.opened = opened
        self.result = result

    def __call__(self, tray):
        self.opened.append(tray)
        return self

    def exec(self):
        return self.result


def make_tray(qapp, status='clear_text', values=None, refuse=(),
              folder_error=None):
    """A tray stand-in: only what `OptionsDialog` reaches for."""
    parent = QWidget()
    KEEP_ALIVE.append(parent)
    tray = SimpleNamespace(
        dialog_parent=parent,
        history=SimpleNamespace(status=status),
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


@pytest.mark.parametrize('status, words', [
    ('clear_text', 'clear text'),
    ('unlocked', 'unlocked'),
    ('locked', 'locked'),
])
def test_the_history_line_describes_each_state(dialog, status, words):
    assert words in dialog(status=status).history_label.text()


def test_an_unknown_history_state_is_shown_verbatim(dialog):
    # A state added later (or a stub) is reported, not hidden behind a guess.
    assert "'mystery'" in dialog(status='mystery').history_label.text()


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


def test_a_refused_write_keeps_the_dialog_open(dialog):
    instance = dialog(refuse=['cannot write config.ini'])

    instance.save()

    assert instance.errors == ['cannot write config.ini']
    assert instance.result() != QDialog.DialogCode.Accepted


# --- the master-password hand-off -----------------------------------------


def test_the_password_button_opens_the_master_password_dialog(dialog, monkeypatch):
    opened = []
    monkeypatch.setattr(Options, 'MasterPasswordDialog', FakePasswordDialog(opened))
    instance = dialog()

    instance.open_master_password()

    assert opened == [instance.tray]


def test_an_accepted_password_change_updates_the_state_and_the_menu(
        dialog, monkeypatch):
    monkeypatch.setattr(Options, 'MasterPasswordDialog', FakePasswordDialog([]))
    instance = dialog(status='clear_text')
    instance.tray.history.status = 'unlocked'

    instance.open_master_password()

    assert 'unlocked' in instance.history_label.text()
    assert instance.tray.menus == [True]


def test_a_cancelled_password_dialog_changes_nothing(dialog, monkeypatch):
    fake = FakePasswordDialog([], result=False)
    monkeypatch.setattr(Options, 'MasterPasswordDialog', fake)
    instance = dialog(status='clear_text')
    before = instance.history_label.text()

    instance.open_master_password()

    assert instance.history_label.text() == before
    assert instance.tray.menus == []
