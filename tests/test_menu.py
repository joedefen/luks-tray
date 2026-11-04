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

from importlib import resources
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import QLabel, QMenu, QWidget, QWidgetAction

from luks_tray import Main, __version__
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


# --- the fixed entries -----------------------------------------------------


class FakeIniTool:
    """The two settings `update_menu_items()` and `open_log()` read."""

    def __init__(self, log_viewer=''):
        self.values = {'show_anomaly_alerts': True, 'log_viewer': log_viewer}
        self.log_path = '/home/joe/.config/luks-tray/debug.log'
        self.log_old_path = '/home/joe/.config/luks-tray/debug.log.old'

    def get_current_val(self, key, selector='ui'):
        return self.values[key]


def menu_builder(qapp, status='locked', log_viewer='', bind=(), **overrides):
    """A `LuksTray` stand-in carrying only what `update_menu_items()` reads.

    The locked state keeps the list short -- master password, Exit, Restart --
    so the assertions are about the fixed entries at the bottom of the menu
    rather than about containers. `overrides` replaces any of the
    callbacks, so a test can watch what an entry triggers instead.

    `log_viewer_available` is inert by default: the answer depends on which
    terminal emulators are installed, so a menu test that changed with the
    machine would prove nothing. `bind` names the real `LuksTray` methods a test
    wants to exercise (`open_log`, `log_viewer_available`); everything else is
    an inert stand-in, which is also what keeps a test from opening a window.
    """
    instance = LuksTray.__new__(LuksTray)
    instance.history = SimpleNamespace(status=status)
    instance.ini_tool = FakeIniTool(log_viewer)
    instance.containers = {}
    instance.app = None
    instance.mono_font = QFont()
    instance.replace_menu_if_different = lambda actions, icon_key: actions
    instance.exit_app = lambda: None
    instance.handle_options_click = lambda: None
    instance.handle_master_password_click = lambda: None
    instance.open_log = lambda: None
    instance.log_viewer_available = lambda: False
    instance.restart_self = lambda *args: None
    for name in bind:
        setattr(instance, name, getattr(LuksTray, name).__get__(instance))
    for name, value in overrides.items():
        setattr(instance, name, value)
    return instance


def menu_texts(actions):
    """The action list as text, with `None` kept for the separators."""
    return [action.text() if action is not None else None for action in actions]


def test_the_restart_entry_carries_the_app_name_and_version(qapp):
    texts = menu_texts(menu_builder(qapp).update_menu_items())

    assert texts[-2:] == ['☓ Exit', f'↺ Restart luks-tray v{__version__}']


def test_the_restart_entry_restarts_the_applet(qapp):
    called = []
    instance = menu_builder(qapp, restart_self=lambda *args: called.append(True))

    actions = instance.update_menu_items()
    actions[-1].trigger()

    assert called == [True]


def test_the_restart_entry_says_it_locks_a_readable_history(qapp):
    """A restart *is* a lock -- the password lives in memory only -- so it is
    also the manual way to lock without leaving."""
    texts = menu_texts(menu_builder(qapp, status='unlocked').update_menu_items())

    assert texts[-1] == f'↺ Restart luks-tray v{__version__} (locks history)'


@pytest.mark.parametrize('status', ['locked', 'clear_text'])
def test_the_restart_entry_says_nothing_extra_without_a_key(qapp, status):
    texts = menu_texts(menu_builder(qapp, status=status).update_menu_items())

    assert texts[-1] == f'↺ Restart luks-tray v{__version__}'


def test_the_unlocked_menu_lists_create_add_and_the_shared_tail(qapp):
    texts = menu_texts(menu_builder(qapp, status='unlocked').update_menu_items())

    assert texts == [
        '✦ Create New Crypt File',
        '✚ Add Existing Crypt File',
        None,
        '⚙ Options…',
        '✎ Update Master Password…',
        None,
        '☓ Exit',
        f'↺ Restart luks-tray v{__version__} (locks history)',
    ]


