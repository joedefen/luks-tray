"""`CommonDialog`: the parts every dialog shares, exercised with a fake tray.

The dialogs are the whole reason the release gate has to install a real PyQt6:
the behaviour worth pinning is Qt behaviour -- window flags, the manual title
label, echo modes, the progress widgets, the mount-point policy -- and a stub
would only assert that the stub was called.  `QT_QPA_PLATFORM=offscreen` comes
from `conftest.py`, so no display is needed.

The tray is a plain stand-in: `CommonDialog` takes it as a constructor
argument, which is exactly what makes these tests possible without building a
`QSystemTrayIcon`.
"""

import os
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QLabel, QLineEdit, QProgressBar, QPushButton

from luks_tray.dialogs import Common
from luks_tray.dialogs.Common import CommonDialog, requires_manual_title


class FakeIni:
    def __init__(self, show_passwords=True):
        self.values = {'show_passwords_by_default': show_passwords}

    def get_current_val(self, key, selector='ui'):
        return self.values[key]

    def the_default(self, key, selector='ui'):
        return '~/Vaults'


class FakeTray:
    def __init__(self, auto_root, dialog_parent=None, show_passwords=True):
        self.ini_tool = FakeIni(show_passwords)
        self.dialog_parent = dialog_parent
        self.auto_root = auto_root
        self.upons = set()
        self.history = SimpleNamespace(upons=set())

    def get_auto_mount_root(self):
        return self.auto_root


@pytest.fixture
def tray(vault_root):
    return FakeTray(vault_root)


@pytest.fixture
def dialog(qapp, tray):
    """A `CommonDialog` whose layouts are installed, as a subclass does.

    `CommonDialog.__init__` builds `main_layout`/`button_layout` but leaves
    installing them to the subclass, and a widget only becomes a child of the
    dialog once its layout is installed -- which is what
    `show_progress()`'s `findChildren(QPushButton)` walks.
    """
    dlg = CommonDialog(tray)
    dlg.main_layout.addLayout(dlg.button_layout)
    dlg.setLayout(dlg.main_layout)
    return dlg


# --- the compositor probe --------------------------------------------------


def test_requires_manual_title_matches_sway(monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'sway')
    monkeypatch.delenv('DESKTOP_SESSION', raising=False)
    assert requires_manual_title() is True


def test_requires_manual_title_is_case_insensitive_and_reads_the_session(
        monkeypatch):
    monkeypatch.delenv('XDG_CURRENT_DESKTOP', raising=False)
    monkeypatch.setenv('DESKTOP_SESSION', 'Sway-custom')
    assert requires_manual_title() is True


def test_requires_manual_title_is_false_elsewhere(monkeypatch):
    # Documented limitation, not an oversight: this is a compositor *name*
    # test, so niri/Hyprland/river get the missing decoration without the
    # mitigation.  Widening it to any WAYLAND_DISPLAY would double-decorate
    # Plasma, which draws server-side decorations.
    for value in ('niri', 'Hyprland', 'KDE', 'i3'):
        monkeypatch.setenv('XDG_CURRENT_DESKTOP', value)
        monkeypatch.delenv('DESKTOP_SESSION', raising=False)
        assert requires_manual_title() is False


def test_requires_manual_title_survives_an_empty_environment(monkeypatch):
    monkeypatch.delenv('XDG_CURRENT_DESKTOP', raising=False)
    monkeypatch.delenv('DESKTOP_SESSION', raising=False)
    assert requires_manual_title() is False


# --- construction ----------------------------------------------------------


def test_the_dialog_is_parented_to_the_trays_hidden_widget(qapp, tray):
    parent = QLabel()
    tray.dialog_parent = parent
    dialog = CommonDialog(tray)
    assert dialog.parent() is parent


def test_the_dialog_sets_exactly_the_flags_it_means_to(dialog):
    # `setWindowFlags()` *replaces*, so this is the whole set: a dialog frame
    # with a title and a close button, and deliberately no `Tool` -- that hint
    # was ignored anyway (see the Sway section of tests/AUDIT.md).  Asserted as
    # one value so a change in either direction has to be deliberate.
    # (`Tool` is a composite value, so `flags & Tool` says nothing.)
    assert dialog.windowFlags() == (
        Qt.WindowType.Window
        | Qt.WindowType.Dialog
        | Qt.WindowType.WindowTitleHint
        | Qt.WindowType.WindowSystemMenuHint
        | Qt.WindowType.WindowCloseButtonHint)


