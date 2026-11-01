"""Tray (re-)registration must survive a bar that starts late.

Qt registers a `QSystemTrayIcon` with the StatusNotifierWatcher once, when the
item is first shown, and never retries.  A bar that starts after the tray -- or
restarts -- therefore leaves the applet running with an item no host knows
about, so the old "wait for a tray at start-up" shape only moved the failure
window.  These tests pin the replacement: the watcher's own bus name is the
event that registers the item again, `tray_watcher_present()` answers True,
False or None instead of blocking, and QtDBus is imported lazily so start-up
never needs a session bus.

`watch_mode()` is exercised for both enum layouts because PyQt6 exposes the
members as `QDBusServiceWatcher.WatchModeFlag.*` while PyQt5 puts them straight
on the class -- and the module has to work with whichever is installed.
"""

import inspect
import sys
import types
from typing import ClassVar

import pytest

import luks_tray.Main as main_module
from luks_tray.TrayWatch import STATUS_NOTIFIER_WATCHER, TrayWatch, watch_mode

# -- fakes for the QtDBus surface we use -------------------------------------


class FakeReply:
    """Stands in for QDBusReply<bool>."""

    def __init__(self, value=False, valid=True):
        self._value = value
        self._valid = valid

    def isValid(self):
        return self._valid

    def value(self):
        return self._value


