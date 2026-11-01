#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" LUKS TRAY -- application entry point and tray orchestration.

    The code that used to live in this one file is now split by concern:

    - [`luks_tray.DeviceInfo`](luks_tray/DeviceInfo.py) -- `lsblk` parsing
      and the "banned mount point" policy (device discovery);
    - [`luks_tray.LuksOps`](luks_tray/LuksOps.py) -- privileged command
      helpers (`sudo -n`, deadlines) and the LUKS primitives (unlock, mount,
      loop setup, format);
    - [`luks_tray.dialogs`](luks_tray/dialogs) -- the Qt dialogs, which take
      the tray instance as a constructor argument.

    What remains here is the Qt tray itself (menu building, refresh policy),
    the auto-mount folder policy and `main()`.

    The Wayland/Sway caveats that shaped the dialogs are documented in
    [`luks_tray.dialogs.Common`](luks_tray/dialogs/Common.py).
"""
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments,too-many-nested-blocks
# pylint: disable=line-too-long,too-many-lines,too-many-public-methods
# pylint: disable=too-many-return-statements
import atexit
import os
import sys
import signal
import shutil
import subprocess
import pwd
import traceback
import time
import importlib.resources
import random
from contextlib import contextmanager
from types import SimpleNamespace
import petname
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtWidgets import QWidgetAction, QWidget, QLabel
from PyQt6.QtGui import QIcon, QCursor, QAction, QFont, QFontInfo
from PyQt6.QtCore import QTimer, Qt

from luks_tray.History import HistoryClass
from luks_tray.Utils import PyKill, prt, prt_once
from luks_tray import Utils, __version__
from luks_tray import service
from luks_tray.IniTool import IniTool
from luks_tray.DeviceInfo import DeviceInfo
from luks_tray.dialogs import (CommonDialog, MasterPasswordDialog,
                               MountDeviceDialog, MountFileDialog,
                               OptionsDialog, requires_manual_title)
from luks_tray.LuksOps import (generate_uuid_for_file_path,
                                get_luks_uuid_for_file, run_cmd,
                                run_cmd_capture, run_unmount, sudo_cmd,
                                sudo_run)
from luks_tray.TrayWatch import STATUS_NOTIFIER_WATCHER, TrayWatch

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
    'MountDeviceDialog',
    'MountFileDialog',
    'OptionsDialog',
    'acquire_lock_or_take_over',
    'generate_uuid_for_file_path',
    'get_luks_uuid_for_file',
    'main',
    'requires_manual_title',
    'run_cmd',
    'run_cmd_capture',
    'run_unmount',
    'sudo_cmd',
    'sudo_run',
    'tray_watcher_present',
]

# Every tray host -- plasmashell, xfce4-panel, waybar, swaybar+snixembed, ... --
# claims STATUS_NOTIFIER_WATCHER (see TrayWatch.py).  A host that is not on the
# bus yet is an event, not a wait: the item is registered again when one
# appears, so start-up never blocks on the bar.
TRAY_ATTACH_DELAY_MS = 500      # let a new host start serving calls


# Paths that must never be accepted as `auto_mount_folder`. The startup cleanup
# pass deletes (empty) subdirectories below that path, so a careless config value
# such as `/home` or `/` would otherwise turn a typo into an `rmdir` sweep over
# unrelated, system-owned paths.
FORBIDDEN_AUTO_MOUNT_ROOTS = frozenset((
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib32',
    '/lib64', '/media', '/mnt', '/opt', '/proc', '/root', '/run',
    '/sbin', '/srv', '/sys', '/tmp', '/usr', '/var',
))
# Same idea, but for whole system trees: nothing *inside* these may be used
# either. `/home`, `/mnt`, `/media` and `/tmp` are absent on purpose -- a vault
# directory may legitimately live there (e.g. `~/Vaults`, `/mnt/Vaults`); they
# are covered by the home-directory and ownership/permission checks instead.
# Paths inside the real user's home are exempt from this list, so non-standard
# layouts such as /var/home/<user>/Vaults keep working.
FORBIDDEN_AUTO_MOUNT_PREFIXES = tuple(os.path.normpath(path) for path in (
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib32', '/lib64', '/opt',
    '/proc', '/root', '/run', '/sbin', '/srv', '/sys', '/usr', '/var',
))


class LuksTray():
    """ The tray icon, the menu it shows, and the refresh/mount policy.

        `main()` creates exactly one instance, and the vault-root policy
        (`get_auto_mount_root()` and friends) are *methods* on it rather than
        lookups through a class-level `singleton`: a static helper that reads
        the configuration can only work after a tray exists, which turned
        construction order into a hidden requirement and crashed with
        `AttributeError` when it was violated. Besides the `QSystemTrayIcon`
        this owns the single `QMenu`, rebuilt only when its contents actually
        change (see `replace_menu_if_different()`), and the 3 s `QTimer` that
        drives `update_menu()`.

        The poller is synchronous but self-guarded: `update_menu()` returns
        immediately while a refresh or a privileged dialog operation is in
        flight, and `busy_operation()` marks the latter, so a tick that lands
        between `cryptsetup open` and `mount` cannot snapshot a half-mounted
        container.
    """
    svg_info = SimpleNamespace(version='04', bases=[
                            'white-shield',  # no LUKS partitions unlocked
                            'alert-shield',  # some partitions unlocked but not mounted
                            'green-shield',   # some partitions unlocked and mounted
                            # 'orange-shield',  # some partitions locked
                            # 'yellow-shield',  # no partitions locked (all locked)
                        ],
                        nicknames=[
                            'none',
                            'alert',
                            'ok',
                        ] )

    def __init__(self, ini_tool, opts):
        self.uid = os.environ.get('SUDO_UID', os.getuid())
        self.gid = os.environ.get('SUDO_GID', os.getgid())

        self.ini_tool = ini_tool
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.mono_font = QFont("Consolas", 10)
        self.mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self.emoji_font = self.get_emoji_font()
        self.app.setFont(self.mono_font)

        _, missing = self.check_dependencies()

        if missing:
            # Was a bare `assert`: `python -O` strips it, so the tray carried
            # on with half the system commands missing (no cryptsetup, no
            # mkfs.ext4, ...) and failed later in confusing ways.
            raise RuntimeError(f'missing system commands: {missing}')

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
        # is picked up by the watch below instead of being waited for.  ``menu``
        # exists first, so re-registering the item can restore it, and the watch
        # starts *before* the item is shown: a watcher that appears between the
        # two still produces the event we need.
        tray_present = tray_watcher_present()
        self._tray_seen = tray_present is True
        self.tray_watch = TrayWatch(on_available=self._on_tray_host_appeared,
                                    on_lost=self._on_tray_host_lost)
        if not self.tray_watch.start() and tray_present is not None:
            prt('WARN: cannot watch for a tray host that starts later')
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
        """ Return (found, missing) for the system commands the tray shells out to.

            `__init__` refuses to start when `missing` is non-empty (it used to
            be a bare `assert`, which `python -O` stripped) and `--check-deps`
            prints the same two lists. Lookup is `shutil.which()`, so a PATH
            that lacks /usr/sbin reports `cryptsetup` as missing even though it
            is installed -- run the tray the way the desktop session would.

            The list is meant to be *complete*, not minimal: a command left off
            it does not go away, it just fails later, on some unlucky path,
            with a message that no longer says "install me". Three entries look
            superfluous and are not:

            - `sudo` is never looked up by the app itself -- `sudo_cmd()` and
              `sudo_run()` prepend it -- so without this check its absence
              would surface as a FileNotFoundError from subprocess rather than
              as a readable "please install sudo";
            - `blkid` is only a read-only probe, but it is what keeps a file
              container's history entry alive: without it the LUKS UUID cannot
              be re-derived and the entry (password, mount point) is purged;
            - `touch` and `chown` sit on the two paths that are rarely re-run:
              a newly created file container, and the repair of a root-owned
              vault root left behind by an older `sudo mkdir -p`.
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
        return found, missing

    @staticmethod
    def get_emoji_font(size=10):
        """Try to load Noto Color Emoji, fallback to system emoji-capable fonts."""
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
        """ Re-read /proc/mounts into `mount_infos` and `upons`.

            Returns the set of *device* keys (`mount_infos.keys()`), which is
            what the old caller passed around as "mount points". Callers that
            need mount points must use `self.upons` -- `check_upon()` documents
            the difference.
        """
        self.mount_infos = {}
        self.upons = set()
        with open('/proc/mounts', 'r', encoding='utf-8') as f:
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
        """ True if `thing` is a mounted device or a mount point. """
        return thing in self.mount_infos or thing in self.upons

    def merge_containers_history(self):
        """ Make the history agree with the containers just discovered.

            The history is restored from disk first (another process may have
            written it), then every container without an entry is registered
            and the result is saved once -- so a device or file seen for the
            first time is remembered before the menu is built from it.
        """
        self.history.restore()
        for container in self.containers.values():
            self.history.ensure_container(container)
        self.history.save()

    def update_menu(self):
        """Refresh device/history state, then rebuild the tray menu.

            This is both the 3 s timer slot and the call dialogs make when an
            operation finishes, so it has to be re-entrancy safe. It reads
            /proc/mounts, spawns lsblk, reloads history (blkid per back_file)
            and finally rebuilds the QMenu. A nested second run used to happen
            whenever an event was dispatched inside the first one -- the
            processEvents() that show_progress() called did exactly that --
            and it could rebuild the menu from a container list captured
            *before* a cryptsetup/mount that was still in flight.
        """
        if self.refreshing or self.operation:
            return
        self.refreshing = True
        try:
            self.ini_tool.update_config()
            if self.history.status in ('unlocked', 'clear_text'):
                self.update_mounts()
                self.containers = self.lsblk.parse_lsblk()
                self.merge_containers_history()
                # add in the containers that are not mounted but in
                # the history
                for vital in self.history.vitals.values():
                    container = self.containers.get(vital.uuid, None)
                    if container:
                        container.vital = vital
                    if not container and vital.back_file:
                        # insert known file container (present device
                        # containers should be in the containers list already)
                        ns = DeviceInfo.make_partition_namespace('', '')
                        ns.type = 'crypt'
                        ns.back_file = vital.back_file
                        ns.opened = False
                        ns.uuid = vital.uuid
                        ns.vital = vital
                        self.containers[vital.uuid] = ns

            self.update_menu_items()
        finally:
            self.refreshing = False

    @contextmanager
    def busy_operation(self, name):
        """Suspend polling while a privileged dialog action is in flight.

            Between `cryptsetup open` and `mount` (or between `umount` and
            `cryptsetup close`) the system is in a state no refresh should
            snapshot: the container looks half-opened, and the menu rebuilt
            from it offered the wrong action. Restored on exit, including via
            exceptions, so a failed operation cannot park the poller.
        """
        previous, self.operation = self.operation, name
        try:
            yield
        finally:
            self.operation = previous

    def update_menu_items(self):
        """ Build the menu entries and hand them to `replace_menu_if_different()`.

            The list is a mix of ready-made `QAction`s and `None` for a
            separator: building actions is cheap and comparing them is what
            decides whether the popup has to be replaced at all.

            Every fixed entry starts with one symbol from the same small set
            (`✚` add, `✦` create, `⚙` options, `☓` exit, `↺` restart), the way
            the containers already carry `▣ ⧈ ▽ ‼`: they are basic dingbats that
            the monospace font itself has, so the list reads at a glance without
            depending on an emoji font being installed.
        """
        actions = []

        icon_key = 'none'
        do_alerts = self.ini_tool.get_current_val('show_anomaly_alerts')

        if self.history.status == 'locked':
            action = QAction('✎ Click to enter master password', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.prompt_master_password)
            actions.append(action)
        else:
            separated = False
            idx = -1
            for idx, container in enumerate(self.containers.values()):
                mountpoint = container.upon
                if not mountpoint and container.vital:
                    mountpoint = f'[{container.vital.upon}]'

                if idx > 0 and not separated and container.type == 'crypt':
                    actions.append(None)
                    separated = True

                name = container.name
                if container.back_file:
                    name = container.back_file
                    if name.startswith('/home/'):
                        name = '~' + name[6:]

                # Determine which emoji/symbol to show
                if mountpoint.startswith('/'):
                    emoji = '⧈' if container.readonly else '▣'
                    icon_key = 'ok' if icon_key != 'alert' else icon_key
                elif container.opened:
                    emoji = '‼'
                    icon_key = 'alert' if do_alerts else icon_key
                else:
                    emoji = '▽'


                # Construct menu line text
                if emoji == '‼':
                    text = f'{name} CLICK-to-LOCK'
                else:
                    text = f'{name} {mountpoint}'


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

        # The tail below is identical in both states, which is why the master
        # password is no longer a top-level entry: setting, changing and
        # clearing it are all reachable from the options dialog, and the line
        # above is the only password action the *locked* state needs.
        actions.append(None)

        action = QAction('⚙ Options…', self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.handle_options_click)
        actions.append(action)

        actions.append(None)

        action = QAction('☓ Exit', self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.exit_app)
        actions.append(action)

        # The version rides on the restart entry instead of a separate dead
        # label: the log says the same thing at start-up, but "which build am I
        # running?" is a question asked from the menu, usually about a desktop
        # that behaves differently from what the reader expects.
        action = QAction(f'↺ Restart luks-tray v{__version__}', self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.restart_self)
        actions.append(action)

        return self.replace_menu_if_different(actions, icon_key)


    def replace_menu_if_different(self, actions, icon_key):
        """ Install `actions` on the tray menu, but only if they changed.

            Rebuilding the `QMenu` on every 3 s tick would close a popup under
            the user's cursor, so the new list is compared first -- by length,
            by rendered text and by icon key. Returns True when the menu was
            replaced.

            `replace_menu()` reopens a popup that was visible, at the cursor
            position, and keeps the list in `self.actions` for the next
            comparison.
        """
        def replace_menu():
            """ Install `actions` on the menu and refresh the tray icon. """
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
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
        if uuid in self.containers:
            dialog = MountDeviceDialog(self, self.containers[uuid])
            dialog.exec()

    def handle_file_click(self, uuid):
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
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
        """ Open the options dialog: the `[ui]` settings and the master password. """
        dialog = OptionsDialog(self)
        dialog.exec()

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

    def restart_self(self, *_args):
        """ Replace this process with a fresh instance of the applet.

            Under a ``systemd --user`` unit the restart is handed to the
            supervisor instead: ``Restart=always`` reacts to *our* exit, not to
            a child we spawn, and the child shares the cgroup systemd kills
            when it stops the unit.  See `_restart_via_systemd()`.

            Elsewhere the replacement is launched as ``python -m luks_tray.Main``
            so that PyKill recognises it via `command_names()`.  The
            single-instance lock is released first: ``os._exit()`` skips atexit
            handlers, and the replacement must be able to take the lock as soon
            as it starts.  If the replacement cannot be spawned the lock is
            taken back, so a failed restart does not quietly leave the running
            tray unlocked.

            The package root goes on PYTHONPATH rather than into ``cwd=``: the
            replacement inherits whatever directory the user is in and must
            still be able to import the module when running from a checkout.
        """
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
        os._exit(0)

    def _restart_via_systemd(self):
        """ Hand this process's restart to systemd, from outside our cgroup.

            ``systemctl --user restart`` is spawned through ``systemd-run
            --user --scope`` (see `Utils.spawn_detached()`), so it keeps running
            while systemd stops us.  We then exit with status 0, which is *not*
            in the unit's ``RestartPreventExitStatus=``, so ``Restart=always``
            also schedules a start: if the detached restart never got off the
            ground, the supervisor still brings the tray back five seconds
            later.  A failure to schedule anything keeps this instance alive
            rather than leaving the user with no tray at all.

            The lock is released last, immediately before ``os._exit()``, so the
            restarting unit never finds the lock held by a live process.
        """
        prt('+', 'restarting applet via systemd...')
        try:
            service.detached_restart()
        except (service.ServiceError, OSError) as exc:
            prt(f'FAIL: cannot restart the unit: {exc}')
            Utils.error_print(f'FAIL: cannot restart the unit: {exc}')
            return
        try:
            self.tray_icon.hide()
        except (AttributeError, RuntimeError) as exc:
            prt(f'note: cannot hide the tray icon before restarting: {exc}')
        Utils.SingleInstance.release_current()
        os._exit(0)

    def prompt_master_password(self):
        """ Prompt for master passdword"""
        dialog = MasterPasswordDialog(self)
        dialog.exec()

    # -- tray registration -------------------------------------------------

    def current_icon_key(self):
        """ The icon key in use, or 'none' before the first update_menu(). """
        key = getattr(self, 'prev_icon_key', '')
        return key if key in self.icons else 'none'

    def _install_tray_icon(self):
        """ Create the tray item; showing it is what registers it with a host.

            Qt talks to the StatusNotifierWatcher once, when the item is shown,
            and never retries, so re-creating the item is the only way to
            register with a host that was absent the first time -- or with a
            host that has been restarted since.  The previous item is hidden
            before the new one is shown, so a host never lists two entries.
        """
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

    def update_history(self, uuid, values, back_file=None):
        """ Record/refresh the history entry for a container.

            `back_file` is only meaningful for LUKS *file* containers, and it
            must be stored here: HistoryClass re-derives the LUKS UUID of every
            back_file on restore and purges the entry when it does not match
            the key. A file entry written without its back_file can therefore
            never be found again after a restart.
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
        """ Return the configured `auto_mount_folder` as an absolute path.

            A relative value is *refused*, not resolved: `abspath` would turn
            `Vaults` into `<cwd>/Vaults`, which then passes every check in
            [`validate_auto_mount_root()`](luks_tray/Main.py) -- it is absolute
            by then, two levels deep, neither home nor a system tree -- so a
            typo would silently relocate the vault area to wherever the process
            happened to start. The configured default is used instead, reported
            once per offending value.
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
        """ Return (uid, gid) of the user this tray acts for.

            Under `sudo` these are SUDO_UID/SUDO_GID; otherwise they are the
            effective ids. The value is always an int -- SUDO_UID arrives as a
            string from the environment, which cannot be compared with the
            numeric st_uid reported by os.stat().
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
        """ Home directory of the user this tray acts for (sudo aware). """
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
        """ Return None if `auto_root` may be cleaned, else an error string.

            [`remove_unused_automounts()`](luks_tray/Main.py) deletes (empty)
            subdirectories below `auto_mount_folder`, which comes straight from
            config.ini, so a typo (`/`, `/home`) or a shared system path must not
            turn the startup cleanup into an `rmdir` sweep over unrelated paths.
            The folder therefore has to look like a *dedicated* vault area:

            - an absolute path, at least two levels deep, not in/under a system
              tree, and not a symlink (which could point anywhere);
            - neither the real user's home nor an ancestor of it, and not inside
              another user's home;
            - if it exists: a plain directory, not a mount point, owned by the
              real user or by root (an install whose mount points were created
              with `sudo mkdir` has a root-owned root; the cleanup still works
              there because it retries `rmdir` via `sudo -n`), and not writable
              by group/other.

            The checks are deliberately conservative: when in doubt, skip the
            cleanup and log why (`remove_unused_automounts` treats any returned
            string as "do nothing").
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
        if stat_info.st_mode & 0o022:
            return (f'auto_mount_folder {path!r} is writable by group/other '
                    f'(shared directory)')
        return None

    @staticmethod
    def ensure_auto_mount_root(auto_root):
        """ Make the auto-mount root exist, and belong to the real user.

            Earlier releases created mount points with `sudo mkdir -p`, which
            left the *root* root-owned as well (the `-p` creates missing
            parents). Since `rmdir` needs write permission on the parent, the
            cleanup could then never remove anything below it, and every new
            mount point had to be created through sudo again. One non-recursive
            `chown` of the root repairs that for good: afterwards the user owns
            the tree, so mount points are created and cleaned up unprivileged.

            The `chown` is deliberately *not* `-R`: that would rewrite the
            ownership of whatever filesystem is mounted inside. It is also only
            attempted on a root that [`validate_auto_mount_root()`]
            (luks_tray/Main.py) accepts, so a path read from config.ini cannot
            turn this into a privileged chown of an arbitrary directory.

            Returns None on success, or a 'FAIL: ...' string.
        """
        if not os.path.isdir(auto_root):
            return run_cmd(['mkdir', '-p', auto_root])
        real_uid, real_gid = LuksTray.get_real_user_ids()
        try:
            owner_uid = os.stat(auto_root).st_uid
        except OSError as exc:
            return f'FAIL: cannot inspect {auto_root!r}: {exc}'
        if owner_uid == real_uid:
            return None                      # Already the user's: nothing to do
        problem = LuksTray.validate_auto_mount_root(auto_root)
        if problem:
            return f'FAIL: not repairing ownership of {auto_root!r}: {problem}'
        return sudo_cmd(['chown', f'{real_uid}:{real_gid}', auto_root])

    def generate_auto_mount_folder(self):
        """ Return a new, unused mount point below the auto-mount root.

            Makes the root exist and belong to the user first (see
            [`ensure_auto_mount_root()`](luks_tray/Main.py)), so the mount point
            below it can be created unprivileged and cleaned up again. Then
            offers petname-style names: the short suffix alone at first and the
            full `adjective_name` form after a few tries, skipping anything
            currently mounted or known to the history. If those run out, a
            numeric `vaultNNNN` fallback is used; RuntimeError is raised when
            even that is exhausted.
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
        """ rmdir `path` if (and only if) it is an empty, plain directory.

            The guards below decide whether a directory may go; the credentials
            used to remove it do not change them -- `rmdir` itself refuses
            non-empty directories and mount points, and the caller has already
            restricted `path` to below a validated auto-mount root (see
            [`validate_auto_mount_root()`](luks_tray/Main.py)).

            The first attempt is unprivileged; EACCES/EPERM is retried with
            `sudo -n`, like the rest of the mount handling. That is what makes
            the cleanup work on a vault root earlier releases left root-owned
            (`sudo mkdir -p` created it as root): `rmdir` needs write permission
            on the *parent*, which the unprivileged tray does not have there.
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
        """Remove target_dir if it is empty and within parent_dir"""
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
        """ A startup function (could be periodic) that cleans up the
            auto mount folder.

            Deletes empty subdirectories of `auto_mount_folder`, which is read
            from config.ini -- so the folder is validated first (see
            [`validate_auto_mount_root()`](luks_tray/Main.py)); when it is not a
            dedicated, user-owned directory the whole pass is skipped. Returns
            the number of directories removed.
        """
        parent_dir = self.get_auto_mount_root()
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
        """
        Expands ~ and ~user in paths relative to the *real* user when run under sudo.
        """
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


