import os
import sys
import json
import signal
from types import SimpleNamespace
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox, QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton
from PyQt5.QtGui import QIcon, QCursor
from PyQt5.QtCore import QTimer

import luks_tray.Utils as Utils
from luks_tray.Utils import prt, PyKill
from luks_tray.IniTool import IniTool


class LUKSTrayApp():
    svg_info = SimpleNamespace(version='03', subdir='resources/SetD'
                , bases= [
                        'white-shield',  # no LUKS partitions 
                        'green-shield',   # all partitions locked
                        'orange-shield',  # some partitions locked
                        'yellow-shield',  # no partitions locked (all locked)
                        ] )

    def __init__(self, ini_tool):
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

    def update_menu(self):
        """Update context menu with LUKS partitions."""
        self.menu.clear()
        
        # Adding dummy partition states for demonstration
        for uuid, details in self.data.items():
            action = QAction(f"{details['device_name']} - {details['mount_point']} - {details['state']}", self)
            action.triggered.connect(lambda checked, uuid=uuid: self.handle_partition_click(uuid))
            self.menu.addAction(action)
        
#       self.menu.addSeparator()

        # Add options to exit
        exit_action = QAction("Exit", self.app)
        exit_action.triggered.connect(self.exit_app)
        self.menu.addAction(exit_action)

    def handle_partition_click(self, uuid):
        """Handle clicking a partition."""
        # Show a dialog to unmount or display info
        dialog = UnmountDialog(uuid, self.data[uuid])
        dialog.exec_()

    def exit_app(self):
        """Exit the application."""
        self.tray_icon.hide()
        sys.exit()

class UnmountDialog(QDialog):
    def __init__(self, uuid, details):
        super().__init__()
        self.setWindowTitle("Unmount Partition")
        self.setFixedSize(300, 200)

        layout = QVBoxLayout()

        self.label = QLabel(f"Unmount {details['mount_point']}?")
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

def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
 
    ini_tool = IniTool(paths_only=False)
    Utils.prt_path = ini_tool.log_path
 
    tray = LUKSTrayApp(ini_tool)
    sys.exit(tray.app.exec_())

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

