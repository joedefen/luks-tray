#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" LUKS TRAY

This is not working so well for sway (or wayland):

Issue,Target,Summary of Failure

- Floating/Decorations, PyQt6/Wayland CSD
  - "PyQt's attempts to hint the window as a floating dialog
    and draw its own Client-Side Decorations (CSD)
    were either stripped by the Xwayland layer or ignored by Sway.
    The CSD feature, meant to give apps control, failed to render even the basic title bar."
- Taskbar Icon, PyQt6/Wayland CSD
   - "The internal flag to skip the taskbar (WindowType.Tool) failed
     because the window was treated as a generic,
     primary application window (app_id: python3) by the Wayland compositor."

"""
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments,too-many-nested-blocks
# pylint: disable=line-too-long,too-many-lines,too-many-public-methods
# pylint: disable=too-many-return-statements
import os
import sys
import json
import signal
import subprocess
import shutil
import shlex
import pwd
import traceback
import hashlib
import time
import importlib.resources
import random
import tempfile
from pathlib import Path
from functools import partial
from contextlib import contextmanager
from io import StringIO
from datetime import datetime
from types import SimpleNamespace
import petname
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt6.QtWidgets import QFileDialog, QCheckBox, QSizePolicy
from PyQt6.QtWidgets import QProgressBar, QWidgetAction, QWidget
from PyQt6.QtGui import QIcon, QCursor, QAction, QFont, QFontDatabase, QFontInfo
from PyQt6.QtCore import QTimer, Qt
    # from PyQt6.QtWidgets import QLabel, QWidgetAction
    # from PyQt6.QtCore import Qt

from luks_tray.History import HistoryClass
from luks_tray.Utils import prt, CMD_TIMEOUT, PROBE_TIMEOUT, FORMAT_TIMEOUT, TIMEOUT_RC
from luks_tray import Utils
from luks_tray.IniTool import IniTool


def requires_manual_title():
    """Checks if we are likely in a Wayland/Sway environment where SSD is missing."""
    # If using Xwayland under a tiling WM, XDG_CURRENT_DESKTOP might be helpful
    params = ['XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION']
    for param in params:
        # .get() returns None when unset (headless, cron, some autostart
        # contexts), so coerce to '' before calling .lower().
        if 'sway' in (os.environ.get(param) or '').lower():
            return True
    return False

# Global flag to run the check only once
IS_SWAY_LIKE_ENV = requires_manual_title()

def generate_uuid_for_file_path(file_path):
    """ Use SHA-256 to hash the file path """
    file_hash = hashlib.sha256(file_path.encode('utf-8')).hexdigest()

    # Take the first 32 characters of the hash and format it as a UUID
    # UUID is normally 32 hexadecimal digits, formatted as 8-4-4-4-12
    generated_uuid = f"{file_hash[:8]}-{file_hash[8:12]}-{file_hash[12:16]}-{file_hash[16:20]}-{file_hash[20:32]}"

    return generated_uuid

def get_luks_uuid_for_file(file_path):
    """ Return the LUKS UUID *inside* a LUKS file ('' if it cannot be read).

        History entries for file containers are keyed by this UUID -- the same
        value lsblk reports for the loop device and the same value
        HistoryClass re-checks with blkid on restore. A path-derived UUID
        (generate_uuid_for_file_path) never matches, so such an entry is
        silently purged the next time the history is loaded.
    """
    if not file_path:
        return ''
    rc, stdout, _stderr = run_cmd_capture(
            ['blkid', '-o', 'value', '-s', 'UUID', file_path])
    return stdout.strip() if rc == 0 else ''

def sudo_cmd(args, errs=None, input_str=None, timeout=None):
    """ run sudo -n {args}; the -n will avoid prompting for a
        sudo password and fail if not allowed
    """
    args = ['sudo', '-n'] + args
    return run_cmd(args, errs, input_str, timeout)

def run_cmd(args, errs=None, input_str=None, timeout=None):
    """ Run args and return None on success, or a 'FAIL: ...' string on failure.

        stdout is deliberately NOT returned here -- this helper is a
        success/failure predicate. Use run_cmd_capture() when you need the
        child's output.

        `timeout` is forwarded to run_cmd_capture() (default CMD_TIMEOUT);
        pass FORMAT_TIMEOUT for the slow, one-shot setup commands.
    """
    rc, stdout, stderr = run_cmd_capture(args, input_str, timeout)
    if rc == 0:
        return None
    # NB: keep the outer quote different from the one used inside the
    # expression -- quote reuse is PEP 701 / Python 3.12+ only.
    err = f"FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]"
    # NOTE: test against None, not truthiness -- callers routinely pass a
    # freshly created [] to collect errors and an empty list is falsy.
    if errs is not None:
        errs.append(err)
    return err

def _as_text(value):
    """ Coerce TimeoutExpired.stdout/.stderr to str.

        Depending on the Python version those attributes hold None, raw bytes
        (the child was killed mid-write) or already-decoded text.
    """
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode(errors='replace')
    return value

def run_cmd_capture(args, input_str=None, timeout=None):
    """ Run args via subprocess.run(); return (returncode, stdout, stderr).

        `timeout` defaults to CMD_TIMEOUT. A child that overruns the deadline
        is killed (subprocess.run() does that for us) and reported through
        TIMEOUT_RC, so callers need no extra branch: run_cmd()/sudo_cmd()
        just render it as their usual 'FAIL: ...' string. Without this, a
        hung cryptsetup/mount blocked the Qt main thread forever and the tray
        had no terminal left (stdin is /dev/null) to interrupt it.
    """
    if timeout is None:
        timeout = CMD_TIMEOUT
    try:
        sub = subprocess.run(args, input=input_str, capture_output=True,
                             text=True, check=False, timeout=timeout)
        return sub.returncode, sub.stdout, sub.stderr
    except subprocess.TimeoutExpired as exce:
        # sudo execs the real command in the same process, so the kill lands
        # on cryptsetup/mount itself rather than on a wrapper.
        return TIMEOUT_RC, _as_text(exce.stdout), (
            f'{_as_text(exce.stderr)}timed out after {timeout}s')

def sudo_run(args, input_str=None, timeout=None):
    """ Like run_cmd_capture(), but with sudo -n (never prompts). """
    return run_cmd_capture(['sudo', '-n'] + args, input_str, timeout)



def run_unmount(mount_point: str, busy_warns: set) -> str | None:
    """Attempts to unmount the given mount point.
    If it fails due to 'busy', show a popup with the list of processes using it.
    Returns error string or None on success.
    """
    try:
        sub = subprocess.run(['sudo', "umount", mount_point],
                    capture_output=True, text=True, check=False,
                    timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f'FAIL: umount {mount_point}: timed out after {CMD_TIMEOUT}s'
    except Exception as e:
        return f"FAIL: umount {mount_point}: Exception: {e}"

    if sub.returncode == 0:
        return None  # success

    err = f"FAIL: umount {mount_point}: {sub.stdout.strip()} {sub.stderr.strip()} [rc={sub.returncode}]"

    if 'busy' not in sub.stderr.lower() or mount_point in busy_warns:
        return err  # Not a 'busy' error — no popup needed

    # Try to get processes using the mount point
    try:
        fuser = subprocess.run(
                ['sudo', "fuser", "-vm", mount_point],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=PROBE_TIMEOUT
            )
        fuser_out = fuser.stderr.strip()  # This is the real data

    except Exception as e:
        fuser_out = f"(Could not get process info: {e})"

    busy_warns.add(mount_point)

    # Extract just PID and COMMAND, skip 'kernel' and header lines
    process_lines = []
    for line in fuser_out.splitlines():
        if line.startswith("USER") or mount_point in line:
            continue  # Skip header and mount line
        process_lines.append(line.strip())

    info = "\n - ".join(process_lines) if process_lines else "(No user-space processes found using the mount)"

    # Show user-friendly popup
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.set_title("Unmount Failed — Device Busy [luks-tray]")
    msg.setText(f"'{mount_point}' busy by these processes:\n - {info}")
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()

    return err

class DeviceInfo:
    """ Class to dig out the info we want from the system."""
    bans = ('/', '/home', '/var', '/usr', '/tmp', '/opt', '/srv',
            '/boot', '/sys', '/proc', '/dev', '/run')

    def __init__(self, opts, tray):
        self.opts = opts
        self.tray = tray
        self.DB = opts.debug
        self.wids = None
        self.partitions = None
        self.entries = {}
        self.prev_entries_str = ''

    @staticmethod
    def make_partition_namespace(name, size_str):
        """ TBD """
        return SimpleNamespace(name=name,       # /proc/partitions
            opened=None,    # or True or False
            upon='',        # primary mount point
            uuid='',
            size_str=size_str,  # /sys/block/{name}/... (e.g., 3.5T)
            type='',        # e.g., loop, crypt, disk, part
            fstype='',      # fstype OR /sys/class/block/{name}/device/model
            label='',       # blkid
            mounts=[],      # /proc/mounts
            parent=None,    # a partition
            filesystems=[],        # child file systems
            back_file='', # backing file
            vital=None, # history if any
            readonly=False, # whether readonly
            )


    @staticmethod
    def get_device_vendor_model(device_name):
        """ Gets the vendor and model for a given device from the /sys/class/block directory.
        - Args: - device_name: The device name, such as 'sda', 'sdb', etc.