def tray_watcher_present():
    """ Ask the session bus whether a StatusNotifierWatcher is registered.

        Returns True/False, or None when the question cannot be asked at all
        (no QtDBus module, no session bus, D-Bus error).  QtDBus ships with the
        PyQt6 this applet already requires, so the check adds no package; the
        import is local, so importing this module -- and therefore
        `luks-tray --help` -- never touches D-Bus.
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
    """ Take the single-instance lock and report whether this process owns it.

        The lock comes *first*: killing an incumbent must never be what a plain
        start does, because a DE autostart plus a supervisor that restarts the
        tray would then ping-pong forever, each new instance terminating the
        other.

        Killing is therefore the *takeover* path, and it is opt-in
        (``--replace``).  Even then the recorded pid is only signalled after its
        command line is confirmed to still be luks-tray, so a recycled pid or a
        foreign lock holder is left alone -- and a holder that systemd
        supervises is never signalled at all, because restarting the unit is
        what recreates it (see `service.supervised_by()`).
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
    """ Run one --svc-* verb and return the process exit status.

        None of these verbs build the applet: they render the shipped unit for
        this session, install it, and hand control to systemctl.  They print to
        stdout/stderr as they are, so the verbs stay usable from a terminal --
        and from `if luks-tray --svc-status; then ...`, which is why `status`
        has its own exit codes (0 active, 1 installed but not running, 2 not
        installed) instead of the usual 0/1.
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
    """ Entry point: parse the CLI and then run the tray loop.

        The informational options (`--version`, `--edit-config`,
        `--follow-log`, `--check-deps`, `--svc-*`) print or exec and never
        return; the normal path takes the single-instance lock, builds
        `IniTool` and `LuksTray` and hands control to `QApplication.exec()`.
        Anything that fails on the way is logged through `prt()` -- stdout may
        already be the debug log by then -- and exits with status 15.  A second
        instance that does not own the lock exits with status 3 instead of
        starting.

        The `--svc-*` verbs never construct the applet and never take the lock:
        they exist to install, inspect and remove the `systemd --user` unit, so
        they have to keep working while a tray is running -- or while the unit
        is broken.

        `--version` is deliberately handled by argparse (`action='version'`):
        it exits while parsing, before `IniTool` is built and before stdin is
        detached from the terminal, which is what makes it usable as the
        smoke check ./bless runs against a wheel installed in a scratch venv.
    """
    import argparse
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser()
    parser.add_argument('-D', '--debug', action='store_true',
            help='enable debug_mode')
    parser.add_argument('-o', '--stdout', action='store_true',
            help='log to stdout (if a tty)')
    parser.add_argument('-f', '--follow-log', action='store_true',
            help='exec tail -n50 -F on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('--check-deps', action='store_true',
            help='check that necessary system programs are installed')
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
            help='write the unit for this session and enable it (not started);'
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
            help='print the systemd unit for this session, then exit'
                 ' (a starter for a hand-installed or customized unit)')
    # The release gate installs the built wheel in a throwaway venv and runs
    # this, so the number printed has to come from the installed package --
    # not from pyproject.toml, and not from the source tree.
    parser.add_argument('--version', action='version',
            version=f'%(prog)s {__version__}',
            help='print the version and exit')
    opts = parser.parse_args()

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
        Utils.configure_log(configure_ini.log_path)

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
