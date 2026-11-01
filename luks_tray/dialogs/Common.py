#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Shared dialog scaffolding for LUKS TRAY.

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
# pylint: disable=line-too-long,too-many-public-methods
import os
from functools import partial

from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QProgressBar, QPushButton, QVBoxLayout)
from PyQt6.QtGui import QCursor
from PyQt6.QtCore import Qt

from luks_tray.Utils import prt_once
from luks_tray.DeviceInfo import DeviceInfo


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


class CommonDialog(QDialog):
    """ Base class for every dialog: layout helpers, paths and progress UI.

        A dialog is built from small pieces (`add_line()`, `add_input_field()`,
        `add_push_button()`) and then runs one privileged operation, so this
        class holds the parts they all share: the invisible parent window from
        the tray, positioning next to the cursor (see `showEvent()`, needed
        because Sway ignores the CSD hints), password visibility toggling, the
        error message box and the progress bar.

        The tray is a constructor argument rather than a singleton lookup: it
        keeps this package independent of `luks_tray.Main` (no circular import)
        and lets tests pass a plain stand-in object.
    """
    home_dir = None
    dot_vault_dir = None # ~/.Vaults (default crypts)
    vault_dir = None # ~/Vaults (default mount area)

    def __init__(self, tray):
        super().__init__(parent=tray.dialog_parent)
        self.tray = tray
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
        """ Append a read-only text line to the main layout. """
        label = QLabel(text)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """ Add a button to the button row that calls `method(arg)`.

            `arg` keeps the callbacks one-argument (`self.mount_device(uuid)`,
            `self.cancel(_)`), which is also the signature Qt's `clicked`
            hands over when a button is connected directly.
        """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)

    def add_input_field(self, keys, label_texts, placeholder_texts, char_width=5,
                       field_type='text', add_on=''):
        """ Adds a label and a line edit input to the main layout. """
        tray = self.tray
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

    def check_upon(self, text, mount_points, is_device=False):
        """ Validate candidate mount point.
            `mount_points` must be the set of *currently mounted mount points*
            (i.e. `LuksTray.upons`), **not** `update_mounts()`, which
            returns the set of device keys.
            Returns error (or None if no error)
        """
        if not text:
            return 'ERR: empty field not allowed for devices or files'
        if not os.path.isabs(text):
            return f'ERR: mount point ({text}) must be absolute path'
        try:
            text = os.path.abspath(text) # normalize
        except (TypeError, ValueError, OSError) as exc:
            # Keep the un-normalized text and let the checks below report on
            # it; swallowing the reason made a rejected name unexplainable.
            prt_once(f'abspath {text!r}', f'WARN: cannot normalize {text!r}: {exc}')

        if DeviceInfo.is_banned(text):
            return f'ERR: cannot mount on "special" {text}'
        auto_root = self.tray.get_auto_mount_root()

        if text == auto_root:
            return 'ERR: cannot mount on auto_mount_folder itself'
        if text.startswith('/media/'):
            if is_device:
                return 'ERR: cannot mount device in /media'
            return 'ERR: use empty field for automatic /media mounting'
        parent_dir = os.path.dirname(text)
        auto_root = self.tray.get_auto_mount_root()
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