def test_the_locked_menu_offers_only_the_password(qapp):
    # A locked history is the one state in which the master password can be
    # neither proved nor rewritten, so the prompt is the whole of what the menu
    # offers -- no Options, and no create/update entry.
    texts = menu_texts(menu_builder(qapp, status='locked').update_menu_items())

    assert texts == [
        '✎ Click to enter master password',
        None,
        '☓ Exit',
        f'↺ Restart luks-tray v{__version__}',
    ]


@pytest.mark.parametrize('status', ['locked', 'clear_text', 'unlocked'])
def test_every_fixed_entry_starts_with_a_symbol(qapp, status):
    for action in menu_builder(qapp, status=status).update_menu_items():
        if action is None:
            continue
        text = action.text()
        assert not text[0].isalnum() and not text[0].isspace(), text


def test_the_master_password_entry_creates_one_when_there_is_none(qapp):
    texts = menu_texts(
        menu_builder(qapp, status='clear_text').update_menu_items())

    assert '✎ Create Master Password…' in texts


def test_the_master_password_entry_updates_one_that_is_set(qapp):
    texts = menu_texts(menu_builder(qapp, status='unlocked').update_menu_items())

    assert '✎ Update Master Password…' in texts
    assert not [text for text in texts if text and 'Create Master' in text]


def test_the_master_password_entry_sits_below_options(qapp):
    """One flat list: the key guards the whole file, it is not one of its settings."""
    texts = menu_texts(menu_builder(qapp, status='unlocked').update_menu_items())

    assert texts.index('✎ Update Master Password…') == texts.index('⚙ Options…') + 1


def test_the_master_password_entry_opens_the_create_dialog_for_clear_text(
        qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        Main, 'MasterPasswordSetupDialog',
        lambda tray, create=False: SimpleNamespace(
            exec=lambda: opened.append(create)))
    instance = menu_builder(qapp, status='clear_text',
                            bind=('handle_master_password_click',))

    instance.handle_master_password_click()

    assert opened == [True]


