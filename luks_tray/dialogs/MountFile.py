#!/usr/bin/env python3
"""Mount, unmount, create and add dialog for LUKS file containers."""
# pylint: disable=broad-exception-caught, invalid-name, too-many-locals
# pylint: disable=too-many-branches,too-many-statements

import os

from PyQt6.QtWidgets import QCheckBox

from luks_tray.ContainerType import TCRYPT
from luks_tray.DeviceInfo import DeviceInfo
from luks_tray.dialogs.Common import CommonDialog
from luks_tray.LuksOps import get_container_identity, mount_luks_container, run_unmount, sudo_cmd
from luks_tray.Utils import prt

# Known container suffixes, removed from the derived mount point.
CONTAINER_SUFFIXES = ('.luks', '.luks2', '.crypt', '.img')


def derive_mount_point(vault_dir, back_file):
    """Return the mount point offered for the container in `back_file`.

    Args:
        vault_dir: Default mount area.
        back_file: Container file path.

    Returns:
        str: The candidate mount point.
    """
    basename = os.path.basename(back_file)
    for suffix in CONTAINER_SUFFIXES:
        if basename.endswith(suffix) and len(basename) > len(suffix):
            basename = basename[:-len(suffix)]
            break
    return os.path.join(vault_dir, basename)


class MountFileDialog(CommonDialog):
    """One dialog for a LUKS file container, in any of its four states."""

    def __init__(self, tray, container, create=False):
        """Build the dialog for `container`.

        Args:
            tray: The running tray instance.
            container: File container record, or None for a new file.
            create: True to create a new crypt file.
        """
        super().__init__(tray)

        if container and container.opened:
            if container.mounts:
                self.set_title('Unmount/Close Crypt File')
                self.add_line(f'{container.back_file}')
                self.add_line(f'Unmount {container.upon}')
            else:
                self.set_title('Close Crypt File')
                self.add_line(f'{container.back_file}')
            self.add_push_button('OK', self.unmount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container:
            self.set_title('Mount Crypt File')
            vital = self.tray.history.get_vital(container.uuid)
            self.add_line(f'{container.back_file}')
            self.add_input_field('password', "Enter Password", f'{vital.password}',
                                24, add_on='password')
            where = vital.upon if vital.upon else self.vault_dir
            self.add_input_field('upon', "Mount At", where, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')
            if container.size_str:
                self.add_line(f'Size: {container.size_str}')
            if vital.meta_type == TCRYPT:
                self.add_line('Type: TrueCrypt/VeraCrypt')
            elif container.uuid:
                self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif not create:
            self.set_title('Add Existing Crypt File')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('back_file', "Crypt File", '', 48, add_on='file')
            self.add_input_field('upon', "Mount At", self.vault_dir, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.set_title('Create New Crypt File')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('size_str', "Size (MiB)", '32', 8)
            self.add_input_field('back_file', "Crypt File", self.dot_vault_dir, 48, add_on='new_file')
            self.add_input_field('overwrite_ok', "Enable Overwrite of Existing File",
                                 '', 48, field_type='checkbox')
            self.add_input_field('upon', "Mount At", self.vault_dir, 36, add_on='folder')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        self.setLayout(self.main_layout)

    def unmount_file(self, uuid):
        """Unmount the file container, close the mapper and detach the loop.

        Args:
            uuid: Container identity.
        """
        errs, container = [], None
        tray = self.tray
        container = tray.containers.get(uuid, None)
        self.show_progress('Unmount/Close crypt file...')
        try:
            with tray.busy_operation('unmount file'):
                busy_warns = set()
                if container:
                    if container.mounts:
                        # A crypt file can be mounted twice when bindfs adds
                        # an overlay, so retry once.
                        for _ in range(2):
                            tray.update_mounts()
                            for mount in container.mounts:
                                if tray.is_mounted(mount):
                                    err = run_unmount(mount, busy_warns)
                                    if err:
                                        errs.append(err)
                                    else:
                                        tray.remove_if_auto(mount)

                            if busy_warns:
                                break

                    if not errs:
                        sudo_cmd(["cryptsetup", "close", container.name], errs=errs)
                    if not errs and container.back_file:
                        ignores = []
                        sudo_cmd(["losetup", "-d", f"/dev/{container.name}"],
                                 errs=ignores)
        finally:
            self.hide_progress()

        tray.update_menu()
        if errs:
            if not busy_warns:
                self.alert_errors(errs)
        else:
            self.accept()

    def mount_file(self, uuid):
        """Validate the form, then create, add or mount the crypt file.

        Args:
            uuid: Container identity, or None for the add/create layouts.
        """

        tray, container = self.tray, None
        errs, values = [], {}
        if tray is None:
            self.alert_errors(['ERR: tray is not initialized'])
            return

        if uuid is None:
            container = DeviceInfo.make_partition_namespace('', '')
            container.opened = False
        else:
            container = tray.containers.get(uuid, None)
            if not container:
                errs.append(f'ERR: container w UUID={uuid} not found')
                return
        context = (container.back_file
                   if (not container.name and container.back_file)
                   else container.name)

        tray.update_mounts()
        mount_point = None

        for key, field in self.inputs.items():
            if isinstance(field, QCheckBox):
                values[key] = field.isChecked()
                continue

            text = field.text().strip()
            values[key] = text
            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')

            elif key == 'back_file':
                if not text:
                    errs.append('ERR: cannot leave the crypt file empty')
                    continue
                path = os.path.expanduser(text)
                if not os.path.isabs(path):
                    errs.append(f'ERR: Crypt File ({text}) must be an absolute path')
                    continue
                path = os.path.abspath(path)
                values[key] = path
                dirname = os.path.dirname(path)
                if not os.path.isdir(os.path.dirname(dirname)):
                    errs.append(f'ERR: Crypt File {path} must be in an existing directory')

            elif key == 'upon':
                path = os.path.expanduser(text)
                if not os.path.isabs(path):
                    errs.append(f'ERR: mount point ({text}) must be absolute path')
                    continue
                path = os.path.abspath(path)
                if 'back_file' in values or container.back_file:
                    back_file = container.back_file if container.back_file else values['back_file']
                    if path == self.vault_dir:
                        path = derive_mount_point(self.vault_dir, back_file)

                values[key] = path
                mount_point = path
                err = self.check_upon(path, tray.upons)
                if err:
                    errs.append(err)

            elif key == 'readonly':
                pass
            elif key == 'size_str':
                try:
                    size_str = values.get('size_str', None)
                    megs = int(size_str)
                    if megs < 32:
                        errs.append(f'at least 32 expected ... invalid size ({megs})')
                except (TypeError, ValueError) as exc:
                    prt(f'note: bad size_str {size_str!r}: {exc}')
                    errs.append(f'"int" expected ... invalid size ({size_str})')

            else:
                errs.append(f'ERR: unknown key({key})')

        if not errs:
            if container.back_file:
                back_file = container.back_file
            else:
                back_file = values['back_file']

            if 'overwrite_ok' in values:
                overwrite_ok = values['overwrite_ok']
                if os.path.exists(back_file) and not overwrite_ok:
                    errs.append(f'cannot overwrite {back_file!r} w/o checking allowed')

        if not errs:
            self.show_progress('Mount file...')
            try:
                with tray.busy_operation('mount file'):
                    err = mount_luks_container(tray, container,
                            values['password'], mount_point,
                            readonly=values.get('readonly', False),
                            luks_file=back_file,
                            size=values.get('size_str', None),
                            dot_vault_dir=self.dot_vault_dir)
            finally:
                self.hide_progress()

            if err:
                errs.append(err)
        if errs:
            self.alert_errors([f'{context}'] + errs)
            return

        if not uuid:
            uuid = container.uuid or get_container_identity(back_file)
        tray.update_history(uuid, values, back_file=back_file,
                            meta_type=getattr(container, 'meta_type', ''))

        tray.update_menu()
        self.accept()
