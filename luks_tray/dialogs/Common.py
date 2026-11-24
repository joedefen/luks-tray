#!/usr/bin/env python3
"""Shared dialog scaffolding for LUKS TRAY."""
# pylint: disable=unused-import,broad-exception-caught, invalid-name
# pylint: disable=no-name-in-module,import-outside-toplevel,too-many-instance-attributes
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-many-arguments,too-many-nested-blocks
# pylint: disable=line-too-long,too-many-public-methods
import os
from functools import partial

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from luks_tray.AppInfo import heading_text, window_title
from luks_tray.DeviceInfo import DeviceInfo
from luks_tray.Utils import note_problem, prt_once


class CommonDialog(QDialog):
    """Base class for every dialog: layout helpers, paths and progress UI.

    Attributes:
        home_dir: Home directory of the real user.
        dot_vault_dir: Default directory for new crypt files.
        vault_dir: Default mount area.
    """
    home_dir = None
    dot_vault_dir = None
    vault_dir = None

    def __init__(self, tray):
        """Build the dialog scaffolding.

        Args:
            tray: The running tray instance.
        """
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
        self.password_fields = {}
        self.items = []
        self.inputs = {}
        self.progress_label = None
        self.progress_bar = None
        self.get_real_user_home_directory()

    def set_title(self, title):
        """Set the window title and add the same purpose as a body heading.

        Args:
            title: Window title, with or without the app suffix.
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
        """Place the dialog near the cursor before Qt shows it.

        Args:
            event: Qt show event.
        """
        self.adjustSize()

        cursor_pos = QCursor.pos()
        dialog_width = self.width()
        dialog_height = self.height()

        screen = self.screen() or QApplication.primaryScreen()
        screen_geometry = screen.geometry()

        x = cursor_pos.x() - (dialog_width // 2)
        y = cursor_pos.y() + 20

        if x < screen_geometry.left():
            x = screen_geometry.left()
        elif (x + dialog_width) > screen_geometry.right():
            x = screen_geometry.right() - dialog_width

        if y < screen_geometry.top():
            y = screen_geometry.top()
        elif (y + dialog_height) > screen_geometry.bottom():
            y = screen_geometry.bottom() - dialog_height

        self.move(x, y)

        super().showEvent(event)

    def hide_password(self):
        """Hide every password field and point its eye the right way."""
        for input_field, toggle in self.password_fields.values():
            toggle.setChecked(False)
            toggle.setText("👁️")
            self.set_password_visibility(input_field, False)

    def show_password(self):
        """Show every password field and point its eye the right way."""
        for input_field, toggle in self.password_fields.values():
            toggle.setChecked(True)
            toggle.setText("●")
            self.set_password_visibility(input_field, True)

    @staticmethod
    def set_password_visibility(input_field, exposed):
        """Set one field's echo mode.

        Args:
            input_field: Line edit to update.
            exposed: True to show the text.
        """
        input_field.setEchoMode(
            QLineEdit.EchoMode.Normal if exposed else QLineEdit.EchoMode.Password)

    def add_line(self, text, wrap=False):
        """Append a read-only text line to the main layout.

        Args:
            text: Text to show.
            wrap: Enable word wrap for long paragraphs.
        """
        label = QLabel(text)
        if wrap:
            label.setWordWrap(True)
        self.main_layout.addWidget(label)

    def add_push_button(self, label, method, arg=None):
        """Add a button to the button row that calls `method(arg)`.

        Args:
            label: Button text.
            method: Callable invoked on click.
            arg: Argument passed to `method`.

        Returns:
            QPushButton: The new button.
        """
        button = QPushButton(label)
        button.clicked.connect(lambda: method(arg))
        self.button_layout.addWidget(button)
        return button

    def add_input_field(self, keys, label_texts, placeholder_texts, char_width=5,
                       field_type='text', add_on=''):
        """Add one or more labeled input fields to the dialog.

        Args:
            keys: Input key, or a list of keys.
            label_texts: Label text, or a list matching `keys`.
            placeholder_texts: Initial text, or a list matching `keys`.
            char_width: Minimum field width in characters.
            field_type: `'text'` or `'checkbox'`.
            add_on: Optional `'password'`, `'folder'`, `'file'` or
                `'new_file'` decoration.
        """
        tray = self.tray
        field_layout = QHBoxLayout()

        if not isinstance(keys, list):
            keys = [keys]

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

            label = QLabel(label_text)
            if field_type == 'checkbox':
                input_field = QCheckBox()
                input_field.setText(label_text)
                input_field.setChecked(False)
                field_layout.addWidget(input_field)

            elif field_type == 'text':
                input_field = QLineEdit()
                input_field.setText(placeholder_text.strip())
                char_width = max(len(placeholder_text), char_width)
                input_field.setFixedWidth(char_width * 10)
                field_layout.addWidget(label)
                field_layout.addWidget(input_field)
            else:
                raise ValueError(f'invalid field_type {field_type!r}')

            if add_on == 'password':
                exposed = tray.ini_tool.get_current_val('show_passwords_by_default')
                exposed = bool(exposed) and not placeholder_text
                self.set_password_visibility(input_field, exposed)
                toggle = QPushButton("●")
                toggle.setFixedWidth(30)
                toggle.setCheckable(True)
                toggle.setChecked(exposed)
                toggle.setText("●" if exposed else "👁️")
                toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                toggle.clicked.connect(
                    partial(self.toggle_password_visibility, input_field, toggle))
                field_layout.addWidget(toggle)
                self.password_fields[key] = (input_field, toggle)
                if self.password_input is None:
                    self.password_input = input_field
                    self.password_toggle = toggle

            if add_on == 'folder':
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(partial(self.browse_folder, input_field))
                field_layout.addWidget(button)

            if add_on == 'file':
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(partial(self.browse_file, input_field))
                field_layout.addWidget(button)

            if add_on == 'new_file':
                button = QPushButton("Browse...", self)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(partial(self.browse_new_file, input_field))
                field_layout.addWidget(button)

            self.inputs[key] = input_field
        self.main_layout.addLayout(field_layout)

    def get_real_user_home_directory(self):
        """Return the home directory of the real user, even under sudo.

        Returns:
            str: The home directory.
        """
        if not self.home_dir:
            real_user = os.environ.get('SUDO_USER')
            if real_user:
                self.home_dir = os.path.join("/home", real_user)
            else:
                self.home_dir = os.path.expanduser("~")
            self.vault_dir = os.path.join(self.home_dir, 'Vaults')
            self.dot_vault_dir = os.path.join(self.home_dir, '.Vaults')
        return self.home_dir

    def browse_folder(self, input_field):
        """Select a folder and put it in `input_field`.

        Args:
            input_field: Line edit to update.
        """
        initial_dir = input_field.text()
        initial_dir = initial_dir if initial_dir else self.get_real_user_home_directory()

        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder", initial_dir)
        if folder_path:
            input_field.setText(folder_path)

    def browse_file(self, input_field):
        """Select an existing file and put it in `input_field`.

        Args:
            input_field: Line edit to update.
        """
        initial_dir = input_field.text()
        if not initial_dir:
            if os.path.exists(self.dot_vault_dir):
                initial_dir = self.dot_vault_dir
            else:
                initial_dir = self.home_dir

        file_path = QFileDialog.getOpenFileName(self, "Select Existing File", initial_dir)
        if file_path[0]:
            input_field.setText(file_path[0])

    def browse_new_file(self, input_field):
        """Select a path for a new file and put it in `input_field`.

        Args:
            input_field: Line edit to update.
        """
        initial_dir = input_field.text()
        if not initial_dir:
            if os.path.exists(self.dot_vault_dir):
                initial_dir = self.dot_vault_dir
            else:
                initial_dir = self.home_dir

        file_path = QFileDialog.getSaveFileName(self, "Select New File", initial_dir)
        if file_path[0]:
            input_field.setText(file_path[0])

    def toggle_password_visibility(self, input_field=None, toggle=None):
        """Toggle the visibility of one password field.

        Args:
            input_field: Field to update; defaults to the first password field.
            toggle: Eye button; defaults to the first password toggle.
        """
        input_field = input_field if input_field is not None else self.password_input
        toggle = toggle if toggle is not None else self.password_toggle
        if not input_field or not toggle:
            return
        exposed = toggle.isChecked()
        self.set_password_visibility(input_field, exposed)
        toggle.setText("●" if exposed else "👁️")

    def clear_passwords(self):
        """Empty every password field of the dialog."""
        for input_field, _toggle in self.password_fields.values():
            input_field.clear()

    def cancel(self, _=None):
        """Reject the dialog."""
        self.reject()

    def alert_errors(self, error_lines):
        """Show errors in a message box and count them as session failures.

        Args:
            error_lines: Error text lines to show.
        """
        if error_lines:
            for line in error_lines:
                note_problem(line)
            error_text = '\n'.join(error_lines)

            error_dialog = QMessageBox(self)
            error_dialog.setIcon(QMessageBox.Icon.Critical)
            error_dialog.setWindowTitle(window_title('Errors Detected'))
            error_dialog.setText("The following errors were encountered:")
            error_dialog.setInformativeText(error_text)
            error_dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
            error_dialog.exec()

            self.raise_()
            self.activateWindow()

    # How long a self-closing notice stays up.
    NOTICE_TIMEOUT_MS = 3000

    def show_timed_message(self, text, success=True, informative=''):
        """Show a notice that closes itself.

        Args:
            text: Main text.
            success: True for an information box, false for a warning.
            informative: Optional secondary text.
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
        self.raise_()
        self.activateWindow()

    def show_progress(self, message):
        """Show the progress indicator and disable the buttons.

        Args:
            message: Progress text.
        """
        for button in self.findChildren(QPushButton):
            button.setEnabled(False)

        if not self.progress_label:
            self.progress_label = QLabel()
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(10)
            self.main_layout.addWidget(self.progress_label)
            self.main_layout.addWidget(self.progress_bar)

        self.progress_label.setText(message)
        self.progress_label.show()
        self.progress_bar.show()
        # Paint without processing events, which would re-enter update_menu().
        if self.layout() is not None:
            self.layout().activate()
        self.repaint()

    def hide_progress(self):
        """Hide the progress indicator and re-enable the buttons."""
        if self.progress_label and self.progress_bar:
            self.progress_label.hide()
            self.progress_bar.hide()

        for button in self.findChildren(QPushButton):
            button.setEnabled(True)

    def check_upon(self, text, mount_points, is_device=False):
        """Validate a candidate mount point.

        Args:
            text: Candidate mount point.
            mount_points: Currently mounted mount points.
            is_device: True when mounting a device rather than a file.

        Returns:
            str | None: The error text, or None when the mount point is usable.
        """
        if not text:
            return 'ERR: empty field not allowed for devices or files'
        if not os.path.isabs(text):
            return f'ERR: mount point ({text}) must be absolute path'
        try:
            text = os.path.abspath(text)
        except (TypeError, ValueError, OSError) as exc:
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
