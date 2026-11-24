#!/usr/bin/env python3
"""LUKS TRAY application entry point and tray orchestration."""
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments,too-many-nested-blocks
# pylint: disable=line-too-long,too-many-lines,too-many-public-methods
# pylint: disable=too-many-return-statements
import atexit
import importlib.resources
import os
import pwd
import random
import shutil
import signal
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from types import SimpleNamespace

import petname
from PyQt6.QtCore import QSocketNotifier, Qt, QTimer, QtMsgType, qInstallMessageHandler
from PyQt6.QtGui import QAction, QCursor, QFont, QFontInfo, QIcon, QSessionManager
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QWidget, QWidgetAction

from luks_tray import Compositor, Session, Utils, __version__, service
from luks_tray.AppInfo import APP_NAME, APPLICATION_DISPLAY_NAME, DESKTOP_FILE_NAME
from luks_tray.DeviceInfo import DeviceInfo
from luks_tray.dialogs import (
    CommonDialog,
    MasterPasswordDialog,
    MasterPasswordSetupDialog,
    MountDeviceDialog,
    MountFileDialog,
    OptionsDialog,
)
from luks_tray.History import HistoryClass
from luks_tray.IniTool import IniTool
from luks_tray.LockWatch import SessionLockWatch
from luks_tray.LuksOps import (
    generate_uuid_for_file_path,
    get_container_identity,
    get_luks_uuid_for_file,
    run_cmd,
    run_cmd_capture,
    run_unmount,
    sudo_cmd,
    sudo_run,
)
from luks_tray.TrayWatch import STATUS_NOTIFIER_WATCHER, TrayWatch
from luks_tray.Utils import PyKill, prt, prt_once

# The command/discovery/dialog helpers used to be module-level API of this
# file. They now live in their own modules and are re-exported here so that
# existing callers (scripts, ad-hoc harnesses) keep working unchanged.
__all__ = [
    'CommonDialog',
    'DeviceInfo',
    'HistoryClass',
    'IniTool',
    'LuksTray',
    'MasterPasswordDialog',
    'MasterPasswordSetupDialog',
    'MountDeviceDialog',
    'MountFileDialog',
    'OptionsDialog',
    'acquire_lock_or_take_over',
    'generate_uuid_for_file_path',
    'get_luks_uuid_for_file',
    'main',
    'run_cmd',
    'run_cmd_capture',
    'run_unmount',
    'sudo_cmd',
    'sudo_run',
    'tray_watcher_present',
]

# Delay before registering with a newly appeared tray host.
TRAY_ATTACH_DELAY_MS = 500

# Exit status for a login whose display is not usable yet; the unit retries it.
EXIT_NO_DISPLAY = 4

# Qt messages that are noise rather than news.
QT_NOISE = (
    'CTPlatformTheme::',
    'QWindowsWindow::',
    'QThreadStorage:',
    'Failed to register with host portal',
)
_qt_seen = set()


def qt_message_handler(mode, context, message):
    """Filter, deduplicate and log one Qt message.

    Args:
        mode: Qt message type.
        context: Qt message context.
        message: Message text.
    """
    if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
        return
    text = ' '.join(str(message).split())
    if not text or any(marker in text for marker in QT_NOISE):
        return
    if text in _qt_seen:
        return              # Qt retries the same warning on every repaint
    if len(_qt_seen) >= 64:
        _qt_seen.clear()    # bounded, like prt_once()'s set: the tray runs for weeks
    _qt_seen.add(text)
    file_name = os.path.basename(str(getattr(context, 'file', '') or ''))
    line = getattr(context, 'line', 0) or 0
    Utils.prt_qt(text, f'[{file_name}:{line}]' if file_name else '[qt]')


def install_qt_messages():
    """Send Qt console output through `prt()`."""
    qInstallMessageHandler(qt_message_handler)


# How many bytes one wakeup drain may read.
_WAKEUP_CHUNK = 64
# The pipe behind `install_signal_wakeup()`: `(notifier, read fd, write fd)`.
_signal_wakeup = None


def _drain_signal_pipe(fd):
    """ Empty the wakeup pipe; the pending handler runs as soon as we return. """
    try:
        while os.read(fd, _WAKEUP_CHUNK):
            pass
    except (BlockingIOError, OSError):
        pass


def install_signal_wakeup(app):
    """Wake the Qt event loop when a signal arrives.

    Args:
        app: The QApplication that owns the notifier.

    Returns:
        tuple: The notifier and the read and write descriptors.
    """
    global _signal_wakeup
    if _signal_wakeup is not None:
        return _signal_wakeup
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    # A pipe that is somehow full must not turn a stop signal into a write
    # error inside the C handler, so the warning is off; one wake is enough.
    signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
    notifier = QSocketNotifier(read_fd, QSocketNotifier.Type.Read, app)
    notifier.activated.connect(lambda *_: _drain_signal_pipe(read_fd))
    _signal_wakeup = (notifier, read_fd, write_fd)
    return _signal_wakeup


# Paths that must never be accepted as `auto_mount_folder`. The startup cleanup
# pass deletes (empty) subdirectories below that path, so a careless config value
# such as `/home` or `/` would otherwise turn a typo into an `rmdir` sweep over
# unrelated, system-owned paths.
FORBIDDEN_AUTO_MOUNT_ROOTS = frozenset((
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib32',
    '/lib64', '/media', '/mnt', '/opt', '/proc', '/root', '/run',
    '/sbin', '/srv', '/sys', '/tmp', '/usr', '/var',
))
# System trees that may not contain the vault root.
FORBIDDEN_AUTO_MOUNT_PREFIXES = tuple(os.path.normpath(path) for path in (
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib32', '/lib64', '/opt',
    '/proc', '/root', '/run', '/sbin', '/srv', '/sys', '/usr', '/var',
))