-       - Returns: A string containing the vendor and model information.
        """
        def get_str(device_name, suffix):
            try:
                rv = ''
                fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
                with open(fullpath, 'r', encoding='utf-8') as f: # Read information
                    rv = f.read().strip()
            except (FileNotFoundError, Exception):
                # print(f"Error reading {info} for {device_name} : {e}")
                pass
            return rv

        # rv = f'{get_str(device_name, "vendor")}' #vendor seems useless/confusing
        rv = f'{get_str(device_name, "model")}'
        return rv.strip()

    @staticmethod
    def is_banned(mounts):
        """ Is a mount point (or any in a list of mountpoints) banned?
            Returns True if so.
        """
        if not isinstance(mounts, list):
            mounts = [mounts]
        for mount in mounts:
            if mount in DeviceInfo.bans:
                return True
        return False

    def parse_lsblk(self):
        """ Parse ls_blk for all the goodies we need """
        def get_backing_file(loop_device):
            try:
                with open(f'/sys/block/{loop_device}/loop/backing_file', 'r',
                          encoding='utf-8') as f:
                    backing_file = f.read().strip()
                return backing_file
            except FileNotFoundError:
                return ''

        def eat_one(device):
            entry = self.make_partition_namespace('', '')
            entry.name=device.get('name', '')
            entry.type = device.get('type', '')
            entry.readonly = bool(device.get('ro', 0))
            entry.fstype = device.get('fstype', '')
            if entry.fstype is None:
                entry.fstype = ''
            entry.label = device.get('label', '')
            if not entry.label:
                entry.label=device.get('partlabel', '')
            if entry.label is None:
                entry.label = ''
            entry.size_str=device.get('size', '')
            entry.uuid = device.get('uuid', '')
            mounts = device.get('mountpoints', [])
            while len(mounts) >= 1 and mounts[0] is None:
                del mounts[0]
            entry.mounts = mounts
            if entry.type == 'loop':
                entry.back_file = get_backing_file(entry.name)
            return entry

               # Run the `lsblk` command and get its output in JSON format with additional columns
        try:
            result = subprocess.run(['lsblk', '-J', '-o',
                        'NAME,MAJ:MIN,TYPE,RO,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,UUID,MOUNTPOINTS', ],
                        stdout=subprocess.PIPE, text=True, check=False,
                        timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # This runs from the menu-refresh timer: block the event loop for
            # the whole deadline and the tray looks dead. Keep the previous
            # snapshot instead (it is {} on the very first poll) and let the
            # next tick try again.
            prt(f'lsblk timed out after {PROBE_TIMEOUT}s {Utils.where()}')
            return self.entries
        parsed_data = json.loads(result.stdout)
        dev_cons, file_cons = {}, {}

        # Parse each block device and its properties
        for device in parsed_data['blockdevices']:
            parent = eat_one(device)
            parent.fstype = self.get_device_vendor_model(parent.name)
            for child in device.get('children', []):
                entry = eat_one(child)
                # entry.parent = parent.name
                entry.parent = parent
                if not parent.fstype:
                    parent.fstype = 'DISK'
                if parent.type == 'loop':
                    # IN the case of the loop device (or file container)
                    # use the UUID of the container
                    entry.uuid = parent.uuid
                    entry.back_file = parent.back_file
                elif 'luks' not in entry.fstype.lower():
                    continue
                # if parent.name not in entries:
                    # entries[parent.name] = parent
                if entry.type == 'crypt':
                    file_cons[entry.uuid] = entry
                else:
                    dev_cons[entry.uuid] = entry
                grandchildren = child.get('children', None)
                if entry.type == 'crypt':
                    if entry.mounts:
                        if self.is_banned(entry.mounts):
                            continue # skip whole disk entries
                        entry.upon = entry.mounts[0]
                elif not isinstance(grandchildren, list):
                    entry.opened = False
                    continue
                entry.opened = True
                grandchildren = child.get('children', [])
                for grandchild in grandchildren:
                    subentry = eat_one(grandchild)
                    subentry.parent = entry.name
                    entry.filesystems.append(subentry)
                    # entries[subentry.name] = subentry
                    if len(grandchildren) == 1 and len(subentry.mounts) == 1:
                        entry.upon = subentry.mounts[0]
                        # The device name in /proc/mounts is the subentry's name (e.g., /dev/mapper/luks_vol)
                        ns = self.tray.mount_infos.get(f'/dev/mapper/{subentry.name}',
                                              self.tray.mount_infos.get(subentry.name, None))
                        if ns:
                            entry.readonly = ns.readonly
                            # Optionally, you can also set entry.upon here if needed,
                            # but it's often better to rely on lsblk's MOUNTPOINTS data first.
                            if self.is_banned(entry.mounts):
                                continue # skip whole disk entries

        self.entries = dev_cons | file_cons
        if self.DB:
            #s = StringIO()
            temps = []
            for entry in self.entries.values():
                tmp_row = {}
                row = vars(entry)
                for key, value in row.items():
                #   if isinstance(value, SimpleNamespace):
                #       tmp_row[key] = str(vars(value))
                #   else:
                    tmp_row[key] = str(value)
                temps.append(tmp_row)
                #print(vars(entry), file=s)
            entries_str = json.dumps(temps, indent=4)
            if entries_str != self.prev_entries_str:
                dt = datetime.now().strftime('%m-%d^%H:%M:%S')
                print(f'\n\nDB: {dt} --->>> after parse_lsblk:')
                print(entries_str)
                self.prev_entries_str = entries_str
#               print(f'{back_files=}')

        return self.entries


    def get_relative(self, name):
        """ TBD """
        return self.entries.get(name, None)

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
    """ TBD """
    singleton = None
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
        LuksTray.singleton = self
        self.uid = os.environ.get('SUDO_UID', os.getuid())
        self.gid = os.environ.get('SUDO_GID', os.getgid())

        self.ini_tool = ini_tool
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.mono_font = QFont("Consolas", 10)
        self.mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self.emoji_font = self.get_emoji_font()
        # self.mono_font = QFont("DejaVu Sans Mono", 10)
        self.app.setFont(self.mono_font)

        # self.has_emoji_font = bool("Noto Color Emoji" in QFontDatabase.families())
        # self.emoji_font = (QFont("Noto Color Emoji") if self.has_emoji_font
                           # else QFont("DejaVu Sans"))

        _, missing, self.udisks_cmd = self.check_dependencies()

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
                        # Copy directly instead of using Utils.copy_to_folder()
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


        # ??? Load JSON data
        # ??? self.load_data()
        self.lsblk = DeviceInfo(opts=opts, tray=self)
        self.mount_infos = {}
        self.upons = set()

        self.tray_icon = QSystemTrayIcon(self.icons['none'], self.app)
        self.tray_icon.setToolTip('luks-tray')
        self.tray_icon.setVisible(True)

        self.containers, self.menu = {}, QMenu()
        self.actions = []
        # Guards for the 3 s poller, see update_menu() and busy_operation().
        # Both must exist before the first update_menu() call below.
        self.refreshing = False
        self.operation = None
        self.update_menu()
        self.remove_unused_automounts()

        self.timer = QTimer(self.tray_icon)
        self.timer.timeout.connect(self.update_menu)
        # Repeating, but update_menu() self-guards: a tick that lands while a
        # refresh or a mount/unmount is still running is dropped instead of
        # stacking a second refresh on top of it.
        self.timer.start(3000)  # 3000 milliseconds = 3 seconds

    @staticmethod
    def check_dependencies(verbose=False):
        """ ensure the system utilities we need are available
            and discover which udisks command we are using
        """
        utilities = [
            'lsblk', 'cryptsetup', 'rmdir',
            'mount', 'umount', 'bindfs', 'losetup',
            'fuser', 'truncate', 'mkfs.ext4', 'bindfs',
            # 'kill', 'losetup', ['udisksctl', 'udisks', 'udisks2'],
        ]
        found, missing, udisks_cmd = [], [], None
        for entry in utilities:
            utils = entry if isinstance(entry, list) else [entry]
            got_one = False
            for util in utils:
                if shutil.which(util):
                    found.append(util)
                    got_one = True
                    if util.startswith('udisks'):
                        udisks_cmd = util
                    break
            if not got_one:
                missing.append(entry)
        if verbose or missing:
            for util in found:
                prt(f'✓  {util}')
            for util in missing:
                prt(f'✗  {util} - please install',
                    ' one of' if isinstance(util, list) else '')
        return found, missing, udisks_cmd

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
        """ TBD """
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
        """ TBD """
        return thing in self.mount_infos or thing in self.upons

    def merge_containers_history(self):
        """ TBD """
        self.history.restore()
        for container in self.containers.values():
            self.history.ensure_container(container)
        self.history.save()

    def show_partition_details(self, name):
        """ TBD """
        container = self.containers.get(name, None)
        if container is None:
            return
        details = f'DETAILS for {container.name}:\n'
        details += 'UUID={container.UUID}\n'
        QMessageBox.information(None, "Partition Details", details)

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
        """Update context menu with LUKS partitions."""
        # menu = QMenu()
        actions = []
        # menu.setFont(self.emoji_font)

        icon_key = 'none'
        do_alerts = self.ini_tool.get_current_val('show_anomaly_alerts')

        if self.history.status == 'locked':
            action = QAction('Click to enter master password', self.app)
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
                    # menu.addSeparator()
                    actions.append(None)
                    separated = True

                name = container.name
                if container.back_file:
                    name = container.back_file
                    if name.startswith('/home/'):
                        name = '~' + name[6:]

#               # Determine which emoji/symbol to show
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


                # Add the emoji-enhanced item
                # self.add_emoji_item(menu, emoji, text, callback)
                # prt(f'{emoji} {text}')
                action = QAction(f'{emoji} {text}', self.app)
                action.setFont(self.mono_font)  # applies to non-HTML paths
                                # Connect the left-click action
                if container.back_file:
                    action.triggered.connect(lambda checked,
                                 x=container.uuid: self.handle_file_click(x))
                else:
                    action.triggered.connect(lambda checked,
                                 x=container.uuid: self.handle_device_click(x))
                # menu.addAction(action)
                actions.append(action)


            # Other fixed menu entries
            if idx > 0 and not separated:
                # menu.addSeparator()
                actions.append(None)

            action = QAction('Create New Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_create_file_click)
            # menu.addAction(action)
            actions.append(action)

            action = QAction('Add Existing Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_add_file_click)
            # menu.addAction(action)
            actions.append(action)

            # menu.addSeparator()
            actions.append(None)

            if self.history.status in ('clear_text', 'unlocked'):
                verb = 'Set' if self.history.status == 'clear_text' else 'Update/Clear'
                action = QAction(f'{verb} Master Password', self.app)
                action.setFont(self.mono_font)
                action.triggered.connect(self.prompt_master_password)
                # menu.addAction(action)
                actions.append(action)

        action = QAction("Exit", self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.exit_app)
        # menu.addAction(action)
        actions.append(action)

        return self.replace_menu_if_different(actions, icon_key)


    def replace_menu_if_different(self, actions, icon_key):
        """ TBD """
        def replace_menu():
            nonlocal actions
            was_visible = self.menu and self.menu.isVisible()

            # self.menu = menu
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
            if action is None:
                return '<None>'
            if isinstance(action, QWidgetAction):
                widget = action.defaultWidget()
                if isinstance(widget, QLabel):
                    return widget.text()
                return '<widget>'
            return action.text()

        if not self.actions: # or menu.actions() != self.menu.actions():
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
            dialog = MountDeviceDialog(self.containers[uuid])
            dialog.exec()

    def handle_file_click(self, uuid):
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
        if uuid in self.containers:
            dialog = MountFileDialog(self.containers[uuid])
            dialog.exec()

    def handle_add_file_click(self):
        """ TBD """
        dialog = MountFileDialog(None)
        # prt('about to exec AddFileClick...')
        dialog.exec()

    def handle_create_file_click(self):
        """ TBD """
        dialog = MountFileDialog(None, create=True)
        dialog.exec()

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

    def prompt_master_password(self):
        """ Prompt for master passdword"""
        dialog = MasterPasswordDialog()
        dialog.exec()

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

    @staticmethod
    def get_auto_mount_root():
        """ Return the configured `auto_mount_folder` as an absolute path. """
        tray = LuksTray.singleton
        auto_root = tray.ini_tool.get_current_val('auto_mount_folder')
        auto_root = LuksTray.expand_real_user(auto_root)
        auto_root = os.path.abspath(auto_root)
        return auto_root

    @staticmethod
    def get_real_user_ids():
        """ Return (uid, gid) of the user this tray acts for.

            Under `sudo` these are SUDO_UID/SUDO_GID; otherwise they are the
            effective ids. The value is always an int -- SUDO_UID arrives as a
            string from the environment, which cannot be compared with the
            numeric st_uid reported by os.stat().
        """
        def as_int(value, fallback):
            try:
                return int(value)
            except (TypeError, ValueError):
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
                return os.path.join('/home', real_user)
        return os.path.expanduser('~')

    @staticmethod
    def validate_auto_mount_root(auto_root=None):
        """ Return None if `auto_root` may be cleaned, else an error string.

            [`remove_unused_automounts()`](main.py) deletes (empty)
            subdirectories below `auto_mount_folder`, which comes straight from
            config.ini, so a typo (`/`, `/home`) or a shared system path must not
            turn the startup cleanup into an `rmdir` sweep over unrelated paths.
            The folder therefore has to look like a *dedicated* vault area:

            - an absolute path, at least two levels deep, not in/under a system
              tree, and not a symlink (which could point anywhere);
            - neither the real user's home nor an ancestor of it, and not inside
              another user's home;
            - if it exists: a plain directory, not a mount point, owned by the
              real user or by root (the tray creates it as root when run under
              sudo), and not writable by group/other.

            The checks are deliberately conservative: when in doubt, skip the
            cleanup and log why (`remove_unused_automounts` treats any returned
            string as "do nothing").
        """
        if auto_root is None:
            auto_root = LuksTray.get_auto_mount_root()
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
    def generate_auto_mount_folder():
        """ Generate auto mount folder"""

        def available(basename):
            nonlocal auto_root, tray
            fullpath = os.path.join(auto_root, basename)
            if fullpath in tray.upons or fullpath in tray.history.upons:
                return None
            return fullpath

        tray = LuksTray.singleton
        auto_root = LuksTray.get_auto_mount_root()
        if os.path.exists(auto_root):
            if not os.path.isdir(auto_root):
                # `assert` is stripped by `python -O`, and the caller would
                # then hand out mount points inside a non-directory.
                raise RuntimeError(
                    f'auto_mount_folder ({auto_root!r}) exists but is not a directory')
        else:
            # os.makedirs(auto_root)
            run_cmd(['mkdir', auto_root])

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
        except Exception:                               # pylint: disable=broad-except
            return False
        if child == parent:
            return False
        return child.startswith(parent.rstrip(os.sep) + os.sep)

    @staticmethod
    def remove_empty_dir(path):
        """ rmdir `path` if (and only if) it is an empty, plain directory.

            This is intentionally unprivileged: the caller has validated the
            auto-mount root (see [`validate_auto_mount_root()`](main.py)), so
            `os.rmdir` suffices and a failure is simply reported instead of
            escalating to sudo. `rmdir` itself refuses to delete non-empty
            directories, and mount points are skipped here as well.
        """
        if os.path.islink(path) or not os.path.isdir(path):
            return False
        if os.path.ismount(path):
            return False
        try:
            os.rmdir(path)              # only removes empty dirs
            return True
        except OSError:
            return False                # not empty or permission denied

    @staticmethod
    def remove_if_auto(mount):
        """Remove target_dir if it is empty and within parent_dir"""
        parent_dir = LuksTray.get_auto_mount_root()
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
            [`validate_auto_mount_root()`](main.py)); when it is not a
            dedicated, user-owned directory the whole pass is skipped. Returns
            the number of directories removed.
        """
        parent_dir = LuksTray.get_auto_mount_root()
        problem = LuksTray.validate_auto_mount_root(parent_dir)
        if problem:
            prt(f'WARN: skipping auto-mount cleanup: {problem}')
            return 0
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
            except OSError:
                continue
            if stat_info.st_uid not in allowed_uids:
                continue  # Someone else's directory: not ours to delete
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