def test_the_dialog_is_deleted_on_close(dialog):
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)


def test_the_dialog_derives_its_directories_from_home(dialog, isolated_home):
    assert dialog.home_dir == str(isolated_home)
    assert dialog.vault_dir == os.path.join(str(isolated_home), 'Vaults')
    assert dialog.dot_vault_dir == os.path.join(str(isolated_home), '.Vaults')


def test_the_dialog_uses_sudo_user_for_home(monkeypatch, qapp, tray):
    monkeypatch.setenv('SUDO_USER', 'someone')
    dialog = CommonDialog(tray)
    assert dialog.home_dir == '/home/someone'


# --- titles ----------------------------------------------------------------


def last_widget(layout):
    item = layout.itemAt(layout.count() - 1) if layout.count() else None
    return item.widget() if item is not None else None


def test_set_title_sets_the_window_title(dialog, monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'i3')
    before = dialog.main_layout.count()
    dialog.set_title('Mount Crypt File [luks-tray]')
    assert dialog.windowTitle() == 'Mount Crypt File [luks-tray]'
    # Without the Sway workaround nothing extra is added inside the dialog.
    assert dialog.main_layout.count() == before


def test_set_title_adds_an_in_dialog_title_bar_on_sway(dialog, monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'sway')
    before = dialog.main_layout.count()
    dialog.set_title('Mount Crypt File [luks-tray]')
    assert dialog.windowTitle() == 'Mount Crypt File [luks-tray]'
    assert dialog.main_layout.count() == before + 1
    label = last_widget(dialog.main_layout)
    assert isinstance(label, QLabel)
    assert label.text() == 'Mount Crypt File [luks-tray]'
    assert label.alignment() & Qt.AlignmentFlag.AlignCenter
    # Styled to read as a title bar, since the compositor draws none.
    assert 'background-color' in label.styleSheet()


# --- layout helpers --------------------------------------------------------


def test_add_line_appends_a_label(dialog):
    dialog.add_line('hello')
    label = last_widget(dialog.main_layout)
    assert isinstance(label, QLabel)
    assert label.text() == 'hello'


def test_add_push_button_calls_the_method_with_the_argument(dialog):
    seen = []
    dialog.add_push_button('Go', lambda arg: seen.append(arg), 'value')
    button = dialog.button_layout.itemAt(0).widget()
    assert isinstance(button, QPushButton)
    button.click()
    assert seen == ['value']


def test_add_input_field_sizes_a_text_field_to_its_placeholder(dialog):
    dialog.add_input_field('upon', 'Mount point', '/home/joe/Vaults', char_width=5)
    field = dialog.inputs['upon']
    assert isinstance(field, QLineEdit)
    assert field.text() == '/home/joe/Vaults'
    assert field.width() == max(len('/home/joe/Vaults'), 5) * 10


def test_add_input_field_repeats_a_scalar_label_for_every_key(dialog):
    dialog.add_input_field(['a', 'b'], 'Label', 'x')
    assert sorted(dialog.inputs) == ['a', 'b']
    assert dialog.inputs['b'].text() == 'x'


def test_add_input_field_builds_a_checkbox(dialog):
    dialog.add_input_field('readonly', 'Read only', '', field_type='checkbox')
    field = dialog.inputs['readonly']
    assert isinstance(field, QCheckBox)
    assert field.text() == 'Read only'
    assert field.isChecked() is False


def test_add_input_field_refuses_an_unknown_type(dialog):
    with pytest.raises(ValueError, match='invalid field_type'):
        dialog.add_input_field('x', 'X', 'x', field_type='slider')


def test_add_input_field_refuses_mismatched_labels(dialog):
    with pytest.raises(ValueError, match='2 key'):
        dialog.add_input_field(['a', 'b'], ['one', 'two', 'three'], 'p')


def test_add_input_field_adds_a_browse_button(dialog):
    dialog.add_input_field('file', 'File', '/tmp/x.luks', add_on='file')
    labels = [button.text() for button in dialog.findChildren(QPushButton)]
    assert 'Browse...' in labels


# --- passwords -------------------------------------------------------------


def test_password_is_shown_when_the_configuration_says_so(qapp, vault_root):
    dialog = CommonDialog(FakeTray(vault_root, show_passwords=True))
    dialog.add_input_field('pw', 'Password', '', add_on='password')
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Normal
    assert dialog.password_toggle.isChecked() is True


