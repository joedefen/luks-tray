#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments
import os
import sys
import stat
import json
import signal
import subprocess
import traceback
from types import SimpleNamespace
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt5.QtWidgets import QFileDialog
from PyQt5.QtGui import QIcon, QCursor, QFont
from PyQt5.QtCore import QTimer, Qt

from luks_tray.History import HistoryClass
from luks_tray.Utils import prt
from luks_tray import Utils
from luks_tray.IniTool import IniTool

class DeviceInfo:
    """ Class to dig out the info we want from the system."""
    def __init__(self, opts):
        self.opts = opts
        self.DB = opts.debug
        self.wids = None
        self.head_str = None
        self.partitions = None
        self.entries = {}

    @staticmethod
    def _make_partition_namespace(name, size_str):
        return SimpleNamespace(name=name,       # /proc/partitions
            opened=None,    # or True or False
            label='',       # blkid
            fstype='',      # fstype OR /sys/class/block/{name}/device/model
            size_str=size_str,  # /sys/block/{name}/...
            uuid='',
            upon='',        # primary mount point
            mounts=[],      # /proc/mounts
            parent=None,    # a partition
            filesystems=[],        # child file systems
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

    def parse_lsblk(self):
        """ Parse ls_blk for all the goodies we need """
        def eat_one(device):
            entry = self._make_partition_namespace('', '')
            entry.name=device.get('name', '')
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
            return entry

               # Run the `lsblk` command and get its output in JSON format with additional columns
        result = subprocess.run(['lsblk', '-J', '-o',
                    'NAME,MAJ:MIN,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,UUID,MOUNTPOINTS', ],
                    stdout=subprocess.PIPE, text=True, check=False)
        parsed_data = json.loads(result.stdout)
        entries = {}

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
                elif 'luks' not in entry.fstype.lower():
                    continue
                # if parent.name not in entries:
                    # entries[parent.name] = parent
                entries[entry.uuid] = entry
                grandchildren = child.get('children', None)
                if not isinstance(grandchildren, list):
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

        self.entries = entries
        if self.DB:
            print('\n\nDB: --->>> after parse_lsblk:')
            for entry in entries.values():
                print(vars(entry))

        return entries

    def get_relative(self, name):
        """ TBD """
        return self.entries.get(name, None)

class LuksTray():
    """ TBD """
    singleton = None
    svg_info = SimpleNamespace(version='03', subdir='resources/SetD'
                , bases= [
                        'white-shield',  # no LUKS partitions
                        'green-shield',   # all partitions locked
                        'orange-shield',  # some partitions locked
                        'yellow-shield',  # no partitions locked (all locked)
                        ] )

    def __init__(self, ini_tool, opts):
        LuksTray.singleton = self
        self.uid = os.environ.get('SUDO_UID', os.getuid())
        self.gid = os.environ.get('SUDO_GID', os.getgid())

        self.ini_tool = ini_tool
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.history = HistoryClass(ini_tool.history_path)
        self.history.restore()

        self.icons, self.svgs = [], []
        for base in self.svg_info.bases:
            self.svgs.append(f'{base}-v{self.svg_info.version}.svg')
        for resource in self.svgs:
            if not os.path.isfile(resource):
                Utils.copy_to_folder(resource, ini_tool.folder)
            if not os.path.isfile(resource):
                prt(f'WARN: cannot find {repr(resource)}')
                continue
            self.icons.append(QIcon(os.path.join(self.ini_tool.folder, resource)))

        # ??? Load JSON data
        # ??? self.load_data()
        self.lsblk = DeviceInfo(opts=opts)

        self.tray_icon = QSystemTrayIcon(self.icons[0], self.app)
        self.tray_icon.setToolTip('luks-tray')
        self.tray_icon.setVisible(True)

        self.containers, self.menu = [], None
        self.update_menu()

        self.timer = QTimer(self.tray_icon)
        self.timer.timeout.connect(self.update_menu)
        self.timer.start(3000)  # 3000 milliseconds = 3 seconds

    def update_menu(self):
        """ TBD """
        if self.history.status in ('unlocked', 'clear_text'):
            self.containers = self.lsblk.parse_lsblk()
            self.merge_containers_history()
        self.update_menu_items()

    def merge_containers_history(self):
        """ TBD """
        for container in self.containers.values():
            self.history.ensure_container(container.uuid, container.upon)
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
        mono_font = QFont("Consolas", 10)

        if self.history.status == 'locked':
            action = QAction('Click to enter master password', self.app)
            action.triggered.connect(self.prompt_master_password)
            menu.addAction(action)
        else:
            for container in self.containers.values():
                mountpoint = ''
                if container.opened:
                    if len(container.filesystems) >= 1:
                        mounts = container.filesystems[0].mounts
                        if mounts:
                            mountpoint = mounts[0]
                else:
                    vital = self.history.get_vital(container.uuid)
                    if vital.upon:
                        mountpoint = f'[{vital.upon}]'

                # Set the title based on the state
                title = ('🡅 ' if mountpoint.startswith('/') else
                            '□ ' if container.opened else '⛛ ')
                title += f' {container.name} {mountpoint}'

                # Create the action for the partition
                action = QAction(title, self.app)
                action.setFont(mono_font)

                # Connect the left-click action
                action.triggered.connect(lambda checked,
                                     x=container.uuid: self.handle_partition_click(x))

                # Use a custom event filter to detect right-click for showing details
                # action.customContextMenuRequested.connect(
                #               lambda: self.show_partition_details(container.name))

                # Add action to the menu
                menu.addAction(action)

            menu.addSeparator()
            if self.history.status in ('clear_text', 'unlocked'):
                verb = 'Set' if self.history.status == 'clear_text' else 'Update/Clear'
                exit_action = QAction(f'{verb} Master Password', self.app)
                exit_action.setFont(mono_font)
                exit_action.triggered.connect(self.prompt_master_password)
                menu.addAction(exit_action)

        # Add options to exit
        exit_action = QAction("Exit", self.app)
        exit_action.setFont(mono_font)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)

        return self.replace_menu_if_different(menu)

    def replace_menu_if_different(self, menu):
        """ TBD """
        def replace_menu():
            self.menu = menu
            self.tray_icon.setContextMenu(self.menu)
            self.tray_icon.show()
            return True

        if not self.menu: # or menu.actions() != self.menu.actions():
            return replace_menu()
        if len(menu.actions()) != len(self.menu.actions()):
            return replace_menu()
        for new_action, old_action in zip(menu.actions(), self.menu.actions()):
            if new_action.text() != old_action.text():
                return replace_menu()
        return False


    def handle_partition_click(self, uuid):
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
        if uuid in self.containers:
            dialog = MountDialog(self.containers[uuid])
            dialog.exec_()

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

    def prompt_master_password(self):
        """ Prompt for master passdword"""
        dialog = MasterPasswordDialog()
        dialog.exec_()