class LuksTray:
    """The tray icon, its menu, and the refresh and mount policy."""
    svg_info = SimpleNamespace(version='04', bases=[
                            'white-shield',
                            'alert-shield',
                            'green-shield',
                            'orange-shield',
                        ],
                        nicknames=[
                            'none',
                            'alert',
                            'ok',
                            'locked',
                        ] )

    def __init__(self, ini_tool, opts):
        """Build the tray, its icons and its timers.

        Args:
            ini_tool: Configuration and path helper.
            opts: Parsed command-line options.
        """
        self.uid = os.environ.get('SUDO_UID', os.getuid())
        self.gid = os.environ.get('SUDO_GID', os.getgid())

        self.ini_tool = ini_tool
        self.app = QApplication([])
        # A stable Wayland app_id is the trait compositor rules match on: it
        # is what makes one `window-rule`/`for_window` cover the dialogs, the
        # error boxes and the file pickers instead of guessing at titles.
        # Measured on Qt 6.12: only `setDesktopFileName()` yields `luks-tray`
        # here, dropping it falls back to the executable name (`python3` for
        # `./runner`).  Setting it also makes Qt register that id with
        # xdg-desktop-portal, which is refused because no desktop entry ships;
        # that one line is filtered in QT_NOISE.
        QApplication.setApplicationName(APP_NAME)
        QApplication.setApplicationDisplayName(APPLICATION_DISPLAY_NAME)
        QApplication.setDesktopFileName(DESKTOP_FILE_NAME)
        self.app.setQuitOnLastWindowClosed(False)
        # On a Qt with X11 session management a logout *asks* the applet to
        # leave, and this is where it answers.  Where Qt has none the applet is
        # terminated instead, and the unit's Restart=* keeps it down: the
        # handler is the second line of defence either way -- see
        # install_session_shutdown_handler().
        self.install_session_shutdown_handler()
        # A stop is a signal, and a signal reaches its Python handler only when
        # the event loop gives the interpreter control again -- on an idle tray,
        # at the next 3 s poll.  This makes that immediate; see
        # install_signal_wakeup().
        install_signal_wakeup(self.app)
        self.mono_font = QFont("Consolas", 10)
        self.mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self.emoji_font = self.get_emoji_font()
        self.app.setFont(self.mono_font)

        _, missing = self.check_dependencies()

        if missing:
            # Was a bare `assert`: `python -O` strips it, so the tray carried
            # on with half the system commands missing (no cryptsetup, no
            # mkfs.ext4, ...) and failed later in confusing ways.  The PATH is
            # in the message because the lookup is `shutil.which()`: the usual
            # cause is not a missing package but a unit or a service manager
            # whose PATH omits /usr/sbin, where cryptsetup/losetup/mkfs.ext4
            # live on a non-usrmerged or Debian-style host.
            raise RuntimeError(f'missing system commands: {missing} '
                               f'(PATH={os.environ.get("PATH", "")})')

        # Create an invisible base widget to serve as the dialog parent
        self.dialog_parent = QWidget(None) # Parented by None, so it's top-level
        self.dialog_parent.setWindowFlags(Qt.WindowType.Tool) # Hint to WM it's a utility window
        self.dialog_parent.hide() # Keep it invisible


        self.history = HistoryClass(ini_tool.history_path)
        self.history.restore()

        self.icons, self.svgs = {}, {}
        self.prev_icon_key = ''

        for idx, base in enumerate(self.svg_info.bases):
            key = self.svg_info.nicknames[idx]
            self.svgs[key] = f'{base}-v{self.svg_info.version}.svg'

        for key, resource_filename in self.svgs.items():
            dest_path = os.path.join(ini_tool.folder, resource_filename)
            if not os.path.isfile(dest_path):
                try:
                    with importlib.resources.as_file(
                        importlib.resources.files('luks_tray.resources').joinpath(resource_filename)
                    ) as source_path:
                        # as_file() hands back a real path (or a temporary
                        # copy of it), so a plain copy2 is all that is needed.
                        shutil.copy2(source_path, dest_path)
                except (FileNotFoundError, AttributeError):
                    prt(f'WARN: cannot find source resource {repr(resource_filename)}')
                    continue

            if not os.path.isfile(dest_path):
                prt(f'WARN: cannot find destination file {repr(dest_path)}')
                continue

            self.icons[key] = QIcon(dest_path)

        missing_icons = sorted(set(self.svgs) - set(self.icons))
        if missing_icons:
            # Same reasoning as the dependency check above: this `assert`
            # vanished under `python -O`, leaving the tray half-initialized
            # and every later `self.icons[key]` lookup a KeyError.
            raise RuntimeError(f'missing tray icon(s): {missing_icons}')


        self.lsblk = DeviceInfo(opts=opts, tray=self)
        self.mount_infos = {}
        self.upons = set()

        self.containers, self.menu = {}, QMenu()
        self.actions = []
        # Guards for the 3 s poller, see update_menu() and busy_operation().
        # Both must exist before the first update_menu() call below.
        self.refreshing = False
        self.operation = None

        # The tray item is shown immediately; a host that is not on the bus yet
        # is picked up by the watch below instead of being waited for.  `menu`
        # exists first, so re-registering the item can restore it, and the watch
        # starts *before* the item is shown: a watcher that appears between the
        # two still produces the event we need.
        tray_present = tray_watcher_present()
        self._tray_seen = tray_present is True
        self.tray_watch = TrayWatch(on_available=self._on_tray_host_appeared,
                                    on_lost=self._on_tray_host_lost)
        if not self.tray_watch.start() and tray_present is not None:
            prt('WARN: cannot watch for a tray host that starts later')
        # The history locks when the session stops being ours: the screen
        # locking, a VT or user switch, a suspend. Best effort on purpose -- a
        # bare WM publishes none of these, and losing the events must not cost
        # the applet its start-up. There is deliberately no idle timer behind
        # it: an idle guess cannot see a screen lock either, so it would add
        # prompts to the ordinary case without covering the one that matters.
        self.lock_watch = SessionLockWatch(on_lock=self.lock_history)
        if not self.lock_watch.start():
            prt('Note: this session publishes no lock events; the master'
                ' password stays in memory until the applet exits.')
        self._install_tray_icon()
        if tray_present is True:
            prt('tray host detected.')
        elif tray_present is False:
            prt('no tray host yet; the icon registers when one appears.')
        else:
            prt('tray host status unknown (no QtDBus / no session bus).')

        self.update_menu()
        self.remove_unused_automounts()

        # Deliberately unparented: re-registering the item deletes the old
        # QSystemTrayIcon, and a timer parented to it would go with it.
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_menu)
        # The same tick retries a host announcement that was missed, so a watch
        # that cannot fire (no QtDBus) still gets the icon registered later.
        self.timer.timeout.connect(self._ensure_tray_attached)
        # Repeating, but update_menu() self-guards: a tick that lands while a
        # refresh or a mount/unmount is still running is dropped instead of
        # stacking a second refresh on top of it.
        self.timer.start(3000)  # 3000 milliseconds = 3 seconds

    @staticmethod
    def check_dependencies(verbose=False):
        """Return `(found, missing)` for the commands the tray runs.

        Args:
            verbose: Print every checked command.

        Returns:
            tuple: Found and missing command names.
        """
        utilities = [
            'sudo', 'lsblk', 'blkid', 'cryptsetup', 'losetup',
            'mkdir', 'rmdir', 'touch', 'chown',
            'mount', 'umount', 'bindfs', 'fuser',
            'truncate', 'mkfs.ext4',
        ]
        found, missing = [], []
        for util in utilities:
            (found if shutil.which(util) else missing).append(util)
        if verbose or missing:
            for util in found:
                prt(f'✓  {util}')
            for util in missing:
                prt(f'✗  {util} - please install')
            if missing:
                # The one line that separates "not installed" from "installed
                # somewhere this process does not look".
                prt(f'   PATH={os.environ.get("PATH", "")}')
        return found, missing

    @staticmethod
    def get_emoji_font(size=10):
        """Return a font that can render color emoji.

        Args:
            size: Point size.

        Returns:
            QFont: The best available emoji font.
        """
        preferred_fonts = [
            "Noto Color Emoji",         # Linux standard
            "Segoe UI Emoji",           # Windows
            "Apple Color Emoji",        # macOS
            "Symbola",                  # B&W fallback
            "EmojiOne Color",           # Older option
        ]

        for font_name in preferred_fonts:
            font = QFont(font_name, size)
            resolved_family = QFontInfo(font).family()
            if resolved_family == font_name:
                return font


        prt("Warning: No known emoji font found — emojis likely degraded.")
        return QFont()  # system default

    def update_mounts(self):
        """Re-read `/proc/mounts` into `mount_infos` and `upons`.

        Returns:
            set: The mounted device keys.
        """
        self.mount_infos = {}
        self.upons = set()
        with open('/proc/mounts', encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    device = parts[0]
                    mount_point = parts[1]
                    options = parts[3]
                    readonly = 'ro' in options.split(',')
                    self.mount_infos[device] = SimpleNamespace(
                                upon=mount_point, readonly=readonly)
                    self.upons.add(mount_point)
        return set(self.mount_infos.keys())

    def is_mounted(self, thing):
        """Return True if `thing` is a mounted device or mount point."""
        return thing in self.mount_infos or thing in self.upons

    def merge_containers_history(self):
        """Make the history agree with the containers just discovered."""
        self.history.restore()
        for container in self.containers.values():
            self.history.ensure_container(container)
        self.history.save()

    def identify_file_containers(self):
        """Re-key loop-backed containers by the container inside the file."""
        identified = {}
        for uuid, container in self.containers.items():
            identity = ''
            if not uuid and container.back_file:
                identity = get_container_identity(container.back_file)
            if not identity:
                # Nothing to improve on: a container with a UUID keeps it, and
                # one whose file cannot be read stays visible under its own key
                # rather than disappearing from the menu.
                identified[uuid] = container
                continue
            container.uuid = identity
            identified[identity] = container
        self.containers = identified

    def history_file_container(self, vital):
        """Return the stand-in container for a history entry lsblk missed.

        Args:
            vital: The history record.

        Returns:
            SimpleNamespace: The container record.
        """
        container = DeviceInfo.make_partition_namespace('', '')
        container.type = 'crypt'
        container.back_file = vital.back_file
        container.uuid = vital.uuid
        container.meta_type = getattr(vital, 'meta_type', '')
        container.vital = vital
        container.name = f'{os.path.basename(vital.back_file)}-luks'
        container.opened = os.path.exists(f'/dev/mapper/{container.name}')
        mount = self.mount_infos.get(f'/dev/mapper/{container.name}')
        if container.opened and mount:
            container.upon = mount.upon
            container.mounts = [mount.upon]
            container.readonly = mount.readonly
        return container

    def update_menu(self):
        """Refresh device and history state, then rebuild the menu."""
        if self.refreshing or self.operation:
            return
        self.refreshing = True
        try:
            self.ini_tool.update_config()
            if self.history.status in ('unlocked', 'clear_text'):
                self.update_mounts()
                self.containers = self.lsblk.parse_lsblk()
                self.identify_file_containers()
                self.merge_containers_history()
                # add in the containers that are not mounted but in
                # the history
                for vital in self.history.vitals.values():
                    container = self.containers.get(vital.uuid, None)
                    if container:
                        container.vital = vital
                        if getattr(vital, 'meta_type', ''):
                            container.meta_type = vital.meta_type
                    if not container and vital.back_file:
                        # insert known file container (present device
                        # containers should be in the containers list already)
                        self.containers[vital.uuid] = (
                                self.history_file_container(vital))

            self.update_menu_items()
        finally:
            self.refreshing = False

    @contextmanager
    def busy_operation(self, name):
        """Suspend polling while a privileged dialog action runs.

        Args:
            name: Operation name recorded while the block runs.

        Yields:
            None: Control while the operation is in flight.
        """
        previous, self.operation = self.operation, name
        try:
            yield
        finally:
            self.operation = previous

    def update_menu_items(self):
        """Build the menu entries and compare them with the current menu."""
        actions = []

        # The icon key is the state the shield reports: the containers decide it
        # in the loop below, and the locked-history branch overrides the default.
        icon_key = 'none'
        do_alerts = self.ini_tool.get_current_val('show_anomaly_alerts')

        if self.history.status == 'locked':
            # Orange, not white. White claims every container is locked, and that
            # is the one claim this state cannot make: an encrypted history that
            # cannot be opened hides every container, so the state is unknown
            # rather than safe. The single menu entry asks for the password.
            icon_key = 'locked'
            action = QAction('✎ Click to enter master password', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.prompt_master_password)
            actions.append(action)
        else:
            separated = False
            idx = -1
            for idx, container in enumerate(self.containers.values()):
                # Two forms of the same path: `upon_path` decides the symbol
                # ('/'-prefixed means mounted), `mountpoint` is what gets shown.
                # A mount point that is only remembered is bracketed -- and has
                # never been '/'-prefixed either, so it reads as closed.
                upon_path = container.upon
                if upon_path:
                    mountpoint = LuksTray.shorten_home(upon_path)
                elif container.vital and container.vital.upon:
                    mountpoint = f'[{LuksTray.shorten_home(container.vital.upon)}]'
                else:
                    mountpoint = ''

                if idx > 0 and not separated and container.type == 'crypt':
                    actions.append(None)
                    separated = True

                # Both halves of the line are abbreviated the same way: the
                # menu is a list, not a place for `/home/<user>` twice per row.
                name = container.name
                if container.back_file:
                    name = LuksTray.shorten_home(container.back_file)

                # Determine which emoji/symbol to show
                if upon_path.startswith('/'):
                    emoji = '⧈' if container.readonly else '▣'
                    icon_key = 'ok' if icon_key != 'alert' else icon_key
                elif container.opened:
                    emoji = '‼'
                    icon_key = 'alert' if do_alerts else icon_key
                else:
                    emoji = '▽'


                # Construct menu line text. `mountpoint` is empty for a
                # container that is closed and was never mounted here, and a
                # trailing space there looks like a truncated row.
                if emoji == '‼':
                    text = f'{name} CLICK-to-LOCK'
                else:
                    text = f'{name} {mountpoint}'.rstrip()


                # Add the emoji-enhanced item; the left click opens the
                # mount/close dialog for this container.
                action = QAction(f'{emoji} {text}', self.app)
                action.setFont(self.mono_font)  # applies to non-HTML paths
                if container.back_file:
                    action.triggered.connect(lambda checked,
                                 x=container.uuid: self.handle_file_click(x))
                else:
                    action.triggered.connect(lambda checked,
                                 x=container.uuid: self.handle_device_click(x))
                actions.append(action)

            # Other fixed menu entries
            if idx > 0 and not separated:
                actions.append(None)

            action = QAction('✦ Create New Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_create_file_click)
            actions.append(action)

            action = QAction('✚ Add Existing Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_add_file_click)
            actions.append(action)

        # The tail below is what both states share: the log, if anything can
        # show it, and the two entries that end every menu. The settings entries
        # are *not* shared: a locked history is the one state where the master
        # password can be neither proved nor rewritten, so its branch above
        # offers the password prompt and that is all it offers.
        def separate():
            """ Start a new group, without stacking two separators. """
            if actions and actions[-1] is not None:
                actions.append(None)

        separate()

        if self.history.status != 'locked':
            action = QAction('⚙ Options…', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_options_click)
            actions.append(action)

            # One entry, and its wording is the state: with no password the only
            # meaningful action is to set one, with one the only meaningful
            # action is to change or clear it. It sits *below* Options rather
            # than inside it -- the key that guards the whole file is not one of
            # the file's settings, and a submenu would put the action two clicks
            # away from the menu that exists to hold it.
            action = QAction(self.master_password_entry_text(), self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_master_password_click)
            actions.append(action)

        if self.log_viewer_available():
            action = QAction(self.log_entry_text(), self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.open_log)
            actions.append(action)

        separate()

        action = QAction('☓ Exit', self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.exit_app)
        actions.append(action)

        # The version rides on the restart entry instead of a separate dead
        # label: the log says the same thing at start-up, but "which build am I
        # running?" is a question asked from the menu, usually about a desktop
        # that behaves differently from what the reader expects.
        label = f'↺ Restart luks-tray v{__version__}'
        if self.history.status == 'unlocked':
            # A restart *is* a lock -- the password lives in memory only -- so
            # it is also the manual way to lock without leaving. Said only while
            # there is something to lock: a clear-text history has no key, and a
            # locked one is already locked.
            label += ' (locks history)'
        action = QAction(label, self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.restart_self)
        actions.append(action)

        return self.replace_menu_if_different(actions, icon_key)


    def replace_menu_if_different(self, actions, icon_key):
        """Install `actions` on the tray menu only when they changed.

        Args:
            actions: Actions and separators for the menu.
            icon_key: Icon state key.

        Returns:
            bool: True when the menu was replaced.
        """
        def replace_menu():
            """Install `actions` on the menu and refresh the tray icon."""
            nonlocal actions
            was_visible = self.menu and self.menu.isVisible()

            self.menu.clear()
            for action in actions:
                if action:
                    self.menu.addAction(action)
                else:
                    self.menu.addSeparator()
            self.actions = actions

            self.tray_icon.setIcon(self.icons[icon_key])
            self.tray_icon.setContextMenu(self.menu)
            self.tray_icon.show()

            # Reopen menu if it was previously open
            if was_visible:
                # Show menu at cursor position
                cursor_pos = QCursor.pos()
                self.menu.popup(cursor_pos)

            return True

        def get_action_text(action):
            """ Comparable text for an action, a widget action or a separator. """
            if action is None:
                return '<None>'
            if isinstance(action, QWidgetAction):
                widget = action.defaultWidget()
                if isinstance(widget, QLabel):
                    return widget.text()
                return '<widget>'
            return action.text()

        if not self.actions:
            return replace_menu()
        if self.prev_icon_key != icon_key:
            self.prev_icon_key = icon_key
            return replace_menu()

        if len(actions) != len(self.actions):
            return replace_menu()

        for idx, action in enumerate(actions):
            old_action = self.actions[idx]
            if get_action_text(action) != get_action_text(old_action):
                return replace_menu()

        return False


    def handle_device_click(self, uuid):
        """Open the mount dialog for a device container."""
        if uuid in self.containers:
            dialog = MountDeviceDialog(self, self.containers[uuid])
            dialog.exec()

    def handle_file_click(self, uuid):
        """Open the mount dialog for a file container."""
        if uuid in self.containers:
            dialog = MountFileDialog(self, self.containers[uuid])
            dialog.exec()

    def handle_add_file_click(self):
        """ Open the "add existing crypt file" dialog. """
        dialog = MountFileDialog(self, None)
        dialog.exec()

    def handle_create_file_click(self):
        """ Open the "create new crypt file" dialog. """
        dialog = MountFileDialog(self, None, create=True)
        dialog.exec()

    def handle_options_click(self):
        """Open the options dialog."""
        if self.history.status == 'locked':
            self.prompt_master_password()
            return
        dialog = OptionsDialog(self)
        dialog.exec()

    def master_password_entry_text(self):
        """ The master-password menu line, worded by the state it is in. """
        if self.history.status == 'clear_text':
            return '✎ Create Master Password…'
        return '✎ Update Master Password…'

    def handle_master_password_click(self):
        """Open the create-or-change dialog for the master password."""
        if self.history.status == 'locked':
            self.prompt_master_password()
            return
        dialog = MasterPasswordSetupDialog(
            self, create=self.history.status == 'clear_text')
        dialog.exec()

    def log_viewer_available(self):
        """ True when the log can be shown: a terminal, or the configured one. """
        return bool(Utils.terminal_argv(
            [], self.ini_tool.get_current_val('log_viewer') or ''))

    def log_entry_text(self):
        """Return the "Open Log" label for the current failure count."""
        problems = Utils.session_problems()
        if not problems:
            return '≡ Open Log'
        return f'≡ Open Log [{problems} error{"" if problems == 1 else "s"}]'

    def open_log(self):
        """Page `debug.log` and its rotated generation in a terminal."""
        preferred = self.ini_tool.get_current_val('log_viewer') or ''
        arguments = Utils.log_viewer_argv(self.ini_tool.log_path,
                                          self.ini_tool.log_old_path,
                                          preferred)
        if not arguments:
            prt('ERROR: no terminal emulator found; set ui.log_viewer in'
                ' config.ini (for example "foot -e") or install one')
            return
        prt('+', 'running:', arguments)
        try:
            Utils.spawn_detached(arguments, quiet=True)
        except (OSError, subprocess.SubprocessError) as exce:
            prt(f'ERROR: cannot open the log: {exce}')

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

    def restart_self(self, *_args):
        """Replace this process with a fresh applet instance."""
        if service.supervised_here():
            self._restart_via_systemd()
            return
        prt('+', 'restarting applet...')
        try:
            self.tray_icon.hide()
        except (AttributeError, RuntimeError) as exc:
            prt(f'note: cannot hide the tray icon before restarting: {exc}')
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            [package_root] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
        # --replace is never inherited: a restart releases the lock first, so
        # the replacement has nothing to take over, and it must not keep a
        # destructive flag should the lock change hands in between.
        inherited = [arg for arg in sys.argv[1:] if arg != '--replace']
        args = [sys.executable, '-m', 'luks_tray.Main', *inherited]
        lock = Utils.SingleInstance.current
        Utils.SingleInstance.release_current()
        try:
            subprocess.Popen(args, env=env)
        except OSError as exc:
            prt(f'FAIL: cannot restart the applet: {exc}')
            if lock is not None:
                lock.acquire()
            return
        # os._exit() skips atexit handlers, so the stop line is written here --
        # the replacement starts its own session banner a moment later.
        Utils.log_session_stop(APP_NAME, __version__, note=' (restarting)')
        os._exit(0)

    def _restart_via_systemd(self):
        """Ask systemd to restart this unit and wait until the job is queued."""
        prt('+', 'restarting applet via systemd...')
        try:
            status = service.request_restart()
        except (service.ServiceError, OSError) as exc:
            prt(f'FAIL: cannot restart the unit: {exc}')
            Utils.error_print(f'FAIL: cannot restart the unit: {exc}')
            return
        if status != 0:
            message = f'FAIL: systemctl refused the restart request (status {status})'
            prt(message)
            Utils.error_print(message)
            return
        try:
            self.tray_icon.hide()
        except (AttributeError, RuntimeError) as exc:
            prt(f'note: cannot hide the tray icon before restarting: {exc}')
        self.app.quit()

    # -- a session that is ending ------------------------------------------

    def install_session_shutdown_handler(self):
        """Answer a session manager that asks the applet to leave."""
        try:
            self.app.commitDataRequest.connect(self._on_commit_data_request)
        except (AttributeError, TypeError) as exc:
            prt(f'note: no session management in this Qt: {exc}')

    def _on_commit_data_request(self, manager):
        """Quit without being restored when the session manager ends the session."""
        prt('session manager is ending the session; quitting')
        try:
            manager.setRestartHint(QSessionManager.RestartHint.RestartNever)
        except (AttributeError, RuntimeError) as exc:
            prt(f'note: cannot set the session restart hint: {exc}')
        self.app.quit()

    def lock_history(self, reason='session event'):
        """Forget the master password and the decrypted vitals.

        Args:
            reason: Event that triggered the lock.

        Returns:
            bool: True when this call locked a readable history.
        """
        if not self.history.lock():
            return False
        prt(f'history locked ({reason}).')
        self.update_menu()
        return True

    def prompt_master_password(self):
        """Ask for the master password that unlocks the history."""
        dialog = MasterPasswordDialog(self)
        dialog.exec()

    # -- tray registration -------------------------------------------------

    def current_icon_key(self):
        """ The icon key in use, or 'none' before the first update_menu(). """
        key = getattr(self, 'prev_icon_key', '')
        return key if key in self.icons else 'none'

    def _install_tray_icon(self):
        """Create the tray item and show it so a host registers it."""
        previous = getattr(self, 'tray_icon', None)
        if previous is not None:
            previous.hide()
            previous.deleteLater()
        self.tray_icon = QSystemTrayIcon(self.icons[self.current_icon_key()],
                                         self.app)
        self.tray_icon.setToolTip('luks-tray')
        if self.menu is not None:
            self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.setVisible(True)

    def _on_tray_host_appeared(self):
        """ A host appeared (or restarted): register the item with it again. """
        self._tray_seen = True
        # A watcher claims its bus name just before it starts serving calls, so
        # give it a moment instead of racing its own start-up.
        QTimer.singleShot(TRAY_ATTACH_DELAY_MS, self._reattach_tray)

    def _on_tray_host_lost(self):
        """ The host went away; the registration went with it. """
        self._tray_seen = False

    def _reattach_tray(self):
        """ Re-create the item at last, if a host is really there now. """
        if tray_watcher_present() is not True:
            # The host left again between its announcement and this call; the
            # next appearance schedules another attempt.
            self._tray_seen = False
            return
        self._install_tray_icon()
        prt('tray icon registered with the host that is now running')

    def _ensure_tray_attached(self):
        """ Retry a missed host announcement from the periodic 3 s tick. """
        watch = getattr(self, 'tray_watch', None)
        if getattr(self, '_tray_seen', True) or watch is None or not watch.watching:
            return
        if tray_watcher_present():
            self._on_tray_host_appeared()

    def update_history(self, uuid, values, back_file=None, meta_type=None):
        """Record or refresh the history entry for a container.

        Args:
            uuid: Container identity.
            values: Form values, including `upon` and `password`.
            back_file: Backing file for a file container.
            meta_type: Header type that opened the container.
        """
        vital = self.history.get_vital(uuid)
        if uuid:
            vital.uuid = uuid
        mount_point = values['upon']
        if not hasattr(vital, 'when'):
            vital.when = 0
        dirty = False
        if back_file is not None and vital.back_file != back_file:
            vital.back_file = back_file
            dirty = True
        if meta_type and getattr(vital, 'meta_type', '') != meta_type:
            vital.meta_type = meta_type
            dirty = True
        if (values['password'] != vital.password or mount_point != vital.upon
                or time.time() - 24*3600 >= vital.when):
            vital.password = values['password']
            dirty = True
        if mount_point and mount_point != vital.upon:
            vital.upon = mount_point   # set before saving, so it is persisted
            dirty = True
        if dirty:
            self.history.put_vital(vital)

    def get_auto_mount_root(self):
        """Return the configured `auto_mount_folder` as an absolute path.

        Returns:
            str: The vault root.
        """
        configured = self.ini_tool.get_current_val('auto_mount_folder')
        auto_root = LuksTray.expand_real_user(configured)
        if not os.path.isabs(auto_root):
            fallback = self.ini_tool.the_default('auto_mount_folder')
            prt_once(f'auto_mount_folder {configured!r}',
                     f'WARN: auto_mount_folder {configured!r} is not an '
                     f'absolute path; using {fallback!r} instead')
            auto_root = LuksTray.expand_real_user(fallback)
        return os.path.abspath(auto_root)

    @staticmethod
    def get_real_user_ids():
        """Return `(uid, gid)` of the user this tray acts for.

        Returns:
            tuple: The user and group ids.
        """
        def as_int(value, fallback):
            """ Coerce an environment id to int, warning on garbage. """
            try:
                return int(value)
            except (TypeError, ValueError):
                # None is the normal case (not running under sudo); anything
                # else means the environment holds something unexpected, which
                # silently changes whose files the tray thinks it manages.
                if value is not None:
                    prt_once(f'bad id {value!r}',
                             f'WARN: ignoring non-numeric id {value!r}, using {fallback!r}')
                return int(fallback)

        return (as_int(os.environ.get('SUDO_UID'), os.getuid()),
                as_int(os.environ.get('SUDO_GID'), os.getgid()))

    @staticmethod
    def get_real_user_home():
        """Return the home directory of the user this tray acts for.

        Returns:
            str: The home directory.
        """
        real_user = os.environ.get('SUDO_USER')
        if real_user:
            try:
                return pwd.getpwnam(real_user).pw_dir
            except KeyError:
                # SUDO_USER names an account that passwd does not know: guessing
                # /home/<name> is a last resort and the user should hear about
                # it, since every path derived from it will be a guess too.
                prt_once(f'no passwd entry for {real_user}',
                         f'WARN: SUDO_USER={real_user!r} has no passwd entry;'
                         f' assuming /home/{real_user}')
                return os.path.join('/home', real_user)
        return os.path.expanduser('~')

    @staticmethod
    def validate_auto_mount_root(auto_root):
        """Return None if `auto_root` may be cleaned, else an error string.

        Args:
            auto_root: Candidate vault root.

        Returns:
            str | None: The refusal reason, or None when acceptable.
        """
        try:
            path = os.path.abspath(auto_root)
        except Exception as exc:                       # pylint: disable=broad-except
            return f'auto_mount_folder {auto_root!r} is not a usable path: {exc}'

        if not os.path.isabs(path):
            return f'auto_mount_folder {path!r} is not an absolute path'
        if path in FORBIDDEN_AUTO_MOUNT_ROOTS:
            return f'auto_mount_folder {path!r} is a system directory'
        if len([part for part in path.split(os.sep) if part]) < 2:
            return f'auto_mount_folder {path!r} is too close to the filesystem root'
        if os.path.islink(path):
            # A symlinked root can point anywhere; never follow it for deletes.
            return f'auto_mount_folder {path!r} is a symlink'

        home = os.path.abspath(LuksTray.get_real_user_home())
        if path == home:
            return (f'auto_mount_folder {path!r} is the home directory itself '
                    f'(not a dedicated vault area)')
        if home.startswith(path.rstrip(os.sep) + os.sep):
            return (f'auto_mount_folder {path!r} contains the home directory '
                    f'{home!r} (shared folder, not a dedicated vault area)')

        within_home = LuksTray.is_strictly_within(path, home)
        if not within_home:
            for prefix in FORBIDDEN_AUTO_MOUNT_PREFIXES:
                if path == prefix or path.startswith(prefix + os.sep):
                    return (f'auto_mount_folder {path!r} is inside the system '
                            f'directory {prefix!r}')
            # `~/Vaults` lives under /home, which is why /home is not in the
            # prefix list -- so any multi-user home tree is checked explicitly:
            # a path below one of them must belong to *our* home (e.g.
            # /home/<other-user>/Vaults is not ours to clean). The parents of
            # `/` and `/` itself are dropped: "within /" says nothing.
            home_roots = {os.path.dirname(home), '/home'} - {'/', ''}
            for home_root in sorted(home_roots):
                if LuksTray.is_strictly_within(path, home_root):
                    return (f'auto_mount_folder {path!r} is inside another '
                            f'user\'s home directory ({home_root!r})')

        if not os.path.exists(path):
            return None            # nothing to clean yet -- harmless

        if not os.path.isdir(path):
            return f'auto_mount_folder {path!r} exists but is not a directory'
        if os.path.ismount(path):
            return f'auto_mount_folder {path!r} is a mount point'
        try:
            stat_info = os.stat(path)
        except OSError as exc:
            return f'auto_mount_folder {path!r} cannot be inspected: {exc}'

        real_uid, _real_gid = LuksTray.get_real_user_ids()
        # root is accepted on purpose: the tray runs under sudo and creates the
        # folder itself, so its owner is either the real user or root.
        if stat_info.st_uid not in (real_uid, os.geteuid(), 0):
            return (f'auto_mount_folder {path!r} is owned by uid '
                    f'{stat_info.st_uid}, not by the real user (uid {real_uid}) '
                    f'or root')
        if stat_info.st_mode & 0o002:
            return (f'auto_mount_folder {path!r} is writable by other '
                    f'(shared directory)')
        return None

    @staticmethod
    def repair_auto_mount_root(auto_root):
        """Make an accepted, user-owned vault root owner-only.

        Args:
            auto_root: Vault root to tighten.

        Returns:
            bool: True when the mode was changed.
        """
        if LuksTray.validate_auto_mount_root(auto_root):
            return False
        try:
            stat_info = os.stat(auto_root)
        except OSError:
            return False
        if not stat_info.st_mode & 0o020:
            return False                # Not group-writable: nothing to do
        real_uid, _real_gid = LuksTray.get_real_user_ids()
        if stat_info.st_uid not in (real_uid, os.geteuid()):
            return False                # Not ours to tighten
        try:
            os.chmod(auto_root, 0o700)
        except OSError as exc:
            prt_once(f'chmod {auto_root}',
                     f'note: cannot make {auto_root!r} owner-only: {exc}')
            return False
        prt(f'note: made auto_mount_folder {auto_root!r} owner-only '
            f'(it was group-writable)')
        return True

    @staticmethod
    def ensure_auto_mount_root(auto_root):
        """Make the auto-mount root exist and belong to the real user.

        Args:
            auto_root: Vault root to create or repair.

        Returns:
            str | None: The failure text, or None on success.
        """
        if not os.path.isdir(auto_root):
            # The mode is part of the policy this folder has to pass: leaving it
            # to the umask made `mkdir -p` create a 0775 root on the common
            # `umask 0002` desktop, which the cleanup then refused to work on.
            # `-m` modes the leaf directory, which is the root being created.
            return run_cmd(['mkdir', '-p', '-m', '0700', auto_root])
        real_uid, real_gid = LuksTray.get_real_user_ids()
        try:
            owner_uid = os.stat(auto_root).st_uid
        except OSError as exc:
            return f'FAIL: cannot inspect {auto_root!r}: {exc}'
        if owner_uid == real_uid:
            # Being the user's is enough to use it; a group-writable mode left
            # by an older `mkdir` is tightened in place (and logged) instead of
            # being reported as a save-time error.
            LuksTray.repair_auto_mount_root(auto_root)
            return None
        problem = LuksTray.validate_auto_mount_root(auto_root)
        if problem:
            return f'FAIL: not repairing ownership of {auto_root!r}: {problem}'
        return sudo_cmd(['chown', f'{real_uid}:{real_gid}', auto_root])

    def generate_auto_mount_folder(self):
        """Return a new, unused mount point below the auto-mount root.

        Returns:
            str: The generated mount point.
        """

        def available(basename):
            """ Full path for `basename`, or None if it is already taken. """
            nonlocal auto_root
            fullpath = os.path.join(auto_root, basename)
            if fullpath in self.upons or fullpath in self.history.upons:
                return None
            return fullpath

        auto_root = self.get_auto_mount_root()
        if os.path.exists(auto_root) and not os.path.isdir(auto_root):
            # `assert` is stripped by `python -O`, and the caller would then
            # hand out mount points inside a non-directory.
            raise RuntimeError(
                f'auto_mount_folder ({auto_root!r}) exists but is not a directory')
        err = LuksTray.ensure_auto_mount_root(auto_root)
        if err:
            # Handing out mount points inside a directory that could not be
            # created only moves the failure to `mount`, with a message that no
            # longer mentions the configured folder.
            raise RuntimeError(
                f'cannot create auto_mount_folder ({auto_root!r}): {err}')

        for loop in range(30):
            full = petname.Generate(2, separator='_')   # e.g., 'stoic_turing'
            short = full.split('_')[1]              # 'turing'
            if (fullpath := available(short)):
                return fullpath
            if loop >= 20 and (fullpath := available(full)):
                return fullpath
        # fallback to simple numeric suffix
        for _ in range(10000):
            fallback = f'vault{random.randint(1000, 9999)}'
            if (fullpath := available(fallback)):
                return fullpath
        raise RuntimeError('cannot generate automount directory (too many in use)')

    @staticmethod
    def is_strictly_within(child, parent):
        """ True if `child` is inside `parent` (and not `parent` itself). """
        try:
            child = os.path.abspath(child)
            parent = os.path.abspath(parent)
        except (TypeError, ValueError, OSError) as exc:
            # The conservative answer stays False, but log it: the caller uses
            # this to decide whether a directory may be deleted.
            prt_once(f'is_strictly_within {child!r} {parent!r}',
                     f'WARN: cannot compare {child!r} with {parent!r}: {exc}')
            return False
        if child == parent:
            return False
        return child.startswith(parent.rstrip(os.sep) + os.sep)

    @staticmethod
    def remove_empty_dir(path):
        """Remove `path` when it is an empty, plain directory.

        Args:
            path: Directory to remove.

        Returns:
            bool: True when it was removed.
        """
        if os.path.islink(path) or not os.path.isdir(path):
            return False
        if os.path.ismount(path):
            return False
        try:
            os.rmdir(path)              # only removes empty dirs
            return True
        except PermissionError:
            err = sudo_cmd(['rmdir', path])
            if err:
                prt_once(f'rmdir {path}', f'note: not removing {path!r}: {err}')
            return err is None
        except OSError as exc:
            # Callers only pass folders that were empty a moment ago, so this is
            # a race (something appeared inside it in between) rather than the
            # normal "directory not empty" -- report each path once instead of
            # failing invisibly.
            prt_once(f'rmdir {path}', f'note: not removing {path!r}: {exc}')
            return False

    @staticmethod
    def is_empty_dir(path):
        """ True if `path` is a directory holding no entries at all. """
        try:
            with os.scandir(path) as entries:
                return next(entries, None) is None
        except OSError as exc:
            # "Cannot tell" is the same answer as "not empty" for both callers
            # (nothing gets deleted), but swallowing it made a permission
            # problem look exactly like a normal in-use vault directory.
            prt_once(f'scandir {path}', f'note: cannot list {path!r}: {exc}')
            return False

    def remove_if_auto(self, mount):
        """Remove `mount` when it is an empty directory below the vault root."""
        parent_dir = self.get_auto_mount_root()
        target_dir = os.path.abspath(mount)

        if not LuksTray.is_strictly_within(target_dir, parent_dir):
            return False  # Not safely within the parent
        problem = LuksTray.validate_auto_mount_root(parent_dir)
        if problem:
            prt(f'WARN: not removing {target_dir!r}: {problem}')
            return False
        return LuksTray.remove_empty_dir(target_dir)

    def remove_unused_automounts(self):
        """Remove empty subdirectories of `auto_mount_folder`.

        Returns:
            int: Number of directories removed.
        """
        parent_dir = self.get_auto_mount_root()
        # A root created by an older release inherits the umask (0775 on a
        # user-private-group system). Tighten it first, so the validation below
        # does not skip the cleanup forever; the repair validates the path
        # itself before touching its mode.
        LuksTray.repair_auto_mount_root(parent_dir)
        problem = LuksTray.validate_auto_mount_root(parent_dir)
        if problem:
            prt(f'WARN: skipping auto-mount cleanup: {problem}')
            return 0
        # Give the root back to the user if an older `sudo mkdir -p` left it
        # root-owned: then the plain rmdir below works without escalating at all.
        # A failure here is not fatal -- remove_empty_dir() falls back to sudo.
        LuksTray.ensure_auto_mount_root(parent_dir)
        if not os.path.isdir(parent_dir):
            return 0  # Nothing to clean yet

        try:
            names = os.listdir(parent_dir)
        except OSError as exc:
            prt(f'WARN: cannot list auto_mount_folder {parent_dir!r}: {exc}')
            return 0

        real_uid, _real_gid = LuksTray.get_real_user_ids()
        # Same owners as validate_auto_mount_root(): the real user, the effective
        # user, and root (auto-mount dirs are created by the tray via sudo).
        allowed_uids = {real_uid, os.geteuid(), 0}
        removed = 0
        for name in names:
            if name.startswith('.'):
                continue  # Never touch dot-entries (markers, trash, ...)
            full_path = os.path.join(parent_dir, name)
            if os.path.islink(full_path):
                continue  # Never follow symlinks out of the vault directory
            if not os.path.isdir(full_path):
                continue  # Regular file, nothing to do
            if os.path.ismount(full_path):
                continue  # Skip mount points -- still mounted/used
            try:
                stat_info = os.stat(full_path)
            except OSError as exc:
                prt_once(f'stat {full_path}',
                         f'note: cannot inspect {full_path!r}: {exc}')
                continue
            if stat_info.st_uid not in allowed_uids:
                continue  # Someone else's directory: not ours to delete
            # Checked before rmdir is even attempted: a folder holding files is
            # a normal, in-use vault, and letting `rmdir` report ENOTEMPTY for
            # it turned every startup into a "note: not removing ..." line.
            # Only a genuine race still reaches remove_empty_dir()'s reporting.
            if not LuksTray.is_empty_dir(full_path):
                continue
            if LuksTray.remove_empty_dir(full_path):
                removed += 1
                prt(f'removed unused auto-mount folder {full_path!r}')
        return removed

    @staticmethod
    def expand_real_user(path):
        """Expand `~` and `~user` relative to the real user under sudo."""
        real_user = os.environ.get('SUDO_USER')
        if not real_user:
            return os.path.expanduser(path)

        if path.startswith('~'):
            if path == '~' or path.startswith('~/'):
                real_home = os.path.join('/home', real_user)
                return os.path.join(real_home, path[2:]) if len(path) > 2 else real_home
            if path.startswith('~' + real_user):
                # e.g., ~joe/foo
                return os.path.expanduser(path)
            # ~otheruser — let os.path.expanduser handle it
            return os.path.expanduser(path)
        return path

    @staticmethod
    def shorten_home(path):
        """Return `path` with the real user's home written as `~`.

        Args:
            path: Path to shorten.

        Returns:
            str: The display form.
        """
        if not path:
            return path
        home = LuksTray.get_real_user_home().rstrip('/')
        if not home:
            return path
        if path == home:
            return '~'
        if path.startswith(home + '/'):
            return '~' + path[len(home):]
        return path


def tray_watcher_present():
    """Return whether a StatusNotifierWatcher is registered.

    Returns:
        bool | None: True or False when known, None when it cannot be asked.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from PyQt6.QtDBus import QDBusConnection
    except ImportError:
        prt('WARN: QtDBus unavailable; cannot detect a tray')
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            prt('WARN: no D-Bus session bus; cannot detect a tray')
            return None
        interface = bus.interface()
        # The default D-Bus timeout is 25s, which would stall start-up and the
        # periodic retry if the bus is wedged.
        interface.setTimeout(2000)
        reply = interface.isServiceRegistered(STATUS_NOTIFIER_WATCHER)
        return bool(reply.isValid() and reply.value())
    except Exception as exc:                    # pylint: disable=broad-except
        prt(f'WARN: tray detection failed: {exc}')
        return None


def acquire_lock_or_take_over(lock, replace=False):
    """Take the single-instance lock, or take over from its holder.

    Args:
        lock: The `SingleInstance` lock.
        replace: Terminate a non-supervised holder.

    Returns:
        bool: True when this process owns the lock.
    """
    if lock.acquire(retries=5, delay=0.2):
        return True
    holder = Utils.SingleInstance.read_pid(lock.path)
    if not replace or not holder:
        return False
    unit = service.supervised_by(holder)
    if unit:
        prt(f'--replace: pid {holder} is supervised by {unit}; not killing it')
        prt(f'use `luks-tray --svc-restart` (or `systemctl --user restart {unit}`) instead')
        return False
    prt(f'--replace: terminating lock holder pid {holder}')
    if not PyKill.kill_pid(holder, ['luks-tray']):
        prt(f'WARNING: pid {holder} does not look like luks-tray; not killing it')
        return False
    # The old process may need a moment to exit and let go of the flock.
    return lock.acquire(retries=25, delay=0.2)


def confirm(question):
    """ Ask on the terminal; anything but an explicit yes is no. """
    print(f'{question} [y/N] ', end='', flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ('y', 'yes')


def run_service_command(opts):
    """Run one `--svc-*` verb and return the process exit status.

    Args:
        opts: Parsed command-line options.

    Returns:
        int: Process exit status.
    """
    try:
        holder = service.applet_holder(IniTool(paths_only=True).folder)
    except OSError:
        holder = None       # unreadable lock: not a reason to refuse the request
    try:
        return service.dispatch(opts.svc,
                                force=opts.force,
                                yes=opts.yes or opts.force,
                                ask=confirm,
                                holder=holder)
    except service.ServiceError as exc:
        Utils.error_print(f'luks-tray: {exc}')
        return 1


def main():
    """Parse the CLI and run the tray loop."""
    import argparse
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser()
    parser.add_argument('-D', '--debug', action='store_true',
            help='enable debug_mode')
    parser.add_argument('-o', '--stdout', action='store_true',
            help='log to the terminal only, not to debug.log (default: both;'
                 ' ignored when a systemd unit started us: there stdout is the'
                 ' unit\'s)')
    parser.add_argument('-f', '--follow-log', action='store_true',
            help='exec tail -n50 -F on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('--check-deps', action='store_true',
            help='check that necessary system programs are installed')
    parser.add_argument('--show-window-rules', action='store_true',
            help='print the niri/sway rule that makes luks-tray dialogs float,'
                 ' then exit (reports the rule for this session)')
    parser.add_argument('--replace', action='store_true',
            help='if another luks-tray holds the single-instance lock, terminate'
                 ' it and take over (default: refuse to start)')
    parser.add_argument('-y', '--yes', action='store_true',
            help='never prompt; assume yes for --svc-* questions')
    parser.add_argument('--force', action='store_true',
            help='replace or delete a unit file luks-tray did not write'
                 ' (implies --yes)')
    svc = parser.add_mutually_exclusive_group()
    svc.add_argument('--svc', '--svc-status', dest='svc', action='store_const',
            const='status',
            help='report the systemd --user unit state, then exit (0 active,'
                 ' 1 installed but not running, 2 not installed)')
    svc.add_argument('--svc-install', dest='svc', action='store_const',
            const='install',
            help='write the unit and enable it (not started);'
                 ' only this verb writes the unit')
    svc.add_argument('--svc-start', dest='svc', action='store_const',
            const='start',
            help='start the installed unit now; enables it if disabled, but'
                 ' never writes the unit')
    svc.add_argument('--svc-restart', dest='svc', action='store_const',
            const='restart',
            help='restart the installed unit so it runs the current code'
                 ' (use after an editable install or a git pull); never writes it')
    svc.add_argument('--svc-stop', dest='svc', action='store_const',
            const='stop',
            help='stop it now; it stays enabled for your next login')
    svc.add_argument('--svc-uninstall', dest='svc', action='store_const',
            const='uninstall',
            help='stop, disable and delete the unit (config and logs are kept)')
    svc.add_argument('--show-svc-file', dest='svc', action='store_const',
            const='show-file',
            help='print the systemd unit, then exit'
                 ' (a starter for a hand-installed or customized unit)')
    # The release gate installs the built wheel in a throwaway venv and runs
    # this, so the number printed has to come from the installed package --
    # not from pyproject.toml, and not from the source tree.
    parser.add_argument('--version', action='version',
            version=f'%(prog)s {__version__}',
            help='print the version and exit')
    opts = parser.parse_args()

    if opts.show_window_rules:
        if opts.svc:
            parser.error('--show-window-rules cannot be combined with --svc-*')
        # Informational only: no IniTool, no Qt, no lock.  The snippet is for
        # the compositor that started this shell, which is exactly the session
        # the user is configuring.
        sys.exit(Compositor.show_rules())

    # The --svc-* verbs touch the unit files and systemd, and nothing else --
    # not the configuration, not the lock, not the Qt application object.
    if opts.svc:
        sys.exit(run_service_command(opts))

    if opts.edit_config:
        ini_tool = IniTool(paths_only=True)
        # `paths_only=True` deliberately skips __init__'s ensure_ini_file(),
        # so on a fresh install neither the folder nor config.ini exists and
        # $EDITOR would open -- or create -- the file with the wrong owner or
        # in a missing directory. Create both before exec'ing.
        ini_tool.ensure_ini_file()
        editor = os.getenv('EDITOR', 'vim')
        args = [editor, ini_tool.ini_path]
        print(f'RUNNING: {args}')
        os.execvp(editor, args)
        sys.exit(1) # just in case ;-)

    if opts.check_deps:
        _, missing = LuksTray.check_dependencies(verbose=True)
        sys.exit(1 if missing else 0) # just in case ;-)

    if opts.follow_log:
        ini_tool = IniTool(paths_only=True)
        # Same reason as --edit-config: `paths_only` skips ensure_ini_file(),
        # so the folder may not exist yet. Create the folder and an empty log
        # file up front -- `tail -F` would otherwise print
        # "cannot open ... for reading" on a fresh install.
        ini_tool.ensure_folder()
        if not os.path.exists(ini_tool.log_path):
            with open(ini_tool.log_path, 'a', encoding='utf-8'):
                pass
        args = ['tail', '-n50', '-F', ini_tool.log_path]
        print(f'RUNNING: {args}')
        os.execvp('tail', args)
        sys.exit(1) # just in case ;-)

    try:
        # Point the log at debug.log before the real IniTool is built: a problem
        # the constructor has to report then reaches the file instead of a
        # stdout that a desktop launch has already pointed nowhere. The
        # `paths_only` instance creates nothing on its own, so `ensure_folder()`
        # is called explicitly -- it makes the folder `configure_log()` writes
        # into, without creating config.ini as a side effect.
        configure_ini = IniTool(paths_only=True)
        configure_ini.ensure_folder()
        # Before configure_log() moves fd 2: error_print() needs a handle on
        # the console the user started us from, or "already running" would
        # only ever appear in the log a desktop launch points nowhere.
        Utils.capture_console()
        # `-o` is a request from a person with a terminal, so a unit ignores it:
        # there stdout is the unit's own redirect, and honouring it would put
        # the log somewhere the menu never looks.
        Utils.configure_log(configure_ini.log_path,
                            keep_stdout=opts.stdout
                            and not Utils.in_systemd_service())
        # Qt's own output goes through prt() from here on, so a theme plugin's
        # chatter cannot drown the log and a real Qt warning arrives with a
        # timestamp like every other line. Must precede the QApplication that
        # LuksTray creates.
        install_qt_messages()

        ini_tool = IniTool(paths_only=False)

        # The lock is the guarantee that two trays never coexist. It is taken
        # before anything destructive happens: a plain launch refuses to start
        # when an instance already owns it, and only `--replace` inspects the
        # holder, terminates it and takes over.
        lock = Utils.SingleInstance(ini_tool.lock_path)
        if not acquire_lock_or_take_over(lock, replace=opts.replace):
            holder = Utils.SingleInstance.read_pid(lock.path)
            unit = service.supervised_by(holder) if holder else None
            message = 'another luks-tray is already running'
            if holder:
                message += f' (pid {holder})'
            if unit:
                # A supervised incumbent comes back as soon as it is killed:
                # restarting its unit is the supported way to replace it.
                message += (f' under {unit}; use `luks-tray --svc-restart`'
                            f' (or `systemctl --user restart {unit}`)')
            elif not opts.replace:
                message += '; use --replace to terminate it and take over'
            prt(f'ERROR: {message}')
            Utils.error_print(f'ERROR: {message}')
            sys.exit(3)
        atexit.register(lock.release)

        # From here on this process *is* the tray, so the log gets its first and
        # last line. The stop line is registered with atexit (the menu's exit and
        # QApplication.exec() both leave that way) and with a signal handler
        # (systemd stopping us, --replace, a closing terminal).
        Utils.log_session_start(APP_NAME, __version__,
                                note=' (systemd)' if Utils.in_systemd_service()
                                else '')
        atexit.register(Utils.log_session_stop, APP_NAME, __version__)
        Utils.install_termination_log(APP_NAME, __version__)

        # Detach stdin from the terminal: every command runs with `sudo -n`
        # and feeds its password over a pipe, so a child that tried to read
        # the tty would steal keystrokes from the user's shell. The price is
        # that there is no terminal left to interrupt a wedged child, which
        # is why every subprocess call carries a deadline (CMD_TIMEOUT /
        # PROBE_TIMEOUT in Utils) and cannot freeze the UI indefinitely.
        # It happens after the lock check, so a refused second start still has
        # a terminal to print its "already running" message on.
        devnull_fd = os.open('/dev/null', os.O_RDWR)
        os.dup2(devnull_fd, sys.stdin.fileno())
        os.close(devnull_fd)

        # A unit's `Environment=` lines are a snapshot of the login that ran
        # `--svc-install`, and a unit's own value wins over the session's, so a
        # snapshot of a session that has ended is what Qt would otherwise use.
        # Re-derive the display variables from the server that is really
        # listening, before QApplication exists: switching between Wayland and
        # X11 must not need a re-install (see luks_tray/Session.py).
        for name, before, after in Session.repair_environment(note=prt):
            prt(f"session: {name} {before or '(unset)'} -> {after or '(unset)'}")

        # That repair fixes the *variables*; it cannot make the server accept us.
        # An X11 login authenticates with a cookie in `$XAUTHORITY`, and a unit
        # only has that variable when the session exported the environment to
        # the user manager.  Without it Qt's xcb plugin prints "could not
        # connect to display" and then *aborts*: one core dump per login in
        # `coredumpctl` and a SIGABRT that systemd reads as a crash.  Asking the
        # server first turns that into a log line and a status the unit retries.
        problem = Session.display_problem()
        if problem:
            prt(f'ERROR: {problem}')
            sys.exit(EXIT_NO_DISPLAY)

        tray = LuksTray(ini_tool, opts)
        sys.exit(tray.app.exec())

    except Exception as exce:                           # pylint: disable=broad-except
        # prt(), not print(): by now stdout may already point at the debug log,
        # and a failure that kills the tray is exactly what that log is for.
        prt("exception:", str(exce))
        prt(traceback.format_exc())
        sys.exit(15)

if __name__ == "__main__":
    main()
