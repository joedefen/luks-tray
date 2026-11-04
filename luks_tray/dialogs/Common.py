#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Shared dialog scaffolding for LUKS TRAY.

    Every dialog uses the same body heading and the same window-title suffix.
    That is deliberate: a Wayland compositor owns decorations and placement, so
    a dialog cannot rely on its title bar being drawn.  Sway hides decorations
    by default, and niri/X11 draw them, but the heading is part of the dialog
    layout everywhere -- the information never depends on the compositor.

    Compositor rules match the stable `app_id` set in
    [`luks_tray.AppInfo`](luks_tray/AppInfo.py:1), not the title, so one rule
    covers the dialogs, the error boxes and the file pickers.
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
from PyQt6.QtCore import QTimer, Qt

from luks_tray.AppInfo import heading_text, window_title
from luks_tray.Utils import note_problem, prt_once
from luks_tray.DeviceInfo import DeviceInfo


class CommonDialog(QDialog):
    """ Base class for every dialog: layout helpers, paths and progress UI.

        A dialog is built from small pieces (`add_line()`, `add_input_field()`,
        `add_push_button()`) and then runs one privileged operation, so this
        class holds the parts they all share: the invisible parent window from
        the tray, cursor-relative placement under X11 (see `showEvent()`; a
        Wayland compositor places the window itself), password visibility
        toggling, the error message box and the progress bar.

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
        # key -> (line edit, eye button), in the order they were added. A
        # dialog may hold more than one masked field (the options dialog asks
        # for a new password twice), and each eye has to drive its own field.
        self.password_fields = {}
        self.items = []
        self.inputs = {}
        self.progress_label = None
        self.progress_bar = None
        self.get_real_user_home_directory() # populate home/vault dir

    def set_title(self, title):
        """ Set the window title and add the same purpose as a body heading.

            The heading is added on every compositor, not only where the title
            bar is missing: Sway hides decorations by default, but even a
            compositor that draws them can change its mind, and a dialog that
            carries its own heading is never ambiguous.  Legacy callers that
            still pass the `[luks-tray]` suffix are accepted; the suffix only
            belongs in the window title.

            Inserted at position 0 because every dialog calls this before it
            adds any fields.
        """
        self.setWindowTitle(window_title(title))
        heading = QLabel(heading_text(title))
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet("""
            QLabel {
                font-weight: bold;
                font-size: 14px;
                padding: 4px;
                border-bottom: 1px solid #888;
            }
        """)
        self.main_layout.insertWidget(0, heading)

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
        """Hide every password field, and point its eye the right way."""
        for input_field, toggle in self.password_fields.values():
            toggle.setChecked(False)
            toggle.setText("👁️")
            self.set_password_visibility(input_field, False)

    def show_password(self):
        """Show every password field, and point its eye the right way."""
        for input_field, toggle in self.password_fields.values():
            toggle.setChecked(True)
            toggle.setText("●")
            self.set_password_visibility(input_field, True)

    @staticmethod
    def set_password_visibility(input_field, exposed):
        """ Set one field's echo mode, whatever its eye button says.

            The one place that knows which `QLineEdit` mode means "readable":
            the toggles, the helpers above and `add_input_field()` all end up
            here, so a field can never be shown while its button says hidden.
        """
        input_field.setEchoMode(
            QLineEdit.EchoMode.Normal if exposed else QLineEdit.EchoMode.Password)


    def add_line(self, text, wrap=False):
        """ Append a read-only text line to the main layout.

            `wrap` is for the paragraphs that explain a consequence: a label
            without word wrap makes the whole dialog as wide as its longest
            sentence, which on a small screen is a dialog wider than the screen.
        """
        label = QLabel(text)
        if wrap:
            label.setWordWrap(True)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """ Add a button to the button row that calls `method(arg)`.

            `arg` keeps the callbacks one-argument (`self.mount_device(uuid)`,
            `self.cancel(_)`), which is also the signature Qt's `clicked`
            hands over when a button is connected directly. The button is
            returned so a dialog whose buttons describe a state (the master
            password dialog enables "Clear" and "Change" as the two fields
            change) can keep hold of it instead of searching the layout.
        """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)
        return button

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
                exposed = bool(exposed) and not placeholder_text # don't show existing passwords
                self.set_password_visibility(input_field, exposed)
                toggle = QPushButton("●")
                toggle.setFixedWidth(30)
                toggle.setCheckable(True)
                toggle.setChecked(exposed)
                toggle.setText("●" if exposed else "👁️")
                toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                # The field and its button are bound here, not looked up later:
                # the callback used to read `self.password_*`, which is only
                # ever the *last* masked field of the dialog.
                toggle.clicked.connect(
                    partial(self.toggle_password_visibility, input_field, toggle))
                field_layout.addWidget(toggle)
                self.password_fields[key] = (input_field, toggle)
                # The plain attributes stay pointed at the first field, which
                # is what `hide_password()`/`show_password()` and the dialogs
                # that hold a single password mean; `password_fields` is the
                # answer for everything else.
                if self.password_input is None:
                    self.password_input = input_field
                    self.password_toggle = toggle

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

    def toggle_password_visibility(self, input_field=None, toggle=None):
        """Toggle the visibility of one password field.

            `add_input_field()` binds each eye button to the field next to it,
            so a dialog can ask for a new password twice without the two boxes
            fighting over one switch. Called with no arguments -- as the tests
            and any older caller do -- it drives the dialog's first password
            field.
        """
        input_field = input_field if input_field is not None else self.password_input
        toggle = toggle if toggle is not None else self.password_toggle
        if not input_field or not toggle:
            return
        exposed = toggle.isChecked() # password to be exposed
        self.set_password_visibility(input_field, exposed)
        toggle.setText("●" if exposed else "👁️")

    def clear_passwords(self):
        """ Empty every password field of the dialog.

            Called on the way out: a widget that keeps a typed password alive
            after the dialog is gone is a copy nobody is watching.
        """
        for input_field, _toggle in self.password_fields.values():
            input_field.clear()

    def cancel(self, _=None):
        """ null function"""
        self.reject()

    def alert_errors(self, error_lines):
        """Callback to show errors if present.

            Every failure the dialogs have to announce comes through here -- a
            mount that did not happen, a password that did not unlock, a write
            that was refused -- so it is also where the session's failure tally
            is kept, which is what the tray menu's "Open Log [n errors]" marker
            shows. Counting the message box, not just prt()'s own error lines,
            is what makes the marker agree with what the user was told.
        """
        if error_lines:  # Check if there are any errors
            for line in error_lines:
                note_problem(line)
            error_text = '\n'.join(error_lines)  # Join the list of error lines into one string

            error_dialog = QMessageBox(self)
            error_dialog.setIcon(QMessageBox.Icon.Critical)  # Set the icon to show it's an error
            error_dialog.setWindowTitle(window_title('Errors Detected'))
            error_dialog.setText("The following errors were encountered:")
            error_dialog.setInformativeText(error_text)
            error_dialog.setStandardButtons(QMessageBox.StandardButton.Ok)  # Add a dismiss button
            error_dialog.exec()  # Show the message box

                    # Ensure the parent dialog regains focus
            self.raise_()
            self.activateWindow()

    #: How long a self-closing notice stays up. Long enough to read a sentence,
    #: short enough that it never becomes the thing the user is fighting.
    NOTICE_TIMEOUT_MS = 3000

    def show_timed_message(self, text, success=True, informative=''):
        """ Say what happened in a box that closes itself, and carry on.

            For answers rather than questions: "the master password was
            changed" / "that password did not open the file". A message with a
            single Ok button asks for a click that decides nothing, so it goes
            away on its own after `NOTICE_TIMEOUT_MS`.

            A failure still reaches `note_problem()`, exactly as
            `alert_errors()` reports one, because the user was told about it and
            the menu's "Open Log [n errors]" marker counts what the user was
            told, not what was written to the file.

            The box runs its own event loop, so the timer fires inside it and
            there is nothing to poll; the timeout is a class attribute so a test
            can set it to 0 instead of sleeping.
        """
        if not success:
            note_problem(text)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information if success
                    else QMessageBox.Icon.Warning)
        box.setWindowTitle(window_title('Done' if success else 'Not Done'))
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        QTimer.singleShot(self.NOTICE_TIMEOUT_MS, box.accept)
        box.exec()
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
        """Hide progress indicator and re-enable buttons.

            `progress_label` exists from `__init__` (as None), so `hasattr()`
            was always true and the `finally:` calls in the dialogs' unmount
            paths -- which run whether or not a progress bar was ever shown --
            hit `AttributeError: 'NoneType' object has no attribute 'hide'`.
            Test what is there, not what attribute exists.
        """
        if self.progress_label and self.progress_bar:
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