class CommonDialog(QDialog):
    """ TBD """
    def __init__(self):
        super().__init__()
        self.main_layout = QVBoxLayout()
        self.button_layout = QHBoxLayout()
        self.password_toggle = None
        self.password_input = None
        self.items = []
        self.inputs = {}

    def add_line(self, text):
        """ TBD """
        label = QLabel(text)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """ TBD """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)

    def add_input_field(self, key, label_text, placeholder_text, char_width=5, is_password=False, is_folder=False):
        """ Adds a label and a line edit input to the main layout. """
        field_layout = QHBoxLayout() # Create a horizontal layout for the label and input field
        label = QLabel(label_text) # Create a QLabel for the label text

        input_field = QLineEdit()
        input_field.setText(placeholder_text.strip())

        char_width = max(len(placeholder_text), char_width)
        # Set the width of the input field based on character width
        # Approximation: assuming an average of 8 pixels per character for a monospace font
         # You can adjust this factor based on the font
        input_field.setFixedWidth(char_width * 10)
        field_layout.addWidget(label)
        field_layout.addWidget(input_field)
        if is_password:
            input_field.setEchoMode(QLineEdit.Password)  # Set the initial mode to hide the password
            self.password_input = input_field
            self.password_toggle = QPushButton("👁️")
            self.password_toggle.setFixedWidth(30)
            self.password_toggle.setCheckable(True)
            self.password_toggle.setFocusPolicy(Qt.NoFocus)
            self.password_toggle.clicked.connect(self.toggle_password_visibility)
            field_layout.addWidget(self.password_toggle)
            
        if is_folder: # Create a Browse button
            browse_button = QPushButton("Browse...", self)
            browse_button.setFocusPolicy(Qt.NoFocus)  # Prevent the button from gaining focus
            browse_button.clicked.connect(lambda: self.browse_folder(input_field))
            field_layout.addWidget(browse_button)

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



    def toggle_password_visibility(self):
        """Toggle password visibility."""
        if not self.password_input or not self.password_toggle:
            return
        if self.password_toggle.isChecked():
            self.password_input.setEchoMode(QLineEdit.Normal)  # Show the password
            self.password_toggle.setText("●")
        else:
            self.password_input.setEchoMode(QLineEdit.Password)  # Hide the password
            self.password_toggle.setText("👁️")


    def cancel(self, _=None):
        """ null function"""
        self.reject()

    def alert_errors(self, error_lines):
        """Callback to show errors if present."""
        if error_lines:  # Check if there are any errors
            error_text = '\n'.join(error_lines)  # Join the list of error lines into one string

            error_dialog = QMessageBox(self)
            error_dialog.setIcon(QMessageBox.Critical)  # Set the icon to show it's an error
            error_dialog.setWindowTitle("Errors Detected")
            error_dialog.setText("The following errors were encountered:")
            error_dialog.setInformativeText(error_text)
            error_dialog.setStandardButtons(QMessageBox.Ok)  # Add a dismiss button
            error_dialog.exec_()  # Show the message box

