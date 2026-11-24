#!/usr/bin/env python3
"""Lock the history when the session stops being ours."""

import contextlib

LOGIN1 = 'org.freedesktop.login1'

MANAGER_PATH = '/org/freedesktop/login1'
MANAGER_INTERFACE = 'org.freedesktop.login1.Manager'
PREPARE_FOR_SLEEP = 'PrepareForSleep'

SESSION_PATH = '/org/freedesktop/login1/session/auto'
SESSION_INTERFACE = 'org.freedesktop.login1.Session'
LOCK = 'Lock'

PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
PROPERTIES_CHANGED = 'PropertiesChanged'


class SessionLockWatch:
    """Turn session lock, switch and sleep events into one callback."""

    def __init__(self, on_lock):
        """Store the callback and begin without a bus connection.

        Args:
            on_lock: Callable invoked when the session is no longer ours.
        """
        self.on_lock = on_lock
        self.watching = False
        self._bus = None
        self._subscriptions = []

    def start(self):
        """Connect the logind signals.

        Returns:
            bool: True when at least one signal was connected.
        """
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
        # QDBusError has no import-safe base class.
        except Exception:                       # pylint: disable=broad-except
            return False
        self._bus = bus
        self._subscriptions = accepted
        self.watching = bool(accepted)
        return self.watching

    def stop(self):
        """Stop watching; safe to call after a failed `start()`."""
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
        """Handle logind's `Lock` signal."""
        self.on_lock()

    def _on_properties(self, interface, changed, _invalidated=None):
        """Handle `LockedHint` and `Active` changes on the session.

        Args:
            interface: Interface that emitted the change.
            changed: Mapping of changed property names to values.
            _invalidated: Invalidated property names (unused).
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
        """Lock when the machine starts suspending.

        Args:
            going_to_sleep: True on the way down, false on resume.
        """
        if going_to_sleep:
            self.on_lock()