def test_password_is_hidden_when_the_configuration_says_so(qapp, vault_root):
    dialog = CommonDialog(FakeTray(vault_root, show_passwords=False))
    dialog.add_input_field('pw', 'Password', '', add_on='password')
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.password_toggle.text() == '👁️'


def test_an_existing_password_is_never_exposed_by_default(qapp, vault_root):
    dialog = CommonDialog(FakeTray(vault_root, show_passwords=True))
    dialog.add_input_field('pw', 'Password', 'existing-secret', add_on='password')
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Password


def test_toggle_switches_the_echo_mode(dialog):
    dialog.add_input_field('pw', 'Password', 'secret', add_on='password')
    dialog.password_toggle.setChecked(True)
    dialog.toggle_password_visibility()
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Normal
    dialog.password_toggle.setChecked(False)
    dialog.toggle_password_visibility()
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Password


def test_password_helpers_are_safe_before_a_password_field_exists(dialog):
    dialog.hide_password()
    dialog.show_password()
    assert dialog.password_input is None


# --- file pickers ----------------------------------------------------------


def test_browse_folder_starts_at_the_current_text(dialog, monkeypatch):
    seen = []
    monkeypatch.setattr(Common.QFileDialog, 'getExistingDirectory',
                        lambda _parent, _title, start: seen.append(start) or '/tmp/chosen')
    field = QLineEdit()
    field.setText('/tmp/current')
    dialog.browse_folder(field)
    assert seen == ['/tmp/current']
    assert field.text() == '/tmp/chosen'


def test_browse_folder_starts_at_home_when_the_field_is_empty(
        dialog, monkeypatch, isolated_home):
    seen = []
    monkeypatch.setattr(Common.QFileDialog, 'getExistingDirectory',
                        lambda _parent, _title, start: seen.append(start) or '')
    field = QLineEdit()
    dialog.browse_folder(field)
    assert seen == [str(isolated_home)]
    assert field.text() == ''


def test_browse_file_prefers_the_dot_vault_dir(dialog, monkeypatch):
    os.makedirs(dialog.dot_vault_dir)
    seen = []
    monkeypatch.setattr(Common.QFileDialog, 'getOpenFileName',
                        lambda _parent, _title, start: seen.append(start) or ('/tmp/a.luks', ''))
    field = QLineEdit()
    dialog.browse_file(field)
    assert seen == [dialog.dot_vault_dir]
    assert field.text() == '/tmp/a.luks'


def test_browse_file_falls_back_to_home(dialog, monkeypatch, isolated_home):
    seen = []
    monkeypatch.setattr(Common.QFileDialog, 'getOpenFileName',
                        lambda _parent, _title, start: seen.append(start) or ('', ''))
    field = QLineEdit()
    dialog.browse_file(field)
    assert seen == [str(isolated_home)]


def test_browse_new_file_writes_the_chosen_path(dialog, monkeypatch):
    monkeypatch.setattr(Common.QFileDialog, 'getSaveFileName',
                        lambda _parent, _title, _start: ('/tmp/new.luks', ''))
    field = QLineEdit()
    dialog.browse_new_file(field)
    assert field.text() == '/tmp/new.luks'


# --- errors and progress ---------------------------------------------------


class FakeBox:
    instances = []

    class Icon:
        Critical = 'critical'

    class StandardButton:
        Ok = 'ok'

    def __init__(self, _parent=None):
        self.texts = []
        FakeBox.instances.append(self)

    def setIcon(self, _icon):
        pass

    def setWindowTitle(self, title):
        self.texts.append(title)

    def setText(self, text):
        self.texts.append(text)

    def setInformativeText(self, text):
        self.texts.append(text)

    def setStandardButtons(self, _buttons):
        pass

    def exec(self):
        self.texts.append('shown')


def test_alert_errors_shows_one_box_for_the_whole_list(dialog, monkeypatch):
    FakeBox.instances = []
    monkeypatch.setattr(Common, 'QMessageBox', FakeBox)
    dialog.alert_errors(['first', 'second'])
    assert len(FakeBox.instances) == 1
    assert 'first\nsecond' in FakeBox.instances[0].texts
    assert 'shown' in FakeBox.instances[0].texts