class MasterPasswordDialog(CommonDialog):
    """ TBD """
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Master Password Dialog')
        self.add_input_field('password', "Master Password", '', 24, is_password=True)
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
        self.accept()

class MountDialog(CommonDialog):
    """ TBD """
    def __init__(self, container):
        super().__init__()
        tray = LuksTray.singleton

        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts
        # mounts if there are
        if mounts:  # unmount dialog
            self.setWindowTitle('Unmount and Close Container')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_partition, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.setWindowTitle('Mount Container')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.name}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, is_password=True)
            self.add_input_field('upon', "Mount At", f'{vital.upon}', 36, is_folder=True)
            self.add_input_field('delay', "Auto-Unmount Delay (min)", f'{vital.delay_min}', 5)
            self.add_input_field('repeat', "Auto-Unmount Repeat (min)", f'{vital.repeat_min}', 5)
#           if container.fstype:
#               self.add_line(f'Filesystem: {container.fstype}')
            if container.label:
                self.add_line(f'Label: {container.label}')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
            self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_partition, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.add_push_button('Hide', self.hide_partition, container.uuid)
            self.main_layout.addLayout(self.button_layout)

        self.setLayout(self.main_layout)

    def unmount_partition(self, uuid):
        """Attempt to unmount the partition."""
        # Here you would implement the unmount logic.
        errs, container = [], None
        tray = LuksTray.singleton
        if tray:
            container = tray.containers.get(uuid, None)
        if container:
            for filesystem in container.filesystems:
                err_cnt = 0
                for mount in filesystem.mounts:
                    sub = subprocess.run( ["umount", mount],
                        capture_output=True, text=True, check=False)
                    if sub.returncode != 0:
                        err_cnt += 1
                        errs.append(f'umount {mount}: {sub.stdout} {sub.stderr} [rc={sub.returncode}]')
                if err_cnt > 0:
                    continue
                mapped_device = f'/dev/mapper/{filesystem.name}'
                if os.path.exists(mapped_device) and stat.S_ISBLK(os.stat(mapped_device).st_mode):

                    sub = subprocess.run(["umount", mapped_device],
                        capture_output=True, text=True, check=False)
                    if sub.returncode != 0:
                        err_cnt += 1
                        errs.append(f'umount {mapped_device}: '
                                    + f'{sub.stdout} {sub.stderr} [rc={sub.returncode}]')

                    sub = subprocess.run(["cryptsetup", "luksClose", filesystem.name],
                        capture_output=True, text=True, check=False)
                    if sub.returncode != 0:
                        err_cnt += 1
                        errs.append(f'cryptsetup luksClose {filesystem.name}: '
                                    + f'{sub.stdout} {sub.stderr} [rc={sub.returncode}]')
        if errs:
            self.alert_errors(errs)
        tray.update_menu()
        self.accept()

    def mount_partition(self, uuid):
        """Attempt to mount the partition."""
        def get_mount_points():
            mount_points = set()
            with open('/proc/mounts', 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mount_points.add(parts[1])  # The mount point is the second field
            return mount_points

        def mount_it(container, password, upon, luks_device):
            nonlocal tray
            try:
                # 1. Unlock the LUKS partition if needed
                if not container.opened:
                    if not luks_device:
                        luks_device = f'{uuid}-luks'
                    dev_path = f'/dev/{container.name}'
                    cmd = ['cryptsetup', 'luksOpen', dev_path, luks_device]
                    prc = subprocess.run(cmd, check=False,
                        input=password.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if prc.returncode != 0:
                        return (f'ERR: unlock {dev_path!r} failed: {prc.stderr.decode()}'
                             + f' rc={prc.returncode}')

                # 2. Mount the unlocked LUKS partition
                # cmd = ['mount', '-o', f'uid={tray.uid},gid={tray.gid}', f'/dev/mapper/{luks_device}', upon]
                cmd = ['mount', f'/dev/mapper/{luks_device}', upon]
                prc = subprocess.run(cmd, check=False,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if prc.returncode != 0:
                    return (f'ERR: {cmd} failed: {prc.stderr.decode()}'
                            + f' rc={prc.returncode}')

                # 3. Run binfs to make mount point available
                cmd = ['bindfs', '-u', str(tray.uid), '-g', str(tray.gid), upon, upon]
                prc = subprocess.run(cmd, check=False,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if prc.returncode != 0:
                    return (f'ERR: {cmd} failed: {prc.stderr.decode()}'
                            + f' rc={prc.returncode}')

                # Return success message if everything went well
                return ''

            except Exception as e:
                return f"An error occurred: {str(e)}"

        def mount_with_udisksctl(luks_device, mount_point):
            """ TBD """
            try:
                # Unlock the LUKS partition
                cmd = ['udisksctl', 'unlock', '-b', luks_device]
                prc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # Get the unlocked device path (usually /dev/mapper/your_device)
                # Here we assume the unlocked device name is based on the original LUKS device
                unlocked_device = f'/dev/mapper/{luks_device.split("/")[-1]}'

                # Mount the unlocked partition at the specified mount point
                cmd = ['udisksctl', 'mount', '-b', unlocked_device, '--mount-options', f'dir={mount_point}']
                prc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                return f'Mount successful at {mount_point}'

            except subprocess.CalledProcessError as e:
                return f'Error during mount: {e.stderr.decode()}'


    #       # Example usage
    #       luks_device = '/dev/sdXn'
    #       mount_point = '/test-upon5'
    #       result = mount_with_udisksctl(luks_device, mount_point)
    #       print(result)

        tray, container = LuksTray.singleton, None
        if tray:
            container = tray.containers.get(uuid, None)
        if not container:
            return
        vital = tray.history.get_vital(uuid)
        errs, values = [], {}
        errs.append(f'{container.name}')

        mount_points = get_mount_points()

        for key, field in self.inputs.items():
            text = field.text().strip()
            values[key] = text
            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')
            elif key == 'upon':
                isabs = os.path.isabs(text)
                isdir = os.path.isdir(text)
                length = 0
                if isabs and isdir:
                    length = len(os.listdir(text))
                if not isabs or not isdir or length > 0:
                    errs.append(f'ERR: mount point ({text}) is not absolute path to empty folder')
                elif text in mount_points:
                    errs.append(f'ERR: mount point ({text}) occupied')

            elif key in ('delay', 'repeat'):
                try:
                    values[key] = max(int(text), 0)
                except Exception:
                    errs.append(f'ERR: value (text) for {key} must be an integer')
            else:
                errs.append(f'ERR: unknown key({key})')

        luks_device = ''
        if len(container.filesystems) == 1:
            luks_device = container.filesystems[0].name

        if len(errs) <= 1:
            err = mount_it(container, values['password'], values['upon'], luks_device)
            if err:
                errs.append(err)
        if len(errs) > 1:
            self.alert_errors(errs)  # FIXME: need to actually do the mount if no errors
            self.accept()
            return

        # update history with new values if mount works
        vital = tray.history.get_vital(container.uuid)
        if (values['password'] != vital.password or values['upon'] != vital.upon
                or values['delay'] != vital.delay_min or values['repeat'] != vital.repeat_min):
            vital.password, vital.upon = values['password'], values['upon']
            vital.delay_min, vital.repeat_min = values['delay'], values['repeat']
            tray.history.put_vital(vital)

        tray.update_menu()
        self.accept()

    def hide_partition(self, uuid):
        """ Hide the partition """
        # FIXME: need body
        return

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
            help='override debug_mode from .ini initially')
    parser.add_argument('-o', '--stdout', action='store_true',
            help='log to stdout (if a tty)')
    parser.add_argument('-f', '--follow-log', action='store_true',
            help='exec tail -n50 -F on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('-q', '--quick', action='store_true',
            help='quick mode (1m lock + 1m sleep')
    opts = parser.parse_args()

    if opts.edit_config:
        ini_tool = IniTool(paths_only=True)
        editor = os.getenv('EDITOR', 'vim')
        args = [editor, ini_tool.ini_path]
        print(f'RUNNING: {args}')
        os.execvp(editor, args)
        sys.exit(1) # just in case ;-)

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
        sys.exit(tray.app.exec_())

    except Exception as exce:
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)


# Basic PyQt5 Tray Icon example
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
        "/home/joe/Projects/luks-tray/src/luks_tray/resources/orange-shield-v03.svg"))

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