class CommonDialog(QDialog):
    """ TBD """
    home_dir = None
    dot_vault_dir = None # ~/.Vaults (default crypts)
    vault_dir = None # ~/Vaults (default mount area)

    def __init__(self):
        super().__init__(parent=LuksTray.singleton.dialog_parent)
        self.setWindowRole("dialog")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowSystemMenuHint |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.main_layout = QVBoxLayout()
        self.button_layout = QHBoxLayout()
        self.password_toggle = None
        self.password_input = None
        self.items = []
        self.inputs = {}
        self.progress_label = None
        self.progress_bar = None
        self.get_real_user_home_directory() # populate home/vault dir

    def set_title(self, title):
        """ Sets the window title ... if sway, putting it in the dialog box """
        self.setWindowTitle(title)
        if IS_SWAY_LIKE_ENV:
            title_label = QLabel(title)
            title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Apply some styling to make it look like a title bar
            title_label.setStyleSheet("""
                QLabel {
                    background-color: #333;
                    color: white;
                    padding: 5px;
                    font-weight: bold;
                    border-bottom: 1px solid #555;
                }
            """)
            # Insert the manual title at the very top of the layout
            self.main_layout.addWidget(title_label)

    def showEvent(self, event):
        """
        Called automatically by Qt immediately before the dialog is shown.
        This is the most reliable place to adjust position based on final size.
        """
        # 1. Finalize size and get screen info
        # This forces the layout to calculate the final width/height of the dialog
        self.adjustSize()

        cursor_pos = QCursor.pos()
        dialog_width = self.width()
        dialog_height = self.height()

        # Get the screen where the cursor currently is, which is the most reliable
        screen = self.screen() or QApplication.primaryScreen()
        screen_geometry = screen.geometry()

        # 2. Calculate initial position (centered below cursor, with a small offset)
        x = cursor_pos.x() - (dialog_width // 2)
        y = cursor_pos.y() + 20

        # 3. Add Explicit Boundary Checks

        # Horizontal (X-Axis) Checks
        if x < screen_geometry.left():
            x = screen_geometry.left()
        elif (x + dialog_width) > screen_geometry.right():
            x = screen_geometry.right() - dialog_width

        # Vertical (Y-Axis) Checks
        # If dialog is launching from the top tray, it's very likely to hit the top
        if y < screen_geometry.top():
            y = screen_geometry.top()
        elif (y + dialog_height) > screen_geometry.bottom():
            y = screen_geometry.bottom() - dialog_height

        # 4. Move the dialog to the adjusted position
        self.move(x, y)

        # IMPORTANT: Call the base class implementation
        super().showEvent(event)

    def hide_password(self):
        """Hide the password programmatically."""
        if self.password_toggle and self.password_input:
            self.password_toggle.setChecked(False)
            self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.password_toggle.setText("👁️")

    def show_password(self):
        """Show the password programmatically."""
        if self.password_toggle and self.password_input:
            self.password_toggle.setChecked(True)
            self.password_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self.password_toggle.setText("●")


    def add_line(self, text):
        """ TBD """
        label = QLabel(text)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """ TBD """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)

    def add_input_field(self, keys, label_texts, placeholder_texts, char_width=5,
                       field_type='text', add_on=''):
        """ Adds a label and a line edit input to the main layout. """
        tray = LuksTray.singleton
        field_layout = QHBoxLayout() # Create a horizontal layout for the label and input field

        if not isinstance(keys, list):
            keys = [keys]

        # Normalize both arguments to lists *before* padding them: a scalar
        # label/placeholder is repeated for every key, and appending to a
        # still-scalar `placeholder_texts` used to raise AttributeError.
        if not isinstance(label_texts, (list, tuple)):
            label_texts = [label_texts]
        else:
            label_texts = list(label_texts)
        if not isinstance(placeholder_texts, (list, tuple)):
            placeholder_texts = [placeholder_texts]
        else:
            placeholder_texts = list(placeholder_texts)

        if not label_texts:
            label_texts = ['']
        if not placeholder_texts:
            placeholder_texts = ['']

        if len(label_texts) == 1 and len(keys) > 1:
            label_texts = label_texts * len(keys)
        if len(placeholder_texts) == 1 and len(keys) > 1:
            placeholder_texts = placeholder_texts * len(keys)

        if len(label_texts) != len(keys) or len(placeholder_texts) != len(keys):
            raise ValueError(
                f'add_input_field: {len(keys)} key(s) but '
                f'{len(label_texts)} label(s) and '
                f'{len(placeholder_texts)} placeholder(s)')

        for idx, key in enumerate(keys):
            label_text = label_texts[idx]
            placeholder_text = placeholder_texts[idx]

            label = QLabel(label_text) # Create a QLabel for the label text
            # Set the width of the input field based on character width
            # Approximation: assuming an average of 8 pixels per character for a monospace font
             # You can adjust this factor based on the font
            if field_type == 'checkbox':
                input_field = QCheckBox()
                # For checkbox, use placeholder_text as the checkbox label instead of separate label
                input_field.setText(label_text)
                input_field.setChecked(False)  # Default unchecked
                field_layout.addWidget(input_field)

            elif field_type == 'text':
                input_field = QLineEdit()
                input_field.setText(placeholder_text.strip())
                char_width = max(len(placeholder_text), char_width)
                input_field.setFixedWidth(char_width * 10)
                field_layout.addWidget(label)
                field_layout.addWidget(input_field)
            else:
                # Validation must survive `python -O`; the old `assert` also
                # left `input_field` unbound for the code that follows.
                raise ValueError(f'invalid field_type {field_type!r}')

            if add_on == 'password':
                exposed = tray.ini_tool.get_current_val('show_passwords_by_default')
                exposed = False if placeholder_text else exposed # don't show existing passwords
                self.password_input = input_field
                self.password_toggle = QPushButton("●")
                self.password_toggle.setFixedWidth(30)
                self.password_toggle.setCheckable(True)
                if exposed:
                    self.show_password()
                else:
                    self.hide_password()
                self.password_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                self.password_toggle.clicked.connect(self.toggle_password_visibility)
                field_layout.addWidget(self.password_toggle)

            if add_on == 'folder': # Create a Browse button
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Prevent the button from gaining focus
                button.clicked.connect(partial(self.browse_folder, input_field))
                field_layout.addWidget(button)

            if add_on == 'file': # Create a Browse button for existing file
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Prevent the button from gaining focus
                button.clicked.connect(partial(self.browse_file, input_field))
                field_layout.addWidget(button)

            if add_on == 'new_file': # Create a Browse button for new file
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Prevent the button from gaining focus
                button.clicked.connect(partial(self.browse_new_file, input_field))
                field_layout.addWidget(button)


            self.inputs[key] = input_field
        self.main_layout.addLayout(field_layout) # Add horizontal layout to main vertical layout

    def get_real_user_home_directory(self):
        """Returns the home directory of the real user when running under sudo."""
        if not self.home_dir:
            real_user = os.environ.get('SUDO_USER')
            if real_user:
                self.home_dir = os.path.join("/home", real_user)  # Assumes standard home directory structure
            else:
                self.home_dir = os.path.expanduser("~")  # Fallback to current user's home directory
            self.vault_dir = os.path.join(self.home_dir, 'Vaults')
            self.dot_vault_dir = os.path.join(self.home_dir, '.Vaults')
        return self.home_dir

    def browse_folder(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        # `home_dir` is a plain string, not a method -- calling it raised
        # TypeError when the field was empty.
        initial_dir = initial_dir if initial_dir else self.get_real_user_home_directory()

        # Open the folder dialog starting at the determined directory
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder", initial_dir)
        if folder_path:
            input_field.setText(folder_path)  # Update the input field with the selected folder path

    def browse_file(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        if not initial_dir:
            if os.path.exists(self.dot_vault_dir):
                initial_dir = self.dot_vault_dir
            else:
                initial_dir = self.home_dir

        # Open the folder dialog starting at the determined directory
        file_path = QFileDialog.getOpenFileName(self, "Select Existing File", initial_dir)
        # returns tuple (path, type of file)
        if file_path[0]:
            input_field.setText(file_path[0])  # Update the input field with the selected folder path

    def browse_new_file(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        if not initial_dir:
            if os.path.exists(self.dot_vault_dir):
                initial_dir = self.dot_vault_dir
            else:
                initial_dir = self.home_dir

        # Open the folder dialog starting at the determined directory
        file_path = QFileDialog.getSaveFileName(self, "Select New File", initial_dir)
        # returns tuple (path, type of file)
        if file_path[0]:
            input_field.setText(file_path[0])  # Update the input field with the selected folder path

    def toggle_password_visibility(self):
        """Toggle password visibility."""
        if self.password_toggle.isChecked(): # password to be exposed
            self.show_password()
        else: # password is to be hidden
            self.hide_password()

    def cancel(self, _=None):
        """ null function"""
        self.reject()

    def alert_errors(self, error_lines):
        """Callback to show errors if present."""
        if error_lines:  # Check if there are any errors
            error_text = '\n'.join(error_lines)  # Join the list of error lines into one string

            error_dialog = QMessageBox(self)
            error_dialog.setIcon(QMessageBox.Icon.Critical)  # Set the icon to show it's an error
            error_dialog.setWindowTitle("Errors Detected")
            error_dialog.setText("The following errors were encountered:")
            error_dialog.setInformativeText(error_text)
            error_dialog.setStandardButtons(QMessageBox.StandardButton.Ok)  # Add a dismiss button
            error_dialog.exec()  # Show the message box

                    # Ensure the parent dialog regains focus
            self.raise_()
            self.activateWindow()

    def show_progress(self, message):
        """Show progress indicator and disable buttons."""
        for button in self.findChildren(QPushButton):
            button.setEnabled(False)

        if not self.progress_label:
            self.progress_label = QLabel()
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)  # Indeterminate
            self.progress_bar.setValue(10)
            self.main_layout.addWidget(self.progress_label)
            self.main_layout.addWidget(self.progress_bar)

        self.progress_label.setText(message)
        self.progress_label.show()
        self.progress_bar.show()
        # Paint the dialog now, but *without* spinning the event loop: this
        # used to be QApplication.processEvents(), which dispatched the tray's
        # 3 s QTimer (and any queued click) in the middle of the operation and
        # re-entered update_menu() while cryptsetup was still running.
        # activate() + repaint() shows the new widgets with no such risk.
        if self.layout() is not None:
            self.layout().activate()
        self.repaint()

    def hide_progress(self):
        """Hide progress indicator and re-enable buttons."""
        if hasattr(self, 'progress_label'):
            self.progress_label.hide()
            self.progress_bar.hide()

        for button in self.findChildren(QPushButton):
            button.setEnabled(True)

    @staticmethod
    def check_upon(text, mount_points, is_device=False):
        """ Validate candidate mount point.
            `mount_points` must be the set of *currently mounted mount points*
            (i.e. [`LuksTray.upons`](main.py)), **not** `update_mounts()`, which
            returns the set of device keys.
            Returns error (or None if no error)
        """
        if not text:
            return 'ERR: empty field not allowed for devices or files'
        if not os.path.isabs(text):
            return f'ERR: mount point ({text}) must be absolute path'
        try:
            text = os.path.abspath(text) # normalize
        except Exception:
            pass

        if DeviceInfo.is_banned(text):
            return f'ERR: cannot mount on "special" {text}'
        auto_root = LuksTray.get_auto_mount_root()

        if text == auto_root:
            return 'ERR: cannot mount on auto_mount_folder itself'
        if text.startswith('/media/'):
            if is_device:
                return 'ERR: cannot mount device in /media'
            return 'ERR: use empty field for automatic /media mounting'
        parent_dir = os.path.dirname(text)
        auto_root = LuksTray.get_auto_mount_root()
        parent_exists = os.path.isdir(parent_dir)
        if not parent_exists and os.path.abspath(parent_dir) != auto_root:
            return f'ERR: parent directory ({parent_dir}) does not exist'
        if os.path.exists(text):
            if not os.path.isdir(text):
                return f'ERR: mount point ({text}) exists but is not a directory'
            if len(os.listdir(text)) > 0:
                return f'ERR: mount point ({text}) exists but is not empty'
        if text in mount_points:
            return f'ERR: mount point ({text}) occupied'
        return None

    ####################################################
    # LUKS Primitives
    ####################################################
    def _unlock_luks(self, device_path, password, luks_device, readonly=False):
        """Common LUKS unlock logic"""
        # No "already unlocked?" short-circuit here on purpose. It used to be
        # `if hasattr(self, 'opened') and self.opened`, but `self` is the
        # dialog, which never defines `opened`, so the test never fired.
        # Making it live (e.g. "the mapper path exists") would skip the
        # password check and mount a volume somebody else has open, so a
        # repeat `cryptsetup open` is left to fail with its own message.
        args = ['cryptsetup', 'open', '--type', 'luks']
        if readonly:
            args.append('--readonly')
        args += ['--key-file', '-', device_path, luks_device]
        return sudo_cmd(args, input_str=password)


    def _mount_manual(self, tray, mapper_path, upon, do_bindfs=False, readonly=False):
        """Manual mounting with bindfs"""
        if readonly:
            err = sudo_cmd(['mount', '-o', 'ro', mapper_path, upon])
        else:
            err = sudo_cmd(['mount', mapper_path, upon])
        if do_bindfs and not err:
            err = sudo_cmd(['bindfs', '-u', str(tray.uid), '-g', str(tray.gid),
                          upon, upon])
        return err

    def _setup_loop_device(self, container):
        """Set up loop device for file-based containers"""
        # The same dead `hasattr(self, 'opened')` test lived here: `self` is
        # the dialog, so it never fired -- and it tested the wrong object
        # anyway, since the loop state ends up on `container.name` below.

        # Use --show to get the loop device name; we need its stdout, so call
        # subprocess directly instead of sudo_cmd() (which only reports errors).
        args = ['losetup', '-f', '--show', container.back_file]
        rc, stdout, stderr = sudo_run(args)
        if rc != 0:
            return (f"FAIL: {' '.join(args)}: "
                    f'{stdout.strip()} {stderr.strip()} [rc={rc}]'), None

        loop_device = stdout.strip()
        if not loop_device:
            return f"FAIL: {' '.join(args)}: no loop device returned", None

        # Update container.name to match the loop device (e.g., 'loop0')
        container.name = os.path.basename(loop_device)

        return None, loop_device

    ####################################################
    # LUKS Generic Mounter
    ####################################################
    def mount_luks_container(self, tray, container, password, upon=None, luks_device=None,
                            readonly=False, luks_file=None, size=None):
        """
        Unified function to mount any LUKS container (device or file).

        Args:
            container: Container object
            password: LUKS password
            upon: Manual mount point (None for auto-mounting)
            luks_device: Device mapper name (for devices)
            luks_file: Path to LUKS file (for files)
            size: Size for new file creation
        """
        if not upon:
            # This function reports failure by returning a string (callers do
            # `if err:`), and must keep doing so under `python -O`, where an
            # `assert` would vanish and let a bogus mount point reach `mount`.
            return 'ERR: cannot mount without a mount point'
        try:
            # Determine if this is a file or device container
            is_file_container = luks_file is not None

            if is_file_container:
                # Handle file container setup
                needs_filesystem = False
                luks_device = os.path.basename(luks_file) + '-luks'
                device_path = luks_file

                # Create file if needed
                if size is not None:
                    # this ensures the luks file is creatable by the user,
                    # and if so, exists
                    luks_file = os.path.abspath(luks_file)
                    luks_dirname = os.path.dirname(luks_file)
                    err = None
                    if luks_dirname == self.dot_vault_dir:
                        if not os.path.exists(luks_dirname):
                            err = run_cmd(['mkdir', '-p', luks_dirname])
                    if not err:
                        err = run_cmd(['touch', luks_file])
                    if not err:
                        err = sudo_cmd(['truncate', '-s', f'{size}M', luks_file])
                    if not err:
                        # Run luksFormat through sudo like every other
                        # privileged operation: `sudo -n` never prompts and
                        # returning rc/stderr keeps the real failure visible
                        # instead of swallowing it in the outer except.
                        args = ['cryptsetup', 'luksFormat', '--type', 'luks2']
                        args += ['--batch-mode', '--key-file', '-', luks_file]
                        rc, stdout, stderr = sudo_run(args, input_str=f'{password}',
                                                      timeout=FORMAT_TIMEOUT)
                        if rc != 0:
                            err = (f"FAIL: {' '.join(args)}: {stdout} {stderr} "
                                   f'[rc={rc}]')
                    if err:
                        return err
                    needs_filesystem = True
            else:
                # Handle device container setup
                luks_device = luks_device or f'{container.name}-luks'
                device_path = f'/dev/{container.name}'
                needs_filesystem = False

                # Setup loop device if needed
                if hasattr(container, 'back_file') and container.back_file:
                    err, device_path = self._setup_loop_device(container)
                    if err:
                        return err

            # Manual mounting always: unlock with cryptsetup, then mount manually
            err = self._unlock_luks(device_path, password, luks_device, readonly=readonly)
            if err:
                return err

            mapper_path = f'/dev/mapper/{luks_device}'

            # Create filesystem if needed (for new files)
            if needs_filesystem:
                # mke2fs writes every inode/bitmap up front: minutes on a
                # large container, so it gets the generous deadline.
                err = sudo_cmd(['mkfs.ext4', mapper_path], timeout=FORMAT_TIMEOUT)
                if err:
                    return err

            err = self._mount_manual(tray, mapper_path, upon,
                    do_bindfs=bool(luks_file), readonly=readonly)
            return err

        except Exception as e:
            return f"An error occurred: {str(e)}"


class MasterPasswordDialog(CommonDialog):
    """ TBD """
    def __init__(self):
        super().__init__()
        self.set_title('Master Password Dialog [luks-tray]')
        self.add_input_field('password', "Master Password", '', 24, add_on='password')
        self.add_push_button('OK', self.set_master_password)
        self.add_push_button('Cancel', self.cancel)
        self.add_push_button('Remove Password', self.clear_master_password)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)


    def clear_master_password(self, _):
        """ TBD """
        self.set_master_password(_, force_clear=True)

    def set_master_password(self, _, force_clear=False):
        """ TBD """
        tray = LuksTray.singleton
        field = self.inputs.get('password', None)
        errs = []
        password = '' if not field or force_clear else field.text().strip()
        if tray.history.status == 'locked':
            tray.history.master_password = password
            if password:
                tray.history.restore()
                if tray.history.status != 'unlocked':
                    tray.history.master_password = ''
                    errs.append(f'failed to unlock {repr(tray.history.path)}')
            else:
                err = tray.history.save(force=True)
                if err:
                    errs.append(err)
                else:
                    tray.history.status = 'clear_text'
        elif tray.history.status in ('unlocked', 'clear_text'):
            tray.history.master_password = password
            err = tray.history.save(force=True)
            if err:
                tray.history.master_password = ''
                errs.append(err)
            elif password:
                tray.history.status = 'locked'
            else:
                tray.history.status = 'clear_text'
        if errs:
            self.alert_errors(errs)
        else:
            self.accept()

class MountDeviceDialog(CommonDialog):
    """ TBD """
    def __init__(self, container):
        super().__init__()
        tray = LuksTray.singleton

        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts
        # mounts if there are
        if mounts:  # unmount dialog
            self.set_title('Unmount and Close Device [luks-tray]')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container.opened:  # close dialog: unlocked but nothing mounted
            self.set_title('Close Unmounted Device [luks-tray]')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            # `mounts` is empty in this branch by definition (that is what
            # selected it), so joining it rendered a bare "Close ?". Name the
            # mapper device when it is known, else the container itself.
            mappers = [fs.name for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
            target = ', '.join(mappers) if mappers else container.name
            self.add_line(f'Close {target}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.set_title('Mount Device [luks-tray]')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.name}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, add_on='password')
            where = vital.upon
            if not vital.upon:
                where = os.path.join(self.vault_dir, container.name)
            self.add_input_field('upon', "Mount At", where, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
            if container.label:
                self.add_line(f'Label: {container.label}')
            self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        self.setLayout(self.main_layout)

    def mount_device(self, uuid):
        """Attempt to mount the partition."""

        tray, container = LuksTray.singleton, None
        errs, values = [], {}

        if tray:
            container = tray.containers.get(uuid, None)

        if not container:
            errs.append(f'ERR: container w UUID={uuid} not found')
            return

        # `errs` holds *errors only*. The container name used to be pushed in
        # as a placeholder so callers could test `len(errs) <= 1`; that made
        # the first element magic and the emptiness test off by one. The
        # context line is now added only where the dialog is built.
        tray.update_mounts()

        # Parse and validate inputs
        for key, field in self.inputs.items():
            text = field.text().strip()
            values[key] = text

            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')
            elif key == 'upon':
                err = self.check_upon(text, tray.upons, is_device=True)
                if err:
                    errs.append(err)
            elif key == 'readonly':
                values[key] = field.isChecked()
            else:
                errs.append(f'ERR: unknown key({key})')

        # Determine the device-mapper name for `cryptsetup open`. Normally the
        # LUKS header has exactly one child filesystem whose name *is* the
        # mapper name. Any other case (no filesystem parsed yet, or several)
        # used to yield '' and end up as an empty argument to `cryptsetup
        # open`, so derive a deterministic name from the container instead.
        filesystems = [fs for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
        if len(filesystems) == 1:
            luks_device = filesystems[0].name
        else:
            luks_device = f'{container.name}-luks'

        # Proceed with mounting if no errors
        if not errs:
            mount_point = values['upon']
            if mount_point and not os.path.exists(mount_point):
                # os.makedirs(mount_point, exist_ok=True)
                sudo_cmd(['mkdir', '-p', mount_point])

            self.hide_password()
            self.show_progress('Mount device...')
            try:
                # Keep the poller out until cryptsetup open *and* mount have
                # both finished; a refresh in between rebuilds the menu from
                # a half-mounted container.
                with tray.busy_operation('mount device'):
                    err = self.mount_luks_container(tray, container, values['password'],
                            upon=mount_point, readonly=values['readonly'],
                            luks_device=luks_device)
            finally:
                self.hide_progress()

            if err:
                errs.append(err)

        if errs:
            self.alert_errors([f'{container.name}'] + errs)
            return

        tray.update_history(uuid, values)

        tray.update_menu()
        self.accept()

    def unmount_device(self, uuid):
        """Attempt to unmount the partition."""

        errs, container = [], None
        tray = LuksTray.singleton
        container = tray.containers.get(uuid, None)

        # Show progress - disable buttons and add progress indicator
        self.show_progress("Unmount/Close device...")
        try:
            # No menu refresh between the umount and the cryptsetup close.
            with tray.busy_operation('unmount device'):
                tray.update_mounts()
                busy_warns = set()

                if container:
                    unmounteds = []
                    filesystems = [fs for fs in container.filesystems
                                   if getattr(fs, 'name', '')]
                    for fs in filesystems:
                        for mount in fs.mounts:
                            ### self.kill_bindfs_on_mount(mount)
                            if tray.is_mounted(mount):
                                err = run_unmount(mount, busy_warns)
                                if err:
                                    errs.append(err)
                                else:
                                    unmounteds.append(mount)

                    # cryptsetup close takes the *mapper* name, which lives in
                    # the child filesystem entries. An opened container may
                    # legitimately have none -- the '‼' open-but-unmounted
                    # anomaly -- so fall back to the container's own name (file
                    # containers are their own mapper) and, failing that,
                    # report a usable error.
                    close_name = filesystems[0].name if filesystems else ''
                    if not close_name and container.type == 'crypt':
                        close_name = container.name
                    if not errs:
                        if close_name:
                            sudo_cmd(["cryptsetup", "close", close_name], errs)
                        else:
                            errs.append(
                                f'ERR: cannot close {container.name}: no mapper '
                                f'device name known (no filesystem detected)')
                    if not errs:
                        for mount in unmounteds:
                            LuksTray.remove_if_auto(mount)
                else:
                    errs.append(f'ERR: container w UUID={uuid} not found')
        finally:
            # Hide progress indicator
            self.hide_progress()

        if errs:
            if not busy_warns:
                # Name the container first, as the old placeholder element did.
                heading = [f'{container.name}'] if container else []
                self.alert_errors(heading + errs)
            return # don't close dialog box

        tray.update_menu()
        self.accept()

class MountFileDialog(CommonDialog):
    """ TBD """
    def __init__(self, container, create=False):
        super().__init__()
        tray = LuksTray.singleton

        # mounts if there are
        if container and container.opened:  # unmount dialog
            if container.mounts:
                self.set_title('Unmount/Close Crypt File [luks-tray]')
                self.add_line(f'{container.back_file}')
                self.add_line(f'Unmount {container.upon}')
            else:
                self.set_title('Close Crypt File [luks-tray]')
                self.add_line(f'{container.back_file}')
            self.add_push_button('OK', self.unmount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container:
            self.set_title('Mount Crypt File [luks-tray]')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.back_file}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, add_on='password')
            where = vital.upon if vital.upon else self.vault_dir
            self.add_input_field('upon', "Mount At", where, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
            if container.uuid:
                self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        elif not create: # no container ... use existing file (not creating)
            self.set_title('Add Existing Crypt File [luks-tray]')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('back_file', "Crypt File", '', 48, add_on='file')
            # where = LuksTray.generate_auto_mount_folder()
            # self.add_input_field('upon', "Mount At", where, 36, add_on='folder')
            self.add_input_field('upon', "Mount At", self.vault_dir, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        else: # no container ... create crypt file
            self.set_title('Create New Crypt File [luks-tray]')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('size_str', "Size (MiB)", '32', 8)
            self.add_input_field('back_file', "Crypt File", self.dot_vault_dir, 48, add_on='new_file')
            self.add_input_field('overwrite_ok', "Enable Overwrite of Existing File",
                                 '', 48, field_type='checkbox')
            # where = LuksTray.generate_auto_mount_folder()
            self.add_input_field('upon', "Mount At", self.vault_dir, 36, add_on='folder')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        self.setLayout(self.main_layout)

    def unmount_file(self, uuid):
        """Attempt to unmount the partition."""
        # Here you would implement the unmount logic.
        errs, container = [], None
        tray = LuksTray.singleton
        container = tray.containers.get(uuid, None)
        self.show_progress('Unmount/Close crypt file...')
        try:
            with tray.busy_operation('unmount file'):
                busy_warns = set()
                if container:
                    if container.mounts:
                        for _ in range(2):
                            # it may take two dismounts, one for the regular
                            # mount, and one for the bindfs mount
                            tray.update_mounts()
                            for mount in container.mounts:
                                if tray.is_mounted(mount):
                                    err = run_unmount(mount, busy_warns)
                                    if err:
                                        errs.append(err)
                                    else:
                                        LuksTray.remove_if_auto(mount)

                            if busy_warns:
                                break

                    if not errs:
                        sudo_cmd(["cryptsetup", "close", container.name], errs=errs)
                        # If this is a file container with a loop device,
                        # detach it
                    if not errs and container.back_file:
                        ignores = []
                        sudo_cmd(["losetup", "-d", f"/dev/{container.name}"],
                                 errs=ignores)
        finally:
            self.hide_progress()

        tray.update_menu()
        if errs:
            if not busy_warns:
                self.alert_errors(errs)
        else:
            self.accept()

    def mount_file(self, uuid):
        """ TBD """

        tray, container = LuksTray.singleton, None
        errs, values = [], {}
        if tray is None:
            # `LuksTray.singleton` is assigned in LuksTray.__init__; the old
            # `assert` (stripped by `python -O`) merely deferred the failure
            # to the first `tray.` dereference below.
            self.alert_errors(['ERR: tray is not initialized'])
            return

        if uuid is None:
            container = DeviceInfo.make_partition_namespace('', '')
            container.opened = False
        else:
            container = tray.containers.get(uuid, None)
            if not container:
                errs.append(f'ERR: container w UUID={uuid} not found')
                return
        # Label for the error dialog; kept out of `errs` so that list contains
        # errors only (the `if not errs` / `if errs` tests below stay honest).
        context = (container.back_file
                   if (not container.name and container.back_file)
                   else container.name)

        tray.update_mounts()
        mount_point = None

        for key, field in self.inputs.items():
            if isinstance(field, QCheckBox):
                values[key] = field.isChecked()
                continue

            # Assume text fields..
            text = field.text().strip()
            values[key] = text
            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')

            elif key == 'back_file':
                path = os.path.abspath(text)
                values[key] = path
                dirname = os.path.dirname(path)
                if not os.path.isdir(dirname):
                    if os.path.basename(dirname) == self.dot_vault_dir:
                        err = run_cmd(['mkdir', '-p', dirname])
                        if err:
                            errs.append(err)
                if not os.path.isdir(os.path.dirname(dirname)):
                    errs.append(f'ERR: Crypt File {path} must be in an existing directory')

            elif key == 'upon':
                path = os.path.abspath(text)
                if 'back_file' in values or container.back_file:
                    back_file = container.back_file if container.back_file else values['back_file']
                    if path == self.vault_dir:
                        basename = os.path.basename(back_file)
                        for suffix in ('.luks', '.luks2', '.crypt'):
                            if basename.endswith(suffix):
                                if len(basename) > len(suffix):
                                    basename = basename[:-len(suffix)]
                        path = os.path.join(self.vault_dir, basename)

                mount_point = path
                err = self.check_upon(path, tray.upons)
                if err:
                    errs.append(err)

            elif key == 'readonly':
                pass
            elif key == 'size_str':
                try:
                    size_str = values.get('size_str', None)
                    megs = int(size_str)
                    if megs < 32:
                        errs.append(f'at least 32 expected ... invalid size ({megs})')
                except Exception:
                    errs.append(f'"int" expected ... invalid size ({size_str})')


            else:
                errs.append(f'ERR: unknown key({key})')

        if not errs:
            if container.back_file:
                back_file = container.back_file
            else:
                back_file = values['back_file']

            if 'overwrite_ok' in values:
                overwrite_ok = values['overwrite_ok']
                if os.path.exists(back_file) and not overwrite_ok:
                    errs.append(f'cannot overwrite {back_file!r} w/o checking allowed')

        if not errs:
            self.show_progress('Mount file...')
            try:
                with tray.busy_operation('mount file'):
                    # `err` has to start out defined: with an existing mount
                    # point the mkdir branch below is skipped.
                    err = None
                    if mount_point and not os.path.exists(mount_point):
                        # os.makedirs(mount_point, exist_ok=True)
                        err = run_cmd(['mkdir', '-p', mount_point])
                    if not err:
                        err = self.mount_luks_container(tray, container,
                                values['password'], mount_point,
                                readonly=values['readonly'],
                                luks_file=back_file,
                                size=values.get('size_str', None))
            finally:
                self.hide_progress()

            if err:
                errs.append(err)
        if errs:
            self.alert_errors([f'{context}'] + errs)
            # self.accept()
            return

        # update history with new values if mount worked
        #
        # A new/added file still has uuid=None here. File entries are keyed by
        # the LUKS UUID inside the file (see get_luks_uuid_for_file), so look
        # it up now that the file exists; the path-derived UUID is a
        # best-effort fallback only (such an entry is culled on the next
        # restore if the real UUID cannot be verified).
        if not uuid:
            uuid = (container.uuid or get_luks_uuid_for_file(back_file)
                    or generate_uuid_for_file_path(back_file))
        tray.update_history(uuid, values, back_file=back_file)

        tray.update_menu()
        self.accept()


def rerun_module_as_root(module_name):
    """ rerun using the module name """
    if os.geteuid() != 0: # Re-run the script with sudo
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        vp = ['sudo', sys.executable, '-m', module_name] + sys.argv[1:]
        os.execvp('sudo', vp)

def main():
    """ TBD """
    import argparse
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
    opts = parser.parse_args()

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
        _, missing, _ = LuksTray.check_dependencies(verbose=True)
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
        # Detach stdin from the terminal: every command runs with `sudo -n`
        # and feeds its password over a pipe, so a child that tried to read
        # the tty would steal keystrokes from the user's shell. The price is
        # that there is no terminal left to interrupt a wedged child, which
        # is why every subprocess call carries a deadline (CMD_TIMEOUT /
        # PROBE_TIMEOUT in Utils) and cannot freeze the UI indefinitely.
        devnull_fd = os.open('/dev/null', os.O_RDWR)
        os.dup2(devnull_fd, sys.stdin.fileno())
        os.close(devnull_fd)

        ini_tool = IniTool(paths_only=False)
        Utils.prt_path = ini_tool.log_path

        tray = LuksTray(ini_tool, opts)
        sys.exit(tray.app.exec())

    except Exception as exce:
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)

if __name__ == "__main__":
    # mainBasic()
    main()