def test_alert_errors_does_nothing_for_an_empty_list(dialog, monkeypatch):
    FakeBox.instances = []
    monkeypatch.setattr(Common, 'QMessageBox', FakeBox)
    dialog.alert_errors([])
    assert FakeBox.instances == []


def test_show_progress_disables_the_buttons_and_reuses_its_widgets(dialog):
    dialog.add_push_button('Mount', lambda _arg: None)
    button = dialog.button_layout.itemAt(0).widget()
    dialog.show_progress('Mounting...')
    assert button.isEnabled() is False
    assert isinstance(dialog.progress_label, QLabel)
    assert isinstance(dialog.progress_bar, QProgressBar)
    assert dialog.progress_label.text() == 'Mounting...'
    first_bar = dialog.progress_bar
    dialog.show_progress('Still mounting...')
    # The same widgets are reused, so a re-entrant call cannot stack bars.
    assert dialog.progress_bar is first_bar
    assert dialog.progress_label.text() == 'Still mounting...'


def test_hide_progress_reenables_the_buttons(dialog):
    dialog.add_push_button('Mount', lambda _arg: None)
    button = dialog.button_layout.itemAt(0).widget()
    dialog.show_progress('Mounting...')
    dialog.hide_progress()
    assert button.isEnabled() is True
    assert dialog.progress_bar.isVisible() is False


def test_hide_progress_is_safe_before_any_progress(dialog):
    dialog.hide_progress()  # no progress widgets exist yet


# --- the mount-point policy ------------------------------------------------


def test_check_upon_requires_a_value(dialog):
    assert dialog.check_upon('', set()) == (
        'ERR: empty field not allowed for devices or files')


def test_check_upon_requires_an_absolute_path(dialog):
    assert 'must be absolute path' in dialog.check_upon('relative/dir', set())


def test_check_upon_refuses_a_special_device(dialog):
    # `DeviceInfo.bans` is the list of mount points the tray refuses to use.
    assert 'special' in dialog.check_upon('/proc', set())


def test_check_upon_refuses_the_auto_mount_root_itself(dialog, tray):
    assert 'auto_mount_folder itself' in dialog.check_upon(tray.auto_root, set())


def test_check_upon_requires_an_empty_field_for_media(dialog, tmp_path):
    target = str(tmp_path / 'media-mount')
    assert 'use empty field for automatic /media mounting' in (
        dialog.check_upon('/media/usb', set()))
    assert 'cannot mount device in /media' in (
        dialog.check_upon('/media/usb', set(), is_device=True))
    assert not os.path.exists(target)


def test_check_upon_requires_an_existing_parent(dialog, tmp_path):
    missing = str(tmp_path / 'nope' / 'below')
    assert 'parent directory' in dialog.check_upon(missing, set())


def test_check_upon_allows_the_auto_root_as_the_parent(dialog, tray):
    # `generate_auto_mount_folder()` hands out paths directly below the root,
    # which does not exist on a fresh install.
    target = os.path.join(tray.auto_root, 'turing')
    assert dialog.check_upon(target, set()) is None


def test_check_upon_refuses_a_file(dialog, tmp_path):
    target = tmp_path / 'file'
    target.write_text('x')
    assert 'is not a directory' in dialog.check_upon(str(target), set())


def test_check_upon_refuses_a_non_empty_directory(dialog, tmp_path):
    target = tmp_path / 'full'
    target.mkdir()
    (target / 'occupant').write_text('x')
    assert 'is not empty' in dialog.check_upon(str(target), set())


def test_check_upon_refuses_an_occupied_mount_point(dialog, tmp_path):
    target = str(tmp_path / 'empty')
    os.makedirs(target)
    assert 'occupied' in dialog.check_upon(target, {target})


def test_check_upon_accepts_a_new_path_below_an_existing_parent(dialog, tmp_path):
    assert dialog.check_upon(str(tmp_path / 'fresh'), set()) is None


def test_check_upon_does_not_guard_against_a_non_path_object(dialog):
    # `abspath()` is wrapped, but `isabs()` above it is not: a caller that
    # passes something other than a str gets a TypeError rather than a policy
    # answer.  That is the right trade here -- every call site passes the text
    # of a QLineEdit -- and this records it so a change is deliberate.
    class Awkward:
        def __fspath__(self):
            raise TypeError('not a path')

    with pytest.raises(TypeError):
        dialog.check_upon(Awkward(), set())
