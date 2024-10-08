#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" TBD """
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
import os
import sys
import json
import signal
import subprocess
import traceback
from types import SimpleNamespace
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
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

        # Load JSON data
        self.load_data()
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
        self.containers = self.lsblk.parse_lsblk()
        self.merge_containers_history()
        # self.menu.clear()
        self.update_menu_items()
        # self.tray_icon.setContextMenu(self.menu)

    def load_data(self):
        """Load data from JSON file (if exists)."""
        self.data = {}
        try:
            with open('luks_data.json', 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}

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

        # Adding dummy partition states for demonstration
        for container in self.containers.values():
            mountpoint = None
            if container.opened:
                if len(container.filesystems) >= 1:
                    mounts = container.filesystems[0].mounts
                    if mounts:
                        mountpoint = mounts[0]

            # Set the title based on the state
            title = '■ ' if mountpoint else '□ '
            title += '🗹' if container.opened else '—'
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

        # Add options to exit
        exit_action = QAction("Exit", self.app)
        exit_action.setFont(mono_font)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(exit_action)
        
        if self.is_menu_different(menu):
            self.menu = menu
            self.tray_icon.setContextMenu(self.menu)
            self.tray_icon.show()
            
    def is_menu_different(self, menu):
        """ TBD """
        if not self.menu: # or menu.actions() != self.menu.actions():
            return True
        for new_action, old_action in zip(menu.actions(), self.menu.actions()):
            if new_action.text() != old_action.text():
                return True
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

class MountDialog(QDialog):
    """ TBD """
    def __init__(self, container):
        super().__init__()
        tray = LuksTray.singleton

        self.main_layout = QVBoxLayout()
        self.button_layout = QHBoxLayout()
        self.items = []
        self.inputs = {}
        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts
        # mounts if there are
        if mounts:  # unmount dialog
            self.setWindowTitle('Unmount and Close Container')
            # self.setFixedSize(300, 200)
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {mounts}?')
            self.add_push_button('OK', self.unmount_partition, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.setWindowTitle('Mount Container')
            vital = tray.history.get_vital(container.uuid)
            self.add_line(f'{container.name}')
            self.add_input_field('password', "Enter Password", f'{vital.password}', 24)
            self.add_input_field('upon', "Mount At", f'{vital.upon}', 36)
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

    def add_line(self, text):
        """ TBD """
        label = QLabel(text)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """ TBD """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)

    def add_input_field(self, key, label_text, placeholder_text, char_width=5):
        """ Adds a label and a line edit input to the main layout. """
        field_layout = QHBoxLayout() # Create a horizontal layout for the label and input field
        label = QLabel(label_text) # Create a QLabel for the label text

        input_field = QLineEdit()
        input_field.setText(placeholder_text)
        char_width = max(len(placeholder_text), char_width)
        # Set the width of the input field based on character width
        # Approximation: assuming an average of 8 pixels per character for a monospace font
         # You can adjust this factor based on the font
        input_field.setFixedWidth(char_width * 10)
        field_layout.addWidget(label)
        field_layout.addWidget(input_field)
        self.inputs[key] = input_field
        self.main_layout.addLayout(field_layout) # Add horizontal layout to main vertical layout

    def cancel(self, _=None):
        """ null function"""
        self.reject()

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


    def mount_partition(self, uuid):
        """Attempt to mount the partition."""
        def mount_it(container, password, upon, luks_device):
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
                cmd = ['mount', f'/dev/mapper/{luks_device}', upon]
                prc = subprocess.run(cmd, check=False,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                if prc.returncode != 0:
                    return (f'ERR: {cmd} failed: {prc.stderr.decode()}'
                            + f' rc={prc.returncode}')

                # Return success message if everything went well
                return ''

            except Exception as e:
                return f"An error occurred: {str(e)}"

        tray, container = LuksTray.singleton, None
        if tray:
            container = tray.containers.get(uuid, None)
        if not container:
            return
        vital = tray.history.get_vital(uuid)
        errs, values = [], {}
        errs.append(f'{container.name}')

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
