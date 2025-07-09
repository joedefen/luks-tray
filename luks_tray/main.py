#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments
# pylint: disable=line-too-long,too-many-lines
import os
import sys
import stat
import json
import signal
import subprocess
import shutil
import traceback
import hashlib
import time
import importlib.resources
from pathlib import Path
from functools import partial
from io import StringIO
from datetime import datetime
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt6.QtWidgets import QFileDialog, QCheckBox, QSizePolicy
from PyQt6.QtWidgets import QProgressBar, QWidgetAction, QWidget
from PyQt6.QtGui import QIcon, QCursor, QAction, QFont, QFontDatabase, QFontInfo
from PyQt6.QtCore import QTimer, Qt
    # from PyQt6.QtWidgets import QLabel, QWidgetAction
    # from PyQt6.QtCore import Qt

from luks_tray.History import HistoryClass
from luks_tray.Utils import prt
from luks_tray import Utils
from luks_tray.IniTool import IniTool



def generate_uuid_for_file_path(file_path):
    """ Use SHA-256 to hash the file path """
    file_hash = hashlib.sha256(file_path.encode('utf-8')).hexdigest()

    # Take the first 32 characters of the hash and format it as a UUID
    # UUID is normally 32 hexadecimal digits, formatted as 8-4-4-4-12
    generated_uuid = f"{file_hash[:8]}-{file_hash[8:12]}-{file_hash[12:16]}-{file_hash[16:20]}-{file_hash[20:32]}"

    return generated_uuid


def run_cmd(args, errs=None, input_str=None):
    """ TBD """
    return rerun_if_busy(args, errs, repeat=0, delay=0.0, input_str=input_str)

def rerun_if_busy(args, errs, repeat=3, delay=0.5, input_str=None):
    """ Handles umounts if busy mostly"""
    err = None
    for loop in range(repeat+1):
        if loop > 0:
            time.sleep(delay)
        sub = subprocess.run( args, capture_output=True, text=True, check=False, input=input_str)
        if sub.returncode == 0:
            return None
        err = f'FAIL: {' '.join(args)}: {sub.stdout} {sub.stderr} [rc={sub.returncode}]'
        if 'busy' not in sub.stderr:
            break
    if err and errs:
        errs.append(err)
    return err


from PyQt6.QtWidgets import QMessageBox
import subprocess

