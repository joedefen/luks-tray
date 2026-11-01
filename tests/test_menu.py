"""`replace_menu_if_different()`: rebuild the tray menu only when it changed.

This is the fix for the sway symptom that produced the `archive/sway` branch:
rebuilding the `QMenu` on every 3 s tick closed a popup under the user's
cursor, so the new action list is compared first.  The comparison is the whole
point, and it has three inputs -- length, rendered text, icon key -- so each one
is exercised, including the widget-action rows the menu uses for device details
and the `None` entries that become separators.

The `tray` fixture depends on `qapp`: Qt aborts the process if a `QWidget` is
constructed before the `QApplication`, and it does so with a core dump rather
than an exception, so the dependency has to be explicit.
"""

import pytest
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QLabel, QMenu, QWidget, QWidgetAction

from luks_tray.Main import LuksTray

# Qt takes ownership of some of these; keeping a Python reference prevents the
# wrapper from being collected while the C++ object is still in use.
KEEP_ALIVE = []


class RecordingMenu(QMenu):
    """A `QMenu` that reports whether it was asked to pop up again."""

    def __init__(self):
        super().__init__()
        self.popups = 0
        self.visible = False

    def isVisible(self):                            # noqa: N802 - Qt's spelling
        return self.visible

    def popup(self, pos):                           # noqa: N802 - Qt's spelling
        self.popups += 1


class FakeTrayIcon:
    def __init__(self):
        self.icons = []
        self.context_menus = []
        self.shows = 0

    def setIcon(self, icon):                        # noqa: N802 - Qt's spelling
        self.icons.append(icon)

    def setContextMenu(self, menu):                 # noqa: N802 - Qt's spelling
        self.context_menus.append(menu)

    def show(self):                                 # noqa: N802 - Qt's spelling
        self.shows += 1


@pytest.fixture
def tray(qapp):
    """A `LuksTray` with only the attributes the menu code touches."""
    instance = LuksTray.__new__(LuksTray)
    instance.menu = RecordingMenu()
    instance.actions = []
    # A settled state: the *first* install returns before the icon comparison,
    # so it does not record the key (see test_the_first_install_... below).
    # Tests that assert "no rebuild" start from where a running tray is.
    instance.prev_icon_key = 'green'
    instance.icons = {'green': 'green-icon', 'orange': 'orange-icon'}
    instance.tray_icon = FakeTrayIcon()
    return instance


def text_action(text):
    return QAction(text)


def label_action(text):
    widget = QLabel(text)
    action = QWidgetAction(None)
    action.setDefaultWidget(widget)
    KEEP_ALIVE.extend([widget, action])
    return action


def test_the_first_menu_is_always_installed(tray):
    assert tray.replace_menu_if_different([text_action('Mount')], 'green') is True
    assert tray.menu.actions()[0].text() == 'Mount'
    assert tray.tray_icon.icons == ['green-icon']
    assert tray.tray_icon.shows == 1


def test_the_first_install_does_not_record_the_icon_key(tray):
    tray.prev_icon_key = None
    assert tray.replace_menu_if_different([text_action('Mount')], 'green') is True
    # The bootstrap path returns before the icon comparison, so the key is only
    # remembered by the next call that differs -- which costs one extra rebuild
    # at start-up, when nothing is on screen to be disturbed by it.
    assert tray.prev_icon_key is None
    assert tray.replace_menu_if_different([text_action('Mount')], 'green') is True
    assert tray.prev_icon_key == 'green'
    assert tray.replace_menu_if_different([text_action('Mount')], 'green') is False


def test_an_unchanged_menu_is_not_rebuilt(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    shows = tray.tray_icon.shows
    menu = tray.menu
    assert tray.replace_menu_if_different([text_action('Mount')], 'green') is False
    # Same object, and the tray was not asked to show itself again: rebuilding
    # here is what closed a popup under the cursor.
    assert tray.menu is menu
    assert tray.tray_icon.shows == shows


def test_changed_text_rebuilds_the_menu(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    assert tray.replace_menu_if_different([text_action('Unmount')], 'green') is True
    assert tray.menu.actions()[0].text() == 'Unmount'


def test_a_changed_length_rebuilds_the_menu(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    assert tray.replace_menu_if_different(
        [text_action('Mount'), text_action('Quit')], 'green') is True
    assert len(tray.menu.actions()) == 2


def test_a_changed_icon_rebuilds_the_menu_and_is_remembered(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    assert tray.replace_menu_if_different([text_action('Mount')], 'orange') is True
    assert tray.prev_icon_key == 'orange'
    assert tray.tray_icon.icons[-1] == 'orange-icon'


def test_a_separator_is_compared_as_its_own_kind(tray):
    tray.replace_menu_if_different([text_action('Mount'), None], 'green')
    assert tray.replace_menu_if_different(
        [text_action('Mount'), text_action('Quit')], 'green') is True


def test_a_none_entry_becomes_a_separator(tray):
    tray.replace_menu_if_different(
        [text_action('Mount'), None, text_action('Quit')], 'green')
    assert tray.menu.actions()[1].isSeparator() is True


def test_widget_actions_are_compared_by_their_label_text(tray):
    tray.replace_menu_if_different([label_action('Device: /dev/sdb1')], 'green')
    # Same label text in a fresh widget action: no rebuild.
    assert tray.replace_menu_if_different(
        [label_action('Device: /dev/sdb1')], 'green') is False
    # Different text: rebuild.
    assert tray.replace_menu_if_different(
        [label_action('Device: /dev/sdc1')], 'green') is True


def test_a_widget_that_is_not_a_label_compares_by_kind(tray):
    def anonymous_widget_action():
        widget = QWidget()
        action = QWidgetAction(None)
        action.setDefaultWidget(widget)
        KEEP_ALIVE.extend([widget, action])
        return action

    tray.replace_menu_if_different([anonymous_widget_action()], 'green')
    # Two unspecified widgets are indistinguishable to the comparison, so the
    # menu is left alone rather than flickering on every refresh.
    assert tray.replace_menu_if_different([anonymous_widget_action()], 'green') is False


def test_a_visible_menu_is_reopened_at_the_cursor(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    tray.menu.visible = True
    assert tray.replace_menu_if_different([text_action('Unmount')], 'green') is True
    assert tray.menu.popups == 1


def test_an_invisible_menu_is_not_reopened(tray):
    tray.replace_menu_if_different([text_action('Mount')], 'green')
    tray.menu.visible = False
    tray.replace_menu_if_different([text_action('Unmount')], 'green')
    assert tray.menu.popups == 0


def test_the_installed_list_is_kept_for_the_next_comparison(tray):
    actions = [text_action('Mount'), text_action('Quit')]
    tray.replace_menu_if_different(actions, 'green')
    assert tray.actions is actions


@pytest.mark.parametrize('icon_key', ['green', 'orange'])
def test_every_icon_key_in_the_table_is_usable(tray, icon_key):
    assert tray.replace_menu_if_different([text_action('Mount')], icon_key) is True
