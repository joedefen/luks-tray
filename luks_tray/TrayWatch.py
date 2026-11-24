#!/usr/bin/env python3
"""Watch for a tray host that appears or restarts after the applet."""

import contextlib

STATUS_NOTIFIER_WATCHER = 'org.kde.StatusNotifierWatcher'


def watch_mode(watcher_class):
    """Return the registration and unregistration watch flag.

    Args:
        watcher_class: `QDBusServiceWatcher` from PyQt5 or PyQt6.

    Returns:
        The combined `WatchModeFlag` value.
    """
    flag_class = getattr(watcher_class, 'WatchModeFlag', None)
    if flag_class is None:
        flag_class = watcher_class
    return (flag_class.WatchForRegistration
            | flag_class.WatchForUnregistration)


class TrayWatch:
    """Turn StatusNotifierWatcher registration events into callbacks."""

    def __init__(self, on_available, on_lost=None):
        """Store the callbacks and begin without a watcher.

        Args:
            on_available: Callable run when the watcher appears.
            on_lost: Callable run when the watcher goes away.
        """
        self.on_available = on_available
        self.on_lost = on_lost
        self.watching = False
        self._watcher = None

    def start(self):
        """Watch the session bus.

        Returns:
            bool: True when the watch was established.
        """
        if self.watching:
            return True
        try:
            # pylint: disable=import-outside-toplevel
            from PyQt6.QtDBus import QDBusConnection, QDBusServiceWatcher
        except ImportError:
            return False
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            watcher = QDBusServiceWatcher(
                STATUS_NOTIFIER_WATCHER, bus, watch_mode(QDBusServiceWatcher))
            watcher.serviceRegistered.connect(self._on_registered)
            watcher.serviceUnregistered.connect(self._on_unregistered)
        # QDBusError has no import-safe base class.
        except Exception:                       # pylint: disable=broad-except
            return False
        self._watcher = watcher
        self.watching = True
        return True

    def stop(self):
        """Stop watching; safe to call after a failed `start()`."""
        watcher, self._watcher = self._watcher, None
        self.watching = False
        if watcher is None:
            return
        for signal, slot in (
                (watcher.serviceRegistered, self._on_registered),
                (watcher.serviceUnregistered, self._on_unregistered)):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)

    def _on_registered(self, name):
        """Run the availability callback for this watcher's own name."""
        if name == STATUS_NOTIFIER_WATCHER:
            self.on_available()

    def _on_unregistered(self, name):
        """Run the loss callback for this watcher's own name."""
        if name == STATUS_NOTIFIER_WATCHER and self.on_lost is not None:
            self.on_lost()
