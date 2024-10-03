import os
import sys
import json
import signal
import subprocess
import traceback
from types import SimpleNamespace
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox, QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt5.QtGui import QIcon, QCursor, QFont
from PyQt5.QtCore import QTimer, Qt

import luks_tray.Utils as Utils
from luks_tray.Utils import prt, PyKill
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
    def _make_partition_namespace(name, size_bytes):
        return SimpleNamespace(name=name,       # /proc/partitions
            opened=None,    # or True or False
            label='',       # blkid
            fstype='',      # fstype OR /sys/class/block/{name}/device/model
            size_bytes=size_bytes,  # /sys/block/{name}/...
            uuid='',
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
            entry.size_bytes=int(device.get('size', 0))
            entry.uuid = device.get('uuid', '')
            mounts = device.get('mountpoints', [])
            while len(mounts) >= 1 and mounts[0] is None:
                del mounts[0]
            entry.mounts = mounts

            return entry

               # Run the `lsblk` command and get its output in JSON format with additional columns
        result = subprocess.run(['lsblk', '-J', '--bytes', '-o',
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
                for grandchild in child.get('children'):
                    subentry = eat_one(grandchild)
                    subentry.parent = entry.name
                    entry.filesystems.append(subentry)
                    # entries[subentry.name] = subentry

        self.entries = entries
        if self.DB:
            print('\n\nDB: --->>> after parse_lsblk:')
            for entry in entries.values():
                print(vars(entry))

        return entries

    def get_relative(self, name):
        return self.entries.get(name, None)

class LuksTray():
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
        self.containers = self.lsblk.parse_lsblk()

        self.tray_icon = QSystemTrayIcon(self.icons[0], self.app)
        self.tray_icon.setToolTip('luks-tray')
        self.tray_icon.setVisible(True)

        # Context Menu
        self.menu = QMenu()
        self.update_menu()
        
        self.tray_icon.setContextMenu(self.menu)

    def load_data(self):
        """Load data from JSON file (if exists)."""
        self.data = {}
        try:
            with open('luks_data.json', 'r') as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        
    def show_partition_details(self, name):
        container = self.containers.get(name, None)
        if container is None:
            return
        details = f'DETAILS for {container.name}:\n'
        details += 'UUID={container.UUID}\n'
        QMessageBox.information(None, "Partition Details", details)
        
    def update_menu(self):
        """Update context menu with LUKS partitions."""
        self.menu.clear()
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
            title = '⯅' if mountpoint else '❢ '
            title += '🗹' if container.opened else '—'
            title += f' {container.name} {mountpoint}'

            # Create the action for the partition
            action = QAction(title, self.app)
            action.setFont(mono_font)
            
            # Connect the left-click action
            action.triggered.connect(lambda checked, x=container.uuid: self.handle_partition_click(x))

            # Use a custom event filter to detect right-click for showing details
            # action.customContextMenuRequested.connect(lambda: self.show_partition_details(container.name))

            # Add action to the menu
            self.menu.addAction(action)

        self.menu.addSeparator()

        # Add options to exit
        exit_action = QAction("Exit", self.app)
        exit_action.setFont(mono_font)
        exit_action.triggered.connect(self.exit_app)
        self.menu.addAction(exit_action)

    def show_partition_details(self, name):
        container = self.containers.get(name, None)
        if container is None:
            return
        details = f'DETAILS for {container.name}:\n'
        details += f'UUID={container.uuid}\n'
        QMessageBox.information(None, "Partition Details", details)


    def handle_partition_click(self, uuid):
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
        if uuid in self.containers:
            dialog = UnmountDialog(self.containers[uuid])
            dialog.exec_()

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

class UnmountDialog(QDialog):
    def __init__(self, container):
        super().__init__()
        self.setWindowTitle("Unmount Partition")
        self.setFixedSize(300, 200)

        layout = QVBoxLayout()
        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts

        self.label = QLabel(f"Unmount {mounts}?")
        layout.addWidget(self.label)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Enter Password")
        layout.addWidget(self.password_input)

        self.delay_input = QLineEdit()
        self.delay_input.setPlaceholderText("Auto-Unmount Delay (min)")
        layout.addWidget(self.delay_input)

        self.repeat_input = QLineEdit()
        self.repeat_input.setPlaceholderText("Auto-Unmount Repeat (min)")
        layout.addWidget(self.repeat_input)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.unmount_partition)
        layout.addWidget(ok_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button)

        self.setLayout(layout)

    def unmount_partition(self):
        """Attempt to unmount the partition."""
        # Here you would implement the unmount logic.
        password = self.password_input.text()
        delay = self.delay_input.text()
        repeat = self.repeat_input.text()
        
        # Placeholder for actual unmount logic
        QMessageBox.information(self, "Unmount", f"Unmounting {self.label.text()} with password: {password}, delay: {delay}, repeat: {repeat}.")
        
        self.accept()

def rerun_module_as_root(module_name):
    """ rerun using the module name """
    if os.geteuid() != 0: # Re-run the script with sudo
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        vp = ['sudo', sys.executable, '-m', module_name] + sys.argv[1:]
        os.execvp('sudo', vp)

def main():
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
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)

    # Check if the system supports tray icons
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("System tray not available")
        sys.exit(1)
    
    # Create a tray icon
    tray_icon = QSystemTrayIcon()
    tray_icon.setIcon(QIcon("/home/joe/Projects/luks-tray/src/luks_tray/resources/orange-shield-v03.svg"))

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