class FakeSignal:
    """Stands in for a Qt signal on a bus interface."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class FakeInterface:
    """Stands in for QDBusConnectionInterface."""

    def __init__(self, reply):
        self.reply = reply
        self.timeout = None
        self.queried = []

    def setTimeout(self, milliseconds):
        self.timeout = milliseconds

    def isServiceRegistered(self, name):
        self.queried.append(name)
        return self.reply


class FakeServiceWatcher:
    """Stands in for QDBusServiceWatcher; instances are recorded for the tests.

    The enum members sit on a nested `WatchModeFlag`, which is the PyQt6
    layout; `watch_mode()` is asked to cope with both spellings.
    """

    class WatchModeFlag:
        WatchForRegistration = 1
        WatchForUnregistration = 2

    instances: ClassVar[list] = []

    def __init__(self, service, connection, mode):
        self.service = service
        self.connection = connection
        self.mode = mode
        self.serviceRegistered = FakeSignal()
        self.serviceUnregistered = FakeSignal()
        FakeServiceWatcher.instances.append(self)


class FakeBus:
    """Stands in for QDBusConnection."""

    def __init__(self, connected=True, reply=None):
        self._connected = connected
        self.interface_obj = FakeInterface(reply or FakeReply())

    def isConnected(self):
        return self._connected

    def interface(self):
        return self.interface_obj


def install_fake_qtdbus(monkeypatch, bus):
    """Install a fake QtDBus module whose watchers are recorded."""
    FakeServiceWatcher.instances = []
    module = types.ModuleType('PyQt6.QtDBus')
    module.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: bus)
    module.QDBusServiceWatcher = FakeServiceWatcher
    monkeypatch.setitem(sys.modules, 'PyQt6.QtDBus', module)


def last_watcher():
    """The QDBusServiceWatcher the code under test just created."""
    return FakeServiceWatcher.instances[-1]


@pytest.fixture
def reported(monkeypatch):
    lines = []
    monkeypatch.setattr(main_module, 'prt',
                        lambda *args, **kwargs: lines.append(args[0] if args else ''))
    return lines


# -- no D-Bus at import time -------------------------------------------------


def test_import_does_not_open_a_session_bus():
    """The module must not create a bus while being imported."""
    source = inspect.getsource(main_module)
    assert 'sessionBus()' not in source.split('def tray_watcher_present', 1)[0]


def test_start_up_does_not_block_on_the_tray():
    """A late bar is an event, so no bounded wait is baked into the module."""
    assert not hasattr(main_module, 'wait_for_tray')
    assert not hasattr(main_module, 'TRAY_WAIT_SECONDS')


# -- watch_mode --------------------------------------------------------------


def test_watch_mode_reads_the_pyqt6_layout():
    assert watch_mode(FakeServiceWatcher) == 3


def test_watch_mode_reads_the_pyqt5_layout():
    class LegacyServiceWatcher:
        WatchForRegistration = 1
        WatchForUnregistration = 2

    assert watch_mode(LegacyServiceWatcher) == 3


# -- tray_watcher_present() --------------------------------------------------


def test_present_is_true_when_the_watcher_owns_the_name(monkeypatch):
    bus = FakeBus(reply=FakeReply(True))
    install_fake_qtdbus(monkeypatch, bus)

    assert main_module.tray_watcher_present() is True
    assert bus.interface_obj.queried == [STATUS_NOTIFIER_WATCHER]


def test_present_uses_a_short_bus_timeout(monkeypatch):
    """The 25s D-Bus default would stall start-up if the bus is wedged."""
    bus = FakeBus(reply=FakeReply(True))
    install_fake_qtdbus(monkeypatch, bus)

    main_module.tray_watcher_present()

    assert bus.interface_obj.timeout is not None
    assert bus.interface_obj.timeout <= 5000


def test_present_is_false_when_nothing_owns_the_name(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(reply=FakeReply(False)))
    assert main_module.tray_watcher_present() is False


def test_present_is_false_for_an_invalid_reply(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(reply=FakeReply(True, valid=False)))
    assert main_module.tray_watcher_present() is False


def test_present_is_none_without_a_session_bus(monkeypatch, reported):
    install_fake_qtdbus(monkeypatch, FakeBus(connected=False))

    assert main_module.tray_watcher_present() is None
    assert any('session bus' in line for line in reported)


def test_present_is_none_without_qtdbus(monkeypatch, reported):
    """PyQt6 can be installed without QtDBus; the tray must still start."""
    monkeypatch.setitem(sys.modules, 'PyQt6.QtDBus', None)

    assert main_module.tray_watcher_present() is None
    assert any('QtDBus' in line for line in reported)


# -- TrayWatch ---------------------------------------------------------------


def make_watch(monkeypatch, bus):
    """A TrayWatch on a fake bus, with its two callbacks recorded."""
    install_fake_qtdbus(monkeypatch, bus)
    events = []
    watch = TrayWatch(on_available=lambda: events.append('available'),
                      on_lost=lambda: events.append('lost'))
    assert watch.start() is True
    assert watch.watching is True
    return watch, events


def test_the_watch_is_bound_to_the_tray_host_name(monkeypatch):
    make_watch(monkeypatch, FakeBus())

    watcher = last_watcher()
    assert watcher.service == STATUS_NOTIFIER_WATCHER
    assert watcher.mode == (FakeServiceWatcher.WatchModeFlag.WatchForRegistration
                            | FakeServiceWatcher.WatchModeFlag.WatchForUnregistration)


def test_the_watcher_appearing_asks_for_a_registration(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['available']


def test_a_watcher_restart_is_an_appearance_too(monkeypatch):
    """waybar being restarted re-registers its name, i.e. starts over."""
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)
    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['lost', 'available']


def test_the_watcher_leaving_is_reported_as_a_loss(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['lost']


def test_another_name_is_ignored(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceRegistered.emit('org.freedesktop.Notifications')
    last_watcher().serviceUnregistered.emit('org.freedesktop.Notifications')

    assert events == []


def test_a_missing_on_lost_callback_is_not_an_error(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus())
    watch = TrayWatch(on_available=lambda: None)
    assert watch.start() is True

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)


def test_stop_disconnects_the_watch(monkeypatch):
    watch, events = make_watch(monkeypatch, FakeBus())

    watch.stop()
    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)
    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == []
    assert watch.watching is False
    assert last_watcher().serviceRegistered.slots == []
    assert last_watcher().serviceUnregistered.slots == []


def test_stop_is_safe_before_start():
    TrayWatch(on_available=lambda: None).stop()


def test_start_is_false_without_qtdbus(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt6.QtDBus', None)

    watch = TrayWatch(on_available=lambda: None)

    assert watch.start() is False
    assert watch.watching is False


def test_start_is_false_without_a_session_bus(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(connected=False))

    watch = TrayWatch(on_available=lambda: None)

    assert watch.start() is False
    assert watch.watching is False


# -- LuksTray: re-registering the item --------------------------------------


class FakeTrayIcon:
    """Records what QSystemTrayIcon is asked to do."""

    def __init__(self, icon, parent=None):
        self.icon = icon
        self.parent = parent
        self.menu = None
        self.tooltip = ''
        self.visible = False
        self.hidden = 0
        self.deleted = 0

    def setToolTip(self, text):
        self.tooltip = text

    def setContextMenu(self, menu):
        self.menu = menu

    def setVisible(self, value):
        self.visible = value

    def hide(self):
        self.hidden += 1
        self.visible = False

    def deleteLater(self):
        self.deleted += 1


ICONS = {'none': 'none-icon', 'alert': 'alert-icon', 'ok': 'ok-icon'}


def bare_tray(monkeypatch, prev_icon_key='ok', menu='MENU'):
    """A LuksTray stripped down to the tray-registration surface."""
    monkeypatch.setattr(main_module, 'QSystemTrayIcon', FakeTrayIcon)
    tray = object.__new__(main_module.LuksTray)
    tray.app = object()
    tray.icons = dict(ICONS)
    tray.prev_icon_key = prev_icon_key
    tray.menu = menu
    return tray


def test_installing_the_icon_shows_it_with_the_current_icon(monkeypatch):
    tray = bare_tray(monkeypatch, prev_icon_key='ok')

    tray._install_tray_icon()

    assert tray.tray_icon.icon == 'ok-icon'
    assert tray.tray_icon.visible is True
    assert tray.tray_icon.menu == 'MENU'
    assert tray.tray_icon.tooltip == 'luks-tray'


def test_the_first_icon_is_used_before_any_state_was_shown(monkeypatch):
    tray = bare_tray(monkeypatch, prev_icon_key='')

    tray._install_tray_icon()

    assert tray.tray_icon.icon == 'none-icon'


def test_an_unknown_icon_key_falls_back_to_the_first_icon(monkeypatch):
    tray = bare_tray(monkeypatch, prev_icon_key='green')

    assert tray.current_icon_key() == 'none'


def test_reinstalling_hides_the_old_item_instead_of_listing_two(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._install_tray_icon()
    previous = tray.tray_icon

    tray._install_tray_icon()

    assert previous.hidden == 1
    assert previous.deleted == 1
    assert tray.tray_icon is not previous
    assert tray.tray_icon.menu == 'MENU'
    assert tray.tray_icon.visible is True


def test_an_appearing_host_schedules_a_re_registration(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = False
    scheduled = []
    monkeypatch.setattr(main_module, 'QTimer', types.SimpleNamespace(
        singleShot=lambda delay, slot: scheduled.append((delay, slot))))

    tray._on_tray_host_appeared()

    assert tray._tray_seen is True
    assert scheduled == [(main_module.TRAY_ATTACH_DELAY_MS,
                          tray._reattach_tray)]


def test_a_host_that_leaves_again_gets_no_item(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = True
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: False)

    tray._reattach_tray()

    assert tray._tray_seen is False
    assert not hasattr(tray, 'tray_icon')


def test_a_host_that_is_really_there_gets_the_item_again(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._install_tray_icon()
    previous = tray.tray_icon
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)

    tray._reattach_tray()

    assert previous.hidden == 1
    assert tray.tray_icon is not previous


def test_losing_the_host_marks_the_item_as_unregistered(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = True

    tray._on_tray_host_lost()

    assert tray._tray_seen is False


def test_the_tick_retries_a_missed_announcement(monkeypatch):
    """A watcher whose name-owner signal never arrived is still picked up."""
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=True)
    tray._tray_seen = False
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    retried = []
    monkeypatch.setattr(tray, '_on_tray_host_appeared',
                        lambda: retried.append(True))

    tray._ensure_tray_attached()

    assert retried == [True]


def test_the_tick_does_nothing_while_attached(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=True)
    tray._tray_seen = True
    monkeypatch.setattr(main_module, 'tray_watcher_present',
                        lambda: pytest.fail('the bus must not be asked'))

    tray._ensure_tray_attached()


def test_the_tick_does_nothing_when_watching_is_impossible(monkeypatch):
    """Without a watch there is no missed signal to compensate for."""
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=False)
    tray._tray_seen = False
    monkeypatch.setattr(main_module, 'tray_watcher_present',
                        lambda: pytest.fail('the bus must not be asked'))

    tray._ensure_tray_attached()


def test_the_watcher_name_is_the_one_qt_hosts_claim():
    assert main_module.STATUS_NOTIFIER_WATCHER == STATUS_NOTIFIER_WATCHER
    assert STATUS_NOTIFIER_WATCHER == 'org.kde.StatusNotifierWatcher'
