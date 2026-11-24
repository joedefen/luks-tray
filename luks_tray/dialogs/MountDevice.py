#!/usr/bin/env python3
"""Mount and unmount dialog for LUKS device containers."""
# pylint: disable=broad-exception-caught, invalid-name, too-many-locals
# pylint: disable=too-many-branches,too-many-statements

import os

from luks_tray.dialogs.Common import CommonDialog
from luks_tray.LuksOps import mount_luks_container, run_unmount, sudo_cmd


class MountDeviceDialog(CommonDialog):
    """One dialog for a LUKS device container, in whichever state it is."""

    def __init__(self, tray, container):
        """Build the dialog for `container`.

        Args:
            tray: The running tray instance.
            container: The device container record.
        """
        super().__init__(tray)

        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts
        if mounts:
            self.set_title('Unmount and Close Device')
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container.opened:
            self.set_title('Close Unmounted Device')
            self.add_line(f'{container.name}')
            mappers = [fs.name for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
            target = ', '.join(mappers) if mappers else container.name
            self.add_line(f'Close {target}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.set_title('Mount Device')
            vital = self.tray.history.get_vital(container.uuid)
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
            self.main_layout.addLayout(self.button_layout)

        self.setLayout(self.main_layout)

    def mount_device(self, uuid):
        """Validate the form, unlock and mount the container, then record it.

        Args:
            uuid: Container identity.
        """

        tray, container = self.tray, None
        errs, values = [], {}

        if tray:
            container = tray.containers.get(uuid, None)

        if not container:
            errs.append(f'ERR: container w UUID={uuid} not found')
            return

        tray.update_mounts()

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

        # The mapper name is the single child filesystem's name when there is
        # exactly one; otherwise derive a deterministic name.
        filesystems = [fs for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
        if len(filesystems) == 1:
            luks_device = filesystems[0].name
        else:
            luks_device = f'{container.name}-luks'

        if not errs:
            mount_point = values['upon']

            self.hide_password()
            self.show_progress('Mount device...')
            try:
                with tray.busy_operation('mount device'):
                    err = mount_luks_container(tray, container, values['password'],
                            upon=mount_point, readonly=values['readonly'],
                            luks_device=luks_device,
                            dot_vault_dir=self.dot_vault_dir)
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
        """Unmount every filesystem of the container, then close the mapper.

        Args:
            uuid: Container identity.
        """

        errs, container = [], None
        tray = self.tray
        container = tray.containers.get(uuid, None)

        self.show_progress("Unmount/Close device...")
        try:
            with tray.busy_operation('unmount device'):
                tray.update_mounts()
                busy_warns = set()

                if container:
                    unmounteds = []
                    filesystems = [fs for fs in container.filesystems
                                   if getattr(fs, 'name', '')]
                    for fs in filesystems:
                        for mount in fs.mounts:
                            if tray.is_mounted(mount):
                                err = run_unmount(mount, busy_warns)
                                if err:
                                    errs.append(err)
                                else:
                                    unmounteds.append(mount)

                    # `cryptsetup close` takes the mapper name from the child
                    # filesystem, falling back to the container name.
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
                            tray.remove_if_auto(mount)
                else:
                    errs.append(f'ERR: container w UUID={uuid} not found')
        finally:
            self.hide_progress()

        if errs:
            if not busy_warns:
                heading = [f'{container.name}'] if container else []
                self.alert_errors(heading + errs)
            return

        tray.update_menu()
        self.accept()