def run_unmount(mount_point: str, busy_warns: set) -> str | None:
    """Attempts to unmount the given mount point.
    If it fails due to 'busy', show a popup with the list of processes using it.
    Returns error string or None on success.
    """
    try:
        sub = subprocess.run(["umount", mount_point],
                             capture_output=True, text=True)
    except Exception as e:
        return f"FAIL: umount {mount_point}: Exception: {e}"

    if sub.returncode == 0:
        return None  # success

    err = f"FAIL: umount {mount_point}: {sub.stdout.strip()} {sub.stderr.strip()} [rc={sub.returncode}]"

    if 'busy' not in sub.stderr.lower() or mount_point in busy_warns:
        return err  # Not a 'busy' error — no popup needed

    # Try to get processes using the mount point
    try:
        fuser = subprocess.run(["fuser", "-vm", mount_point],
                               capture_output=True, text=True, check=True)
        fuser = subprocess.run(
                ["fuser", "-vm", mount_point],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
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
    msg.setWindowTitle("Unmount Failed — Device Busy")
    msg.setText(f"'{mount_point}' busy by these processes:\n - {info}")
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()

    return err

class DeviceInfo:
    """ Class to dig out the info we want from the system."""
    bans = ('/', '/home', '/var', '/usr', '/tmp', '/opt', '/srv',
            '/boot', '/sys', '/proc', '/dev', '/run')

    def __init__(self, opts):
        self.opts = opts
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
        result = subprocess.run(['lsblk', '-J', '-o',
                    'NAME,MAJ:MIN,TYPE,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,UUID,MOUNTPOINTS', ],
                    stdout=subprocess.PIPE, text=True, check=False)
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

        assert not missing, f"missing system commands: {missing}"

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
        assert len(self.icons) == len(self.svgs)


        # ??? Load JSON data
        # ??? self.load_data()
        self.lsblk = DeviceInfo(opts=opts)
        self.mounteds = set()
        self.upons = set()

        self.tray_icon = QSystemTrayIcon(self.icons['none'], self.app)
        self.tray_icon.setToolTip('luks-tray')
        self.tray_icon.setVisible(True)

        self.containers, self.menu = [], None
        self.update_menu()

        self.timer = QTimer(self.tray_icon)
        self.timer.timeout.connect(self.update_menu)
        self.timer.start(3000)  # 3000 milliseconds = 3 seconds
        
    @staticmethod
    def check_dependencies(verbose=False):
        """ ensure the system utilities we need are available
            and discover which udisks command we are using
        """
        utilities = [
            'lsblk', 'cryptsetup',
            'mount', 'umount', 'bindfs',
            'kill', 'fuser', 'losetup', 'truncate',
            ['udisksctl', 'udisks', 'udisks2'],
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
        with open('/proc/mounts', 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    self.mounteds.add(parts[0]) # what is mounted is first
                    self.upons.add(parts[1])  # The mount point is the second field

    def is_mounted(self, thing):
        """ TBD """
        return thing in self.mounteds or thing in self.upons

    def update_menu(self):
        """ TBD """
        self.ini_tool.update_config()
        if self.history.status in ('unlocked', 'clear_text'):
            self.containers = self.lsblk.parse_lsblk()
            self.merge_containers_history()
            # add in the containers that are not mounted but in
            # the history
            for vital in self.history.vitals.values():
                container = self.containers.get(vital.uuid, None)
                if container:
                    container.vital = vital
                if not container and vital.back_file:
                    # insert known file container (present device containers
                    # should be in the containers list already)
                    ns = DeviceInfo.make_partition_namespace('', '')
                    ns.type = 'crypt'
                    ns.back_file = vital.back_file
                    ns.opened = False
                    ns.uuid = vital.uuid
                    ns.vital = vital
                    self.containers[vital.uuid] = ns

        self.update_menu_items()

    def merge_containers_history(self):
        """ TBD """
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

    def update_menu_items(self):
        """Update context menu with LUKS partitions."""
        menu = QMenu()
        # menu.setFont(self.emoji_font)

        icon_key = 'none'
        do_alerts = self.ini_tool.get_current_val('show_anomaly_alerts')

        if self.history.status == 'locked':
            action = QAction('Click to enter master password', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.prompt_master_password)
            menu.addAction(action)
        else:
            separated = False
            idx = -1
            for idx, container in enumerate(self.containers.values()):
                mountpoint = container.upon
                if not mountpoint and container.vital:
                    mountpoint = f'[{container.vital.upon}]'

                if idx > 0 and not separated and container.type == 'crypt':
                    menu.addSeparator()
                    separated = True

                name = container.name
                if container.back_file:
                    name = container.back_file
                    if name.startswith('/home/'):
                        name = '~' + name[6:]

                # Determine which emoji to show
                if mountpoint.startswith('/'):
                    emoji = '✅'
                    icon_key = 'ok' if icon_key != 'alert' else icon_key
                elif container.opened:
                    emoji = '‼️'
                    icon_key = 'alert' if do_alerts else icon_key
                else:
                    emoji = '🔳'

                # Construct menu line text
                if emoji == '‼️':
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
                menu.addAction(action)


            # Other fixed menu entries
            if idx > 0 and not separated:
                menu.addSeparator()

            action = QAction('Create New Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_create_file_click)
            menu.addAction(action)

            action = QAction('Add Existing Crypt File', self.app)
            action.setFont(self.mono_font)
            action.triggered.connect(self.handle_add_file_click)
            menu.addAction(action)

            menu.addSeparator()

            if self.history.status in ('clear_text', 'unlocked'):
                verb = 'Set' if self.history.status == 'clear_text' else 'Update/Clear'
                action = QAction(f'{verb} Master Password', self.app)
                action.setFont(self.mono_font)
                action.triggered.connect(self.prompt_master_password)
                menu.addAction(action)

        action = QAction("Exit", self.app)
        action.setFont(self.mono_font)
        action.triggered.connect(self.exit_app)
        menu.addAction(action)

        return self.replace_menu_if_different(menu, icon_key)


    def replace_menu_if_different(self, menu, icon_key):
        """ TBD """
        def replace_menu():
            was_visible = self.menu and self.menu.isVisible()

            self.menu = menu
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
            if isinstance(action, QWidgetAction):
                widget = action.defaultWidget()
                if isinstance(widget, QLabel):
                    return widget.text()
                return '<widget>'
            return action.text()

        if not self.menu: # or menu.actions() != self.menu.actions():
            return replace_menu()
        if self.prev_icon_key != icon_key:
            self.prev_icon_key = icon_key
            return replace_menu()

        if len(menu.actions()) != len(self.menu.actions()):
            return replace_menu()

        for new_action, old_action in zip(menu.actions(), self.menu.actions()):
            if get_action_text(new_action) != get_action_text(old_action):
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
        
    def update_history(self, uuid, values):
        """ TBD """
        vital = self.history.get_vital(uuid)
        mount_point = values['upon']
        if not hasattr(vital, 'when'):
            vital.when = 0
        if (values['password'] != vital.password or mount_point != vital.upon
                or time.time() - 24*3600 >= vital.when):
            vital.password = values['password']
            self.history.put_vital(vital)
            if mount_point:
                vital.upon = mount_point

class CommonDialog(QDialog):
    """ TBD """
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.main_layout = QVBoxLayout()
        self.button_layout = QHBoxLayout()
        self.password_toggle = None
        self.password_input = None
        self.items = []
        self.inputs = {}
        self.progress_label = None
        self.progress_bar = None

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
        if not isinstance(label_texts, list):
            text_str, label_texts = label_texts, [label_texts]
            while len(label_texts) < len(keys):
                placeholder_texts.append(text_str)
        if not isinstance(placeholder_texts, list):
            text_str, placeholder_texts = placeholder_texts, [placeholder_texts]
            while len(placeholder_texts) < len(keys):
                placeholder_texts.append(text_str)

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
                assert False, f'invalid field_type{field_type}'

            if add_on == 'password':
                val = tray.ini_tool.get_current_val('show_passwords_by_default')
                val = False if placeholder_text else val # don't show existig passwords
                if val:
                    input_field.setEchoMode(QLineEdit.EchoMode.Normal)
                else:
                    input_field.setEchoMode(QLineEdit.EchoMode.Password)
                self.password_input = input_field
                # self.password_toggle = QPushButton("👁️")
                self.password_toggle = QPushButton("●")
                self.password_toggle.setFixedWidth(30)
                self.password_toggle.setCheckable(True)
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

    @staticmethod
    def get_real_user_home_directory():
        """Returns the home directory of the real user when running under sudo."""
        real_user = os.environ.get('SUDO_USER')
        if real_user:
            return os.path.join("/home", real_user)  # Assumes standard home directory structure
        return os.path.expanduser("~")  # Fallback to current user's home directory

    def browse_folder(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        initial_dir = initial_dir if initial_dir else self.get_real_user_home_directory()

        # Open the folder dialog starting at the determined directory
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder", initial_dir)
        if folder_path:
            input_field.setText(folder_path)  # Update the input field with the selected folder path

    def browse_file(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        initial_dir = initial_dir if initial_dir else self.get_real_user_home_directory()

        # Open the folder dialog starting at the determined directory
        file_path = QFileDialog.getOpenFileName(self, "Select Existing File", initial_dir)
        # returns tuple (path, type of file)
        if file_path[0]:
            input_field.setText(file_path[0])  # Update the input field with the selected folder path

    def browse_new_file(self, input_field):
        """ Open a dialog to select a folder and update the input field with the selected path. """
        # Determine the initial directory to open
        initial_dir = input_field.text()
        initial_dir = initial_dir if initial_dir else self.get_real_user_home_directory()

        # Open the folder dialog starting at the determined directory
        file_path = QFileDialog.getSaveFileName(self, "Select New File", initial_dir)
        # returns tuple (path, type of file)
        if file_path[0]:
            input_field.setText(file_path[0])  # Update the input field with the selected folder path

    def toggle_password_visibility(self):
        """Toggle password visibility."""
        if not self.password_input or not self.password_toggle:
            return
        if self.password_toggle.isChecked():
            self.password_input.setEchoMode(QLineEdit.EchoMode.Password)  # Hide the password
            self.password_toggle.setText("👁️")
        else:
            self.password_input.setEchoMode(QLineEdit.EchoMode.Normal)  # Show the password
            self.password_toggle.setText("●")

    @staticmethod
    def get_mount_points():
        """ TBD """
        mount_points = set()
        with open('/proc/mounts', 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mount_points.add(parts[1])  # The mount point is the second field
        return mount_points



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
        # Force UI update before starting potentially slow operation
        QApplication.processEvents()

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
            Returns error (or None if no error)
        """
        # Empty mount point is valid (means auto-mount)
        if text:  # Only validate if not empty
            if DeviceInfo.is_banned(text):
                return f'ERR: cannot mount on "special" {text}'
            if text.startswith('/media/'):
                if is_device:
                    return 'ERR: cannot mount device in /media'
                else:
                    return 'ERR: use empty field for automatic /media mounting'
            if not os.path.isabs(text):
                return f'ERR: mount point ({text}) must be absolute path'
            parent_dir = os.path.dirname(text)
            parent_exists = os.path.isdir(parent_dir)
            if not parent_exists:
                return f'ERR: parent directory ({parent_dir}) does not exist'
            if os.path.exists(text):
                if not os.path.isdir(text):
                    return f'ERR: mount point ({text}) exists but is not a directory'
                if len(os.listdir(text)) > 0:
                    return f'ERR: mount point ({text}) exists but is not empty'
            if text in mount_points:
                return f'ERR: mount point ({text}) occupied'
        elif is_device:
                return 'ERR: empty field not allowed for devices'
        return None

    ####################################################
    # LUKS Primitives
    ####################################################
    def _unlock_luks(self, device_path, password, luks_device):
        """Common LUKS unlock logic"""
        if hasattr(self, 'opened') and self.opened:
            return None  # Already unlocked
        return run_cmd(['cryptsetup', 'luksOpen', device_path, luks_device],
                      input_str=password)

#   def _mount_with_udisks_full(self, device_path, password):
#       """Mount using udisks2 for auto-mounting with password (handles unlock + mount)"""
#       udisks_cmd = self.find_udisks_command()
#       if not udisks_cmd:
#           return "Error: No udisks command found. Install udisks2."

#       if udisks_cmd == 'udisksctl':
#           # Unlock first with --no-user-interaction to force stdin usage
#           err = run_cmd([udisks_cmd, 'unlock', '-b', device_path, '--no-user-interaction'],
#                        input_str=password)
#           if err:
#               return err
#           # Then mount (udisksctl will figure out the mapper path)
#           return run_cmd([udisks_cmd, 'mount', '-b', device_path])
#       else:
#           # For older udisks
#           return run_cmd([udisks_cmd, '--unlock', device_path],
#                         input_str=password)

    def _mount_with_udisks_mapper(self, mapper_path):
        """Mount using udisks2 for auto-mounting (assumes already unlocked)"""
        udisks_cmd = LuksTray.singleton.udisks_cmd
        if not udisks_cmd:
            return "Error: No udisks command found. Install udisks2."

        # Determine the real desktop user if running as root
        real_user = os.environ.get('SUDO_USER')
        if not real_user:
            return "Error: Cannot determine real user (SUDO_USER not set)."

        # Choose appropriate mount arguments
        mount_args = ['mount', '-b', mapper_path] if udisks_cmd == 'udisksctl' else ['--mount', mapper_path]

        return run_cmd(['sudo', '-u', real_user, udisks_cmd] + mount_args)


    def _mount_manual(self, tray, mapper_path, upon, do_bindfs=False):
        """Manual mounting with bindfs"""
        err = run_cmd(['mount', mapper_path, upon])
        if do_bindfs and not err:
            err = run_cmd(['bindfs', '-u', str(tray.uid), '-g', str(tray.gid),
                          upon, upon])
        return err

    def _setup_loop_device(self, container):
        """Set up loop device for file-based containers"""
        if hasattr(self, 'opened') and self.opened:
            return None, f'/dev/{container.name}'

        # Use --show to get the loop device name
        result = run_cmd(['losetup', '-f', '--show', container.back_file])
        if result.startswith('FAIL:'):
            return result, None

        loop_device = result.strip()
        # Update container.name to match the loop device (e.g., 'loop0')
        container.name = os.path.basename(loop_device)

        return None, loop_device

    ####################################################
    # LUKS Generic Mounter
    ####################################################
    def mount_luks_container(self, tray, container, password, upon=None, luks_device=None,
                            luks_file=None, size=None):
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
                    err = run_cmd(['truncate', '-s', f'{size}M', luks_file])
                    if not err:
                        err = run_cmd(['cryptsetup', 'luksFormat', '--force-password',
                               '-q', '--batch-mode', luks_file], input_str=password)
                    if err:
                        return err
                    needs_filesystem = True

                # For file containers, we need to set up a loop device for udisks2
                if not upon:  # Auto-mounting case
                    # Use losetup with --show to get the loop device name directly
                    sub = subprocess.run(['losetup', '-f', '--show', luks_file],
                                       capture_output=True, text=True, check=False)
                    if sub.returncode != 0:
                        return f'FAIL: losetup -f --show {luks_file}: {sub.stderr.strip()}'

                    device_path = sub.stdout.strip()  # This will be something like /dev/loop0
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

            # Choose mounting strategy
            if upon:
                # Manual mounting: unlock with cryptsetup, then mount manually
                err = self._unlock_luks(device_path, password, luks_device)
                if err:
                    return err

                mapper_path = f'/dev/mapper/{luks_device}'

                # Create filesystem if needed (for new files)
                if needs_filesystem:
                    err = run_cmd(['mkfs.ext4', mapper_path])
                    if err:
                        return err

                return self._mount_manual(tray, mapper_path, upon, do_bindfs=bool(luks_file))
            else:
                # Auto-mounting: use hybrid approach (cryptsetup unlock + udisks2 mount)
                err = self._unlock_luks(device_path, password, luks_device)
                if err:
                    return err

                mapper_path = f'/dev/mapper/{luks_device}'

                # Create filesystem if needed (for new files)
                if needs_filesystem:
                    err = run_cmd(['mkfs.ext4', mapper_path])
                    if err:
                        return err

                # Use udisks2 just for mounting (no unlock needed)
                return self._mount_with_udisks_mapper(mapper_path)

        except Exception as e:
            return f"An error occurred: {str(e)}"

    def kill_bindfs_on_mount(mount_point, timeout=1.0):
        """
        Kill any `bindfs` process that is holding onto the given mount point.
        
        Args:
            mount_point (str): The mount point to check.
            timeout (float): Time (in seconds) to wait after SIGTERM before SIGKILL.
        
        Returns:
            str or None: Error string if failure occurs, otherwise None.
        """
        try:
            # Run fuser -vm on the mount point
            result = subprocess.run(
                ['fuser', '-vm', mount_point],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                check=True
            )
            if result.returncode not in (0, 1):  # 0 = PIDs found, 1 = none, others = error
                return f"fuser failed: {result.stderr.strip()}"

            lines = result.stdout.strip().splitlines()
            if len(lines) < 2:
                return None  # No processes using the mount

            headers = lines[0].split()
            cmd_idx = headers.index('COMMAND')
            pid_idx = headers.index('PID')

            for line in lines[1:]:
                cols = line.split()
                if len(cols) <= cmd_idx:
                    continue
                cmd = cols[cmd_idx]
                pid = int(cols[pid_idx])
                if cmd == 'bindfs':
                    # Kill it gracefully first
                    subprocess.run(['kill', '-TERM', str(pid)], check=True)
                    time.sleep(timeout)
                    # Check if it's still running
                    try:
                        os.kill(pid, 0)
                        # Still alive — kill hard
                        subprocess.run(['kill', '-KILL', str(pid)], check=True)
                    except ProcessLookupError:
                        pass  # Already dead
                    return None  # Successfully killed
            return None  # No bindfs found

        except Exception as e:
            return f"Error killing bindfs: {e}"


class MasterPasswordDialog(CommonDialog):
    """ TBD """
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Master Password Dialog')
        self.add_input_field('password', "Master Password", '', 24, add_on='password')
        self.add_push_button('OK', self.set_master_password)
        self.add_push_button('Cancel', self.cancel)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def set_master_password(self, _):
        """ TBD """
        tray = LuksTray.singleton
        field = self.inputs.get('password', None)
        errs = []
        password = field.text().strip()
        if tray.history.status == 'locked':
            tray.history.master_password = password
            tray.history.restore()
            if tray.history.status != 'unlocked':
                tray.history.master_password = ''
                errs.append(f'failed to unlock {repr(tray.history.path)}')
        elif tray.history.status in ('unlocked', 'clear_text'):
            tray.history.master_password = password
            tray.history.dirty = True
            err = tray.history.save()
            if err:
                tray.history.master_password = ''
                errs.append(err)
            elif password:
                tray.history.status = 'unlocked'
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
            self.setWindowTitle('Unmount and Close Device')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container.opened:  # unmount dialog
            self.setWindowTitle('Close Unmounted Device')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            self.add_line(f'Close {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.setWindowTitle('Mount Device')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.name}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, add_on='password')
            self.add_input_field('upon', "Mount At", f'{vital.upon}', 36, add_on='folder')
            if container.label:
                self.add_line(f'Label: {container.label}')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
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

        errs.append(f'{container.name}')
        mount_points = self.get_mount_points()

        # Parse and validate inputs
        for key, field in self.inputs.items():
            text = field.text().strip()
            values[key] = text

            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')
            elif key == 'upon':
                err = self.check_upon(text, mount_points, is_device=True)
                if err:
                    errs.append(err)
            else:
                errs.append(f'ERR: unknown key({key})')

        # Determine LUKS device name
        luks_device = ''
        if len(container.filesystems) == 1:
            luks_device = container.filesystems[0].name

        # Proceed with mounting if no errors
        if len(errs) <= 1:
            mount_point = values['upon']
            if mount_point and not os.path.exists(mount_point):
                os.makedirs(mount_point, exist_ok=True)

            self.show_progress('Mount device...')
            err = self.mount_luks_container(tray, container, values['password'],
                            upon=mount_point, luks_device=luks_device)
            self.hide_progress()

            if err:
                errs.append(err)

        if len(errs) > 1:
            self.alert_errors(errs)
            return

        tray.update_history(uuid, values)

        tray.update_menu()
        self.accept()

    def unmount_device(self, uuid):
        """Attempt to unmount the partition."""

        errs, container = [], None
        tray = LuksTray.singleton
        container = tray.containers.get(uuid, None)
        errs.append(f'{container.name}')

        # Show progress - disable buttons and add progress indicator
        self.show_progress("Unmount/Close device...")

        tray.update_mounts()
        busy_warns = set()

        if container:
            for filesystem in container.filesystems:
                for mount in filesystem.mounts:
                    ### self.kill_bindfs_on_mount(mount)
                    if tray.is_mounted(mount):
                        err = run_unmount(mount, busy_warns)
                        if err:
                            errs.append(err)
#               mapped_device = f'/dev/mapper/{filesystem.name}'
#               ignores = []
#               if os.path.exists(mapped_device) and stat.S_ISBLK(os.stat(mapped_device).st_mode):
#                   if tray.is_mounted(mapped_device):
#                       run_cmd(["umount", mapped_device], ignores)

            if len(errs) <= 1:
                run_cmd(["cryptsetup", "luksClose", filesystem.name], errs)

        # Hide progress indicator
        self.hide_progress()

        if len(errs) > 1:
            if not busy_warns:
                self.alert_errors(errs)
            return # don't close dialog box

        tray.update_menu()
        self.accept()

#   def hide_partition(self, uuid):
#       """ Hide the partition """
#       # FIXME: need body
#       return

class MountFileDialog(CommonDialog):
    """ TBD """
    def __init__(self, container, create=False):
        super().__init__()
        tray = LuksTray.singleton

        # mounts if there are
        if container and container.opened:  # unmount dialog
            if container.mounts:
                self.setWindowTitle('Unmount/Close Crypt File')
                self.add_line(f'{container.back_file}')
                self.add_line(f'Unmount {container.upon}')
            else:
                self.setWindowTitle('Close Crypt File')
                self.add_line(f'{container.back_file}')
            self.add_push_button('OK', self.unmount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container:
            self.setWindowTitle('Mount Crypt File')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.back_file}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, add_on='password')
            self.add_input_field('upon', "Mount At", f'{vital.upon}', 36, add_on='folder')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
            if container.uuid:
                self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        elif not create: # no container ... use existing file (not creating)
            self.setWindowTitle('Add Existing Crypt File')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('back_file', "Crypt File", '', 48, add_on='file')
            self.add_input_field('upon', "Mount At", '', 36, add_on='folder')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            # self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        else: # no container ... create crypt file
            self.setWindowTitle('Create New Crypt File')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('size_str', "Size (MiB)", '32', 8)
            self.add_input_field('back_file', "Crypt File", '', 48, add_on='new_file')
            self.add_input_field('overwrite_ok', "Enable Overwrite of Existing File",
                                 '', 48, field_type='checkbox')
            self.add_input_field('upon', "Mount At", '', 36, add_on='folder')

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
        busy_warns = set()
        if container:
            if container.mounts:
                is_auto_mount = container.mounts[0].startswith('/media/')
                attempts = 1 if is_auto_mount else 2
                for _ in range(attempts):
                    # it may take two dismounts, one for the regular mount, and
                    # one for the bindfs mount
                    tray.update_mounts()
                    for mount in container.mounts:
                        if tray.is_mounted(mount):
                            err = run_unmount(mount, busy_warns)
                            if err:
                                errs.append(err)
                    if busy_warns:
                        break

            if not errs:
                run_cmd(["cryptsetup", "luksClose", container.name], errs=errs)
                    # If this is a file container with a loop device, detach it
            if not errs and container.back_file:
                ignores = []
                run_cmd(["losetup", "-d", f"/dev/{container.name}"], errs=ignores)

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
        assert tray

        if uuid is None:
            container = DeviceInfo.make_partition_namespace('', '')
            container.opened = False
        else:
            container = tray.containers.get(uuid, None)
            if not container:
                errs.append(f'ERR: container w UUID={uuid} not found')
                return
        errs.append(f'{container.name}')

        mount_points = self.get_mount_points()

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
            elif key == 'upon':
                err = self.check_upon(text, mount_points)
                if err:
                    errs.append(err)
                    
            elif key == 'size_str':
                try:
                    size_str = values.get('size_str', None)
                    megs = int(size_str)
                    if megs < 32:
                        errs.append(f'at least 32 expected ... invalid size ({megs})')
                except Exception:
                    errs.append(f'"int" expected ... invalid size ({size_str})')

            elif key == 'back_file':
                path = os.path.abspath(text)
                values[key] = path
                if not os.path.isdir(os.path.dirname(path)):
                    errs.append(f'ERR: Crypt File {path} must be in an existing directory')

            else:
                errs.append(f'ERR: unknown key({key})')

        if len(errs) <= 1:
            if container.back_file:
                back_file = container.back_file
            else:
                back_file = values['back_file']
                
            if 'overwrite_ok' in values:
                overwrite_ok = values['overwrite_ok']
                if os.path.exists(back_file) and not overwrite_ok:
                    errs.append('cannot overwrite {back_file!r} w/o checking allowed')

        if len(errs) <= 1:
            self.show_progress('Mount file...')
            mount_point = values['upon']
            if mount_point and not os.path.exists(mount_point):
                os.makedirs(mount_point, exist_ok=True)
            err = self.mount_luks_container(tray, container, values['password'],
                    mount_point, luks_file=back_file, size=values.get('size_str', None))
            self.hide_progress()

            if err:
                errs.append(err)
        if len(errs) > 1:
            self.alert_errors(errs)  # FIXME: need to actually do the mount if no errors
            # self.accept()
            return

        # update history with new values if mount worked
        tray.update_history(uuid, values)

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
        args = ['tail', '-n50', '-F', ini_tool.log_path]
        print(f'RUNNING: {args}')
        os.execvp('tail', args)
        sys.exit(1) # just in case ;-)

    try:
        if os.geteuid() != 0:
            # Re-run the script with sudo needed and opted
            rerun_module_as_root('luks_tray.main')

        ini_tool = IniTool(paths_only=False)
        Utils.prt_path = ini_tool.log_path

        tray = LuksTray(ini_tool, opts)
        sys.exit(tray.app.exec())

    except Exception as exce:
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)


# Basic PyQt6 Tray Icon example
def mainBasic():
    """ TBD """
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)

    # Check if the system supports tray icons
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("System tray not available")
        sys.exit(1)

    # Create a tray icon
    tray_icon = QSystemTrayIcon()
    tray_icon.setIcon(QIcon(
        "/home/joe/Projects/luks-tray/luks_tray/resources/orange-shield-v03.svg"))

    # Create a right-click menu for the tray icon
    tray_menu = QMenu()

    # Add an action to the menu
    quit_action = QAction("Quit")
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(quit_action)

    # Set the context menu to the tray icon
    tray_icon.setContextMenu(tray_menu)

    # Show the tray icon
    tray_icon.show()

    # Run the application's event loop
    sys.exit(app.exec_())

if __name__ == "__main__":
    # mainBasic()
    main()
