#!/usr/bin/env python3
"""Options dialog for the `[ui]` settings of `config.ini`."""
# pylint: disable=broad-exception-caught, invalid-name

import os
import shlex

from luks_tray.dialogs.Common import CommonDialog


class OptionsDialog(CommonDialog):
    """Edit the four `[ui]` settings of `config.ini`."""

    def __init__(self, tray):
        """Build the options form from the current configuration.

        Args:
            tray: The running tray instance.
        """
        super().__init__(tray)
        self.set_title('Options')

        ini_tool = tray.ini_tool
        self.add_input_field(
            ['show_passwords_by_default', 'show_anomaly_alerts'],
            ['Show passwords by default', 'Show anomaly alerts'],
            '', field_type='checkbox')
        for key in ('show_passwords_by_default', 'show_anomaly_alerts'):
            self.inputs[key].setChecked(bool(ini_tool.get_current_val(key)))

        auto_root = ini_tool.get_current_val('auto_mount_folder')
        self.add_input_field('auto_mount_folder', 'Auto-mount folder', auto_root,
                             char_width=32, add_on='folder')

        log_viewer = ini_tool.get_current_val('log_viewer')
        self.add_input_field('log_viewer', 'Log viewer command', log_viewer,
                             char_width=32)

        self.add_push_button('Cancel', self.cancel)
        self.add_push_button('Save', self.save)
        self.main_layout.addLayout(self.button_layout)
        self.setLayout(self.main_layout)

    def read_settings(self):
        """Return the form as a settings mapping.

        Returns:
            dict: Values in the shape `set_values()` expects.
        """
        return {
            'auto_mount_folder': self.inputs['auto_mount_folder'].text().strip(),
            'log_viewer': self.inputs['log_viewer'].text().strip(),
            'show_passwords_by_default':
                self.inputs['show_passwords_by_default'].isChecked(),
            'show_anomaly_alerts': self.inputs['show_anomaly_alerts'].isChecked(),
        }

    def validate(self, values):
        """Return the error lines for `values`.

        Args:
            values: Settings mapping to check.

        Returns:
            list: Error lines; empty when the values may be saved.
        """
        errors = []
        configured = values['auto_mount_folder']
        stored = self.tray.ini_tool.get_current_val('auto_mount_folder')
        if configured != (stored.strip() if isinstance(stored, str) else stored):
            if not configured:
                return ['auto-mount folder may not be empty']
            expanded = self.tray.expand_real_user(configured)
            if not os.path.isabs(expanded):
                return [f'auto-mount folder {configured!r} must be an absolute '
                        f'path, or start with ~/']
            problem = self.tray.validate_auto_mount_root(expanded)
            if problem:
                errors.append(problem)
        viewer = values['log_viewer']
        try:
            shlex.split(viewer)
        except ValueError as exc:
            errors.append(f'log viewer command {viewer!r} cannot be parsed: {exc}')
        return errors

    def save(self, _=None):
        """Validate, persist, rebuild the menu and close.

        Args:
            _: Optional signal argument.
        """
        values = self.read_settings()
        errors = self.validate(values)
        if not errors:
            errors = self.tray.ini_tool.set_values(values)
        if errors:
            self.alert_errors(errors)
            return
        self.tray.update_menu()
        self.accept()
