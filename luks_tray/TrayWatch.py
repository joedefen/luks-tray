#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Watch for a tray host that appears -- or restarts -- after the applet.

    Qt registers a ``QSystemTrayIcon`` with ``org.kde.StatusNotifierWatcher``
    once, when the item is first shown, and never retries.  A session where the
    bar comes up later than the tray -- waybar still starting, luks-tray started
    before the compositor's bar, a bar that crashed and was started again --
    therefore leaves the applet running with an item no host knows about: it is
    alive, it keeps working, and it is invisible until someone restarts it.
    Other applets in the same session (nm-applet, copyq) put their items on the
    bus again on their own, which is why they show up where luks-tray does not.

    Waiting at start-up cannot fix that: a bar that starts five minutes later is
    outside any bounded wait, and a bar that *restarts* is outside it entirely.
    The watcher's own bus name is the event instead: every time the name is
    registered, the applet is asked to register its item again.

    The trigger is ``QDBusServiceWatcher``, not a hand-made connection to
    ``NameOwnerChanged``: it is the supported API for exactly this question, and
    it reports a name registered by *another* process.

    QtDBus is imported lazily, so importing this module never opens a bus.  A
    failed watch is not fatal: an X11 bar without a StatusNotifierWatcher works
    through XEmbed and needs none of this.
"""

import contextlib

STATUS_NOTIFIER_WATCHER = 'org.kde.StatusNotifierWatcher'


def watch_mode(watcher_class):
    """ The ``WatchForRegistration | WatchForUnregistration`` flag, PyQt-agnostic.

        PyQt6 exposes the enum as ``QDBusServiceWatcher.WatchModeFlag`` while
        PyQt5 puts the same members directly on the class.  Both spellings are
        accepted here so the module -- and its tests -- do not depend on which
        Qt binding is installed.
    """
    flag_class = getattr(watcher_class, 'WatchModeFlag', None)
    if flag_class is None:
        flag_class = watcher_class
    return (flag_class.WatchForRegistration
            | flag_class.WatchForUnregistration)


class TrayWatch:
    """ Turn "the watcher's bus name was registered" into a callback.

        ``on_available`` runs when the watcher's name appears or is
        re-registered (a bar starting, or restarting); ``on_lost`` when the name
        goes away (the bar exited).  Both are plain callables, so this class
        subclasses no QObject and can be driven from a fake bus in tests.
    """

    def __init__(self, on_available, on_lost=None):
        self.on_available = on_available
        self.on_lost = on_lost
        self.watching = False
        self._watcher = None

    def start(self):
        """ Watch the session bus; False when this session cannot be watched. """
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
        # QDBusError has no import-safe base class, so this one has to be broad.
        except Exception:                       # pylint: disable=broad-except
            return False
        self._watcher = watcher
        self.watching = True
        return True

    def stop(self):
        """ Stop watching; safe to call when start() never succeeded. """
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
        """ The watcher only reports its own service, but stay defensive. """
        if name == STATUS_NOTIFIER_WATCHER:
            self.on_available()

    def _on_unregistered(self, name):
        if name == STATUS_NOTIFIER_WATCHER and self.on_lost is not None:
            self.on_lost()
