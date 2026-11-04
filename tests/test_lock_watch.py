"""The history must lock itself when the session stops being ours.

logind publishes the events that mean exactly that -- the screen locking, a VT
or user switch (`Active` false), a suspend (`PrepareForSleep`) -- so none of them
requires guessing at idleness.  These tests pin the three subscriptions, the half
of each property pair that does anything, and the promise that a session with no
logind simply gets no watch instead of a start-up failure.

The bus is faked on purpose: CI has no logind session, and what has to be right
here is which signal is connected to which handler and what each payload means,
not that Qt can talk to D-Bus.  The end-to-end wiring, from a signal to a locked
history and a rebuilt menu, is asserted in `test_menu.py`.
"""

import inspect
import sys
import types

import luks_tray.LockWatch as lock_module
from luks_tray.LockWatch import (LOCK, LOGIN1, MANAGER_INTERFACE,
                                 MANAGER_PATH, PREPARE_FOR_SLEEP,
                                 PROPERTIES_CHANGED, PROPERTIES_INTERFACE,
                                 SESSION_INTERFACE, SESSION_PATH,
                                 SessionLockWatch)

# -- fakes for the QtDBus surface we use -------------------------------------


class FakeBus:
    """Stands in for QDBusConnection, recording what was subscribed.

    `accept` is either True (take every subscription), False (take none, which
    is what a logind-less bus answers) or a collection of member names, so a
    test can say "this session publishes only the sleep signal".
    """

    def __init__(self, connected=True, accept=True):
        self.connected = connected
        self.accept = accept
        self.subscriptions = []
        self.dropped = []

    def isConnected(self):
        return self.connected

    def connect(self, service, path, interface, member, slot):
        if self.accept is not True and member not in self.accept:
            return False
        self.subscriptions.append((service, path, interface, member, slot))
        return True

    def disconnect(self, service, path, interface, member, slot):
        self.dropped.append((service, path, interface, member, slot))
        if (service, path, interface, member, slot) in self.subscriptions:
            self.subscriptions.remove(
                (service, path, interface, member, slot))

    def fire(self, interface, member, *args):
        """Deliver one signal, as the bus would deliver it."""
        for _service, _path, iface, name, slot in list(self.subscriptions):
            if (iface, name) == (interface, member):
                slot(*args)


def install(monkeypatch, bus):
    """Install a fake QtDBus module whose session bus is `bus`."""
    module = types.ModuleType('PyQt6.QtDBus')
    module.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: bus)
    monkeypatch.setitem(sys.modules, 'PyQt6.QtDBus', module)


def make_watch(monkeypatch, bus=None):
    """A `SessionLockWatch` on a fake bus, with its callback recorded."""
    bus = FakeBus() if bus is None else bus
    install(monkeypatch, bus)
    events = []
    return SessionLockWatch(on_lock=lambda: events.append('lock')), bus, events


def subscribed(bus):
    """The subscriptions without their slots, for comparing."""
    return [each[:4] for each in bus.subscriptions]


# -- the module itself -------------------------------------------------------


def test_import_does_not_open_a_session_bus():
    """Importing the module must never create a bus, or an import would."""
    source = inspect.getsource(lock_module)
    assert 'sessionBus()' not in source.split('def start', 1)[0]


def test_the_session_is_the_callers_own():
    """`/session/auto` is logind's alias for the caller, so nothing resolves ids."""
    assert SESSION_PATH == '/org/freedesktop/login1/session/auto'
    # No shelling out to ask which session this is: the session is the one
    # asking, and the alias is how it says so.
    assert 'subprocess' not in inspect.getsource(lock_module)


# -- what is subscribed ------------------------------------------------------


def test_the_three_events_are_subscribed(monkeypatch):
    watch, bus, _events = make_watch(monkeypatch)

    assert watch.start() is True
    assert watch.watching is True
    assert subscribed(bus) == [
        (LOGIN1, SESSION_PATH, SESSION_INTERFACE, LOCK),
        (LOGIN1, SESSION_PATH, PROPERTIES_INTERFACE, PROPERTIES_CHANGED),
        (LOGIN1, MANAGER_PATH, MANAGER_INTERFACE, PREPARE_FOR_SLEEP),
    ]


