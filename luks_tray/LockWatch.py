#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Lock the history when the session stops being ours.

    The master password lives only in memory, so the history is unprotected for
    exactly as long as this process runs -- across a screen lock, a VT switch, a
    user switch, a suspend. logind publishes all of those as events, and they
    mean precisely "the session is no longer mine", so no idle guessing is
    needed to hear them:

    * ``org.freedesktop.login1.Session.Lock`` -- the screen locked;
    * ``LockedHint`` turning true in the session's ``PropertiesChanged`` -- the
      same fact, for a locker that sets the hint without the signal;
    * ``Active`` turning false -- a VT switch, or a switch to another user;
    * ``Manager.PrepareForSleep(true)`` -- the machine is going down, so a resume
      that fails to re-lock the screen still finds the history locked.

    ``/org/freedesktop/login1/session/auto`` is the caller's *own* session, which
    is why nothing here parses ``loginctl`` or trusts ``XDG_SESSION_ID``: the
    session is the one asking.

    ``LockedHint`` is a property and ``Active`` is a property, so neither has a
    signal of its own; both ride in the same ``PropertiesChanged`` payload, which
    is why one connection covers the two of them.

    All of it is best effort. A session with no logind -- a bare window manager,
    a container, a machine where QtDBus was built out -- publishes none of these,
    ``start()`` says so by returning False, and the applet keeps working exactly
    as it did before. QtDBus is imported lazily, so importing this module never
    opens a bus: the same arrangement as `TrayWatch`.
"""

import contextlib

LOGIN1 = 'org.freedesktop.login1'

#: The manager object, which is where sleep is announced.
MANAGER_PATH = '/org/freedesktop/login1'
MANAGER_INTERFACE = 'org.freedesktop.login1.Manager'
PREPARE_FOR_SLEEP = 'PrepareForSleep'

#: The caller's own session object, without resolving an id first.
SESSION_PATH = '/org/freedesktop/login1/session/auto'
SESSION_INTERFACE = 'org.freedesktop.login1.Session'
LOCK = 'Lock'

PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
PROPERTIES_CHANGED = 'PropertiesChanged'


class SessionLockWatch:
    """ Turn "the session changed hands" into `on_lock`.

        One plain callable, so this class subclasses no QObject and a fake bus
        can drive it in tests. Each of the three events calls the same callback,
        and a single screen lock typically produces two of them: the callback is
        expected to be idempotent, and `LuksTray.lock_history()` is.
    """

    def __init__(self, on_lock):
        self.on_lock = on_lock
        self.watching = False
        self._bus = None
        self._subscriptions = []

    def start(self):
        """ Connect the logind signals; False when this session has none. """
        if self.watching:
            return True
        try:
            # pylint: disable=import-outside-toplevel
            from PyQt6.QtDBus import QDBusConnection
        except ImportError:
            return False
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            wanted = (
                (LOGIN1, SESSION_PATH, SESSION_INTERFACE, LOCK, self._on_lock),
                (LOGIN1, SESSION_PATH, PROPERTIES_INTERFACE,
                 PROPERTIES_CHANGED, self._on_properties),
                (LOGIN1, MANAGER_PATH, MANAGER_INTERFACE,
                 PREPARE_FOR_SLEEP, self._on_prepare_for_sleep),
            )
            accepted = [each for each in wanted if bus.connect(*each)]
        # QDBusError has no import-safe base class, so this one has to be broad.
        except Exception:                       # pylint: disable=broad-except
            return False
        self._bus = bus
        self._subscriptions = accepted
        # Any one of them is worth having: logind versions differ in which
        # events they publish, and a partly watched session still locks on the
        # ones it does. All three failing is "no events here", not an error.
        self.watching = bool(accepted)
        return self.watching

    def stop(self):
        """ Stop watching; safe to call when start() never succeeded. """
        bus, subscriptions = self._bus, self._subscriptions
        self._bus = None
        self._subscriptions = []
        self.watching = False
        if bus is None:
            return
        for subscription in subscriptions:
            with contextlib.suppress(TypeError, RuntimeError):
                bus.disconnect(*subscription)

    def _on_lock(self, *_args):
        """ The screen locked: logind's own `Lock` signal. """
        self.on_lock()

    def _on_properties(self, interface, changed, _invalidated=None):
        """ The session's `LockedHint` or `Active` changed.

            A falsy `LockedHint` and a true `Active` are the two answers that
            mean the session is still in front of its owner, so only the
            dangerous half of each pair does anything.
        """
        if interface != SESSION_INTERFACE:
            return
        try:
            locked = changed.get('LockedHint') is True
            left = changed.get('Active') is False
        except AttributeError:
            return
        if locked or left:
            self.on_lock()

    def _on_prepare_for_sleep(self, going_to_sleep=False):
        """ Suspending (or hibernating): `true` on the way down, `false` back. """
        if going_to_sleep:
            self.on_lock()