def test_the_master_password_entry_opens_the_update_dialog_for_a_set_password(
        qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(
        Main, 'MasterPasswordSetupDialog',
        lambda tray, create=False: SimpleNamespace(
            exec=lambda: opened.append(create)))
    instance = menu_builder(qapp, status='unlocked',
                            bind=('handle_master_password_click',))

    instance.handle_master_password_click()

    assert opened == [False]


def test_a_stale_master_password_click_on_a_locked_history_asks_for_the_password(
        qapp, monkeypatch):
    """The entry is gone from the locked menu, but a menu can be one tick old."""
    opened = []
    monkeypatch.setattr(
        Main, 'MasterPasswordSetupDialog',
        lambda tray, create=False: SimpleNamespace(
            exec=lambda: opened.append('setup')))
    instance = menu_builder(qapp, status='locked',
                            bind=('handle_master_password_click',))
    instance.prompt_master_password = lambda: opened.append('password')

    instance.handle_master_password_click()

    assert opened == ['password']


def test_the_options_entry_opens_the_options_dialog(qapp):
    called = []
    instance = menu_builder(qapp, status='unlocked',
                            handle_options_click=lambda: called.append(True))

    actions = instance.update_menu_items()
    options = [action for action in actions
               if action is not None and action.text().startswith('⚙')]
    options[0].trigger()

    assert called == [True]


def test_the_options_click_opens_the_dialog_for_a_readable_history(
        qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(Main, 'OptionsDialog', lambda tray: SimpleNamespace(
        exec=lambda: opened.append('options')))
    instance = menu_builder(qapp, status='unlocked',
                            bind=('handle_options_click',))

    instance.handle_options_click()

    assert opened == ['options']


def test_a_stale_options_click_on_a_locked_history_asks_for_the_password(
        qapp, monkeypatch):
    """The entry is gone from the locked menu, but a menu can be one tick old.

    Doing nothing would leave a click on a dead entry; opening the dialog would
    be the form-over-an-unreadable-file this change exists to prevent. Asking
    for the password is the action the user was reaching for anyway.
    """
    opened = []
    monkeypatch.setattr(Main, 'OptionsDialog', lambda tray: SimpleNamespace(
        exec=lambda: opened.append('options')))
    instance = menu_builder(qapp, status='locked',
                            bind=('handle_options_click',))
    instance.prompt_master_password = lambda: opened.append('password')

    instance.handle_options_click()

    assert opened == ['password']


# --- locking on a session event --------------------------------------------
#
# `SessionLockWatch` turns logind's Lock / Active / PrepareForSleep events into
# `lock_history()`.  The watch itself is asserted in `test_lock_watch.py`; what
# matters here is the tray's half: the history really loses its key, the menu is
# rebuilt so the state is visible, and the second and third event of one screen
# lock cost nothing.


class FakeHistory:
    """The one method `lock_history()` calls, plus the state it reports."""

    def __init__(self, status='unlocked'):
        self.status = status
        self.locked = 0

    def lock(self):
        if self.status != 'unlocked':
            return False
        self.status = 'locked'
        self.locked += 1
        return True


def lockable_tray(qapp, status='unlocked'):
    """A tray carrying only a history and a menu rebuild, both recorded."""
    instance = LuksTray.__new__(LuksTray)
    instance.history = FakeHistory(status)
    instance.menus = []
    instance.update_menu = lambda: instance.menus.append(True)
    return instance


def test_a_session_event_locks_the_history(qapp):
    tray = lockable_tray(qapp)

    assert tray.lock_history('screen lock') is True

    assert tray.history.status == 'locked'
    assert tray.history.locked == 1
    assert tray.menus == [True]


def test_one_screen_lock_costs_one_menu_rebuild(qapp):
    """The Lock signal and LockedHint both arrive; the second is not a change."""
    tray = lockable_tray(qapp)
    tray.lock_history()

    assert tray.lock_history() is False
    assert tray.menus == [True]


def test_a_history_with_no_key_is_not_locked_by_an_event(qapp):
    tray = lockable_tray(qapp, status='clear_text')

    assert tray.lock_history() is False
    assert tray.history.status == 'clear_text'
    assert tray.menus == []


def test_a_locked_history_skips_the_container_merge(qapp):
    """A mid-session lock must not make the poller write what it cannot read."""
    instance = LuksTray.__new__(LuksTray)
    instance.refreshing = False
    instance.operation = None
    instance.history = SimpleNamespace(status='locked')
    instance.ini_tool = SimpleNamespace(update_config=lambda: None)
    called = []
    instance.update_menu_items = lambda: called.append('items')
    instance.update_mounts = lambda: called.append('mounts')
    instance.merge_containers_history = lambda: called.append('merge')

    instance.update_menu()

    assert called == ['items']


# --- the log entry ---------------------------------------------------------


def test_the_log_entry_is_offered_only_when_something_can_show_it(qapp):
    # The applet's stdout *is* the log file, so less needs a terminal window.
    # Offering the entry without one would produce a menu item that does
    # nothing, which is worse than the entry not being there.
    assert '≡ Open Log' in menu_texts(
        menu_builder(qapp, log_viewer_available=lambda: True).update_menu_items())
    assert '≡ Open Log' not in menu_texts(
        menu_builder(qapp, log_viewer_available=lambda: False).update_menu_items())


def test_the_log_entry_sits_below_options_and_above_the_separator(qapp):
    texts = menu_texts(
        menu_builder(qapp, status='unlocked',
                     log_viewer_available=lambda: True).update_menu_items())

    assert texts[-6:] == ['⚙ Options…', '✎ Update Master Password…',
                          '≡ Open Log', None, '☓ Exit',
                          f'↺ Restart luks-tray v{__version__}'
                          ' (locks history)']


def test_the_log_entry_opens_the_log(qapp):
    called = []
    instance = menu_builder(qapp, log_viewer_available=lambda: True,
                            open_log=lambda: called.append(True))

    actions = instance.update_menu_items()
    entry = [action for action in actions
             if action is not None and action.text().startswith('≡')]
    entry[0].trigger()

    assert called == [True]


def test_the_viewer_availability_uses_the_configured_prefix(qapp, monkeypatch):
    asked = []

    def fake_terminal_argv(command, preferred=''):
        asked.append((list(command), preferred))
        return ['foot', '-e', *command]

    monkeypatch.setattr(Main.Utils, 'terminal_argv', fake_terminal_argv)
    instance = menu_builder(qapp, log_viewer='foot -e',
                            bind=('log_viewer_available',))

    assert instance.log_viewer_available() is True
    # The probe asks "can anything start?", so it passes no command at all.
    assert asked == [([], 'foot -e')]


def test_opening_the_log_spawns_the_viewer_outside_our_process(qapp, monkeypatch):
    spawned, asked = [], []
    monkeypatch.setattr(
        Main.Utils, 'log_viewer_argv',
        lambda log, old, preferred: asked.append((log, old, preferred))
        or ['foot', '-e', 'less', '+F', log])
    monkeypatch.setattr(Main.Utils, 'spawn_detached',
                        lambda command, **kwargs: spawned.append((command, kwargs)))
    instance = menu_builder(qapp, log_viewer='foot -e', bind=('open_log',))

    instance.open_log()

    assert asked == [(instance.ini_tool.log_path,
                      instance.ini_tool.log_old_path, 'foot -e')]
    # `quiet=True` is the point, not decoration: the viewer inherits fds 1
    # and 2, which prt() pointed at debug.log, and a Qt terminal emulator
    # then writes its own Qt chatter (the platform theme's palette table,
    # QThreadStorage, QLayout) into our log in Qt's raw, unstamped format.
    # No message handler can filter that -- it is a different process.
    assert spawned == [(['foot', '-e', 'less', '+F',
                         instance.ini_tool.log_path], {'quiet': True})]


def test_opening_the_log_without_a_viewer_only_complains(qapp, monkeypatch,
                                                        capsys):
    spawned = []
    monkeypatch.setattr(Main.Utils, 'log_viewer_argv',
                        lambda log, old, preferred: [])
    monkeypatch.setattr(Main.Utils, 'spawn_detached', spawned.append)
    instance = menu_builder(qapp, bind=('open_log',))

    instance.open_log()

    assert spawned == []
    assert 'ui.log_viewer' in capsys.readouterr().out


def test_a_viewer_that_cannot_start_is_reported_not_raised(qapp, monkeypatch,
                                                           capsys):
    monkeypatch.setattr(Main.Utils, 'log_viewer_argv',
                        lambda log, old, preferred: ['foot', '-e', 'less'])
    monkeypatch.setattr(Main.Utils, 'spawn_detached',
                        lambda command, **kwargs:
                        (_ for _ in ()).throw(OSError('no such file')))
    instance = menu_builder(qapp, bind=('open_log',))

    instance.open_log()

    assert 'cannot open the log' in capsys.readouterr().out


def test_the_log_entry_is_plain_while_nothing_has_failed(qapp):
    texts = menu_texts(
        menu_builder(qapp, log_viewer_available=lambda: True).update_menu_items())

    assert '≡ Open Log' in texts


def test_the_log_entry_carries_the_session_s_failure_count(qapp):
    Main.Utils.note_problem('ERR: unlock failed')

    texts = menu_texts(
        menu_builder(qapp, log_viewer_available=lambda: True).update_menu_items())

    # The dialog says what failed; the marker says the log has the why.
    assert '≡ Open Log [1 error]' in texts


def test_the_marker_grows_and_says_errors_in_the_plural(qapp):
    for _ in range(3):
        Main.Utils.note_problem('ERR: nope')

    assert menu_builder(qapp).log_entry_text() == '≡ Open Log [3 errors]'


def test_a_new_session_clears_the_marker(qapp):
    Main.Utils.note_problem('ERR: from a run that is over')

    Main.Utils.log_session_start('luks-tray', '9.9.9')

    assert menu_builder(qapp).log_entry_text() == '≡ Open Log'


# --- the tray state icon ---------------------------------------------------


def icon_key_for(qapp, status, **overrides):
    """The icon key `update_menu_items()` asks for in a given history state."""
    seen = []
    instance = menu_builder(
        qapp, status=status,
        replace_menu_if_different=lambda actions, key: seen.append(key) or actions,
        **overrides)
    instance.update_menu_items()
    return seen[-1]


def test_a_locked_history_shows_the_orange_shield(qapp):
    # White claims every container is locked, and an encrypted history the applet
    # cannot open hides them all -- the one claim this state cannot make.
    assert icon_key_for(qapp, 'locked') == 'locked'


@pytest.mark.parametrize('status', ['unlocked', 'clear_text'])
def test_an_open_history_with_nothing_unlocked_stays_white(qapp, status):
    assert icon_key_for(qapp, status) == 'none'


def test_the_icon_table_pairs_every_nickname_with_a_shipped_shield():
    table = LuksTray.svg_info

    assert len(table.nicknames) == len(table.bases)
    assert set(table.nicknames) == {'none', 'alert', 'ok', 'locked'}
    for base in table.bases:
        name = f'{base}-v{table.version}.svg'
        assert resources.files('luks_tray.resources').joinpath(name).is_file(), name


# --- the container rows ----------------------------------------------------


def file_container(upon='', vital=None):
    """A crypt *file* container, with the fields the menu line reads."""
    container = Main.DeviceInfo.make_partition_namespace('tcrypt.img-luks', '64M')
    container.type = 'crypt'
    container.uuid = 'salt-abc'
    container.back_file = '/home/joe/.Vaults/test-only-tcrypt.img'
    container.upon = upon
    container.opened = bool(upon)
    container.vital = vital
    return container


def test_a_mounted_file_container_is_shortened_on_both_halves(qapp, monkeypatch):
    monkeypatch.setenv('HOME', '/home/joe')
    instance = menu_builder(qapp, status='unlocked',
                            containers={'salt-abc': file_container(
                                upon='/home/joe/Vaults/test-only-tcrypt')})

    texts = menu_texts(instance.update_menu_items())

    # The same home appears twice on the line -- once as the file, once as the
    # mount point -- and neither half spells it out.
    assert texts[0] == '▣ ~/.Vaults/test-only-tcrypt.img ~/Vaults/test-only-tcrypt'


def test_a_remembered_mount_point_is_bracketed_and_shortened_too(qapp, monkeypatch):
    monkeypatch.setenv('HOME', '/home/joe')
    instance = menu_builder(qapp, status='unlocked', containers={
        'salt-abc': file_container(
            vital=SimpleNamespace(upon='/home/joe/Vaults/test-only-tcrypt'))})

    texts = menu_texts(instance.update_menu_items())

    # Not mounted, so the remembered mount point is bracketed -- and still
    # abbreviated, because a line full of /home/<user> reads badly.
    assert texts[0] == '▽ ~/.Vaults/test-only-tcrypt.img [~/Vaults/test-only-tcrypt]'


def test_a_container_with_no_mount_point_shows_only_its_file(qapp, monkeypatch):
    monkeypatch.setenv('HOME', '/home/joe')
    instance = menu_builder(qapp, status='unlocked',
                            containers={'salt-abc': file_container()})

    texts = menu_texts(instance.update_menu_items())

    # No empty brackets around a mount point that was never chosen.
    assert texts[0] == '▽ ~/.Vaults/test-only-tcrypt.img'