def test_starting_twice_does_not_subscribe_twice(monkeypatch):
    watch, bus, _events = make_watch(monkeypatch)

    assert watch.start() is True
    assert watch.start() is True

    assert len(bus.subscriptions) == 3


def test_one_published_event_is_enough(monkeypatch):
    """logind versions differ; a partly watched session still locks."""
    watch, bus, events = make_watch(monkeypatch, FakeBus(accept=(LOCK,)))

    assert watch.start() is True
    assert watch.watching is True
    bus.fire(SESSION_INTERFACE, LOCK)
    assert events == ['lock']


def test_a_bus_that_takes_nothing_is_not_watched(monkeypatch):
    watch, _bus, _events = make_watch(monkeypatch, FakeBus(accept=False))

    assert watch.start() is False
    assert watch.watching is False


# -- the screen locking ------------------------------------------------------


def test_the_lock_signal_locks(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(SESSION_INTERFACE, LOCK)

    assert events == ['lock']


def test_the_lock_hint_locks(monkeypatch):
    """A locker may set `LockedHint` without emitting `Lock`; both count."""
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED,
             SESSION_INTERFACE, {'LockedHint': True})

    assert events == ['lock']


def test_the_hint_going_back_to_false_does_not_lock(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED,
             SESSION_INTERFACE, {'LockedHint': False})

    assert events == []


# -- leaving the foreground --------------------------------------------------


def test_a_vt_or_user_switch_locks(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED,
             SESSION_INTERFACE, {'Active': False})

    assert events == ['lock']


def test_staying_in_front_does_not_lock(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED,
             SESSION_INTERFACE, {'Active': True})

    assert events == []


def test_one_payload_carrying_both_facts_locks_once(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED, SESSION_INTERFACE,
             {'LockedHint': True, 'Active': False})

    assert events == ['lock']


def test_another_interfaces_properties_are_ignored(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED,
             'org.freedesktop.login1.Session.SomethingElse', {'Active': False})

    assert events == []


def test_a_payload_without_a_get_is_ignored(monkeypatch):
    """A malformed signal must not raise inside a Qt slot."""
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(PROPERTIES_INTERFACE, PROPERTIES_CHANGED, SESSION_INTERFACE, [])

    assert events == []


# -- suspend -----------------------------------------------------------------


def test_going_to_sleep_locks(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(MANAGER_INTERFACE, PREPARE_FOR_SLEEP, True)

    assert events == ['lock']


def test_coming_back_does_not_lock(monkeypatch):
    """The lock is taken on the way down, where the screen locker may have died."""
    watch, bus, events = make_watch(monkeypatch)
    watch.start()

    bus.fire(MANAGER_INTERFACE, PREPARE_FOR_SLEEP, False)

    assert events == []


# -- no logind, no watch -----------------------------------------------------


def test_start_is_false_without_qtdbus(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt6.QtDBus', None)
    watch = SessionLockWatch(on_lock=lambda: None)

    assert watch.start() is False
    assert watch.watching is False


def test_start_is_false_without_a_session_bus(monkeypatch):
    watch, _bus, _events = make_watch(monkeypatch, FakeBus(connected=False))

    assert watch.start() is False
    assert watch.watching is False


def test_stop_disconnects_every_subscription(monkeypatch):
    watch, bus, events = make_watch(monkeypatch)
    watch.start()
    assert len(bus.subscriptions) == 3

    watch.stop()
    bus.fire(SESSION_INTERFACE, LOCK)

    assert len(bus.dropped) == 3
    assert events == []
    assert watch.watching is False


def test_stop_is_safe_before_start():
    SessionLockWatch(on_lock=lambda: None).stop()
