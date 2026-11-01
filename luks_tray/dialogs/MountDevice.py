#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Mount/unmount dialog for LUKS *device* containers (partitions). """
# pylint: disable=broad-exception-caught, invalid-name, too-many-locals
# pylint: disable=too-many-branches,too-many-statements

import os

from luks_tray.dialogs.Common import CommonDialog
from luks_tray.LuksOps import mount_luks_container, run_unmount, sudo_cmd


class MountDeviceDialog(CommonDialog):
    """ One dialog for a LUKS *device* container, in whichever state it is.

        The layout follows the container, not the caller: mounted filesystems
        give the unmount/close prompt, an opened-but-unmounted container gives
        the close prompt (the anomaly case that used to render "Close ?"), and
        anything else gives the mount form. `tray.containers[uuid]` stays the
        source of truth during the operation, so the dialog itself keeps no
        copy of the container state.
    """
    def __init__(self, tray, container):
        super().__init__(tray)

        mounts = []
        if container.filesystems:
            mounts = container.filesystems[0].mounts
        if mounts:  # unmount dialog
            self.set_title('Unmount and Close Device [luks-tray]')
            self.add_line(f'{container.name}')
            self.add_line(f'Unmount {",".join(mounts)}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container.opened:  # close dialog: unlocked but nothing mounted
            self.set_title('Close Unmounted Device [luks-tray]')
            self.add_line(f'{container.name}')
            # `mounts` is empty in this branch by definition (that is what
            # selected it), so joining it rendered a bare "Close ?". Name the
            # mapper device when it is known, else the container itself.
            mappers = [fs.name for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
            target = ', '.join(mappers) if mappers else container.name
            self.add_line(f'Close {target}?')
            self.add_push_button('OK', self.unmount_device, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else:
            self.set_title('Mount Device [luks-tray]')
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
        """ Validate the form, unlock+mount the container, then record it.

            Nothing is executed unless every field validates (`errs` stays
            empty), and the whole privileged part runs inside
            `tray.busy_operation()` so the poller cannot rebuild the menu
            between `cryptsetup open` and `mount`. On success the mount point
            and password go into the history and the menu is refreshed.
        """

        tray, container = self.tray, None
        errs, values = [], {}

        if tray:
            container = tray.containers.get(uuid, None)

        if not container:
            errs.append(f'ERR: container w UUID={uuid} not found')
            return

        # `errs` holds *errors only*. The container name used to be pushed in
        # as a placeholder so callers could test `len(errs) <= 1`; that made
        # the first element magic and the emptiness test off by one. The
        # context line is now added only where the dialog is built.
        tray.update_mounts()

        # Parse and validate inputs
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

        # Determine the device-mapper name for `cryptsetup open`. Normally the
        # LUKS header has exactly one child filesystem whose name *is* the
        # mapper name. Any other case (no filesystem parsed yet, or several)
        # used to yield '' and end up as an empty argument to `cryptsetup
        # open`, so derive a deterministic name from the container instead.
        filesystems = [fs for fs in (container.filesystems or [])
                       if getattr(fs, 'name', '')]
        if len(filesystems) == 1:
            luks_device = filesystems[0].name
        else:
            luks_device = f'{container.name}-luks'

        # Proceed with mounting if no errors
        if not errs:
            # The mount point is created by mount_luks_container(), as the user:
            # the startup cleanup removes empty auto-mount folders with a plain
            # rmdir, and a root-owned directory (what `sudo mkdir -p` left
            # behind) can never be removed again by the unprivileged tray.
            mount_point = values['upon']

            self.hide_password()
            self.show_progress('Mount device...')
            try:
                # Keep the poller out until cryptsetup open *and* mount have
                # both finished; a refresh in between rebuilds the menu from
                # a half-mounted container.
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
        """ Unmount every filesystem of the container, then close the mapper.

            `cryptsetup close` is skipped when an unmount failed, so nothing is
            closed while files are still open. The unmounted mount points are
            pruned through `tray.remove_if_auto()`, which only touches empty
            directories below the validated auto-mount root.
        """

        errs, container = [], None
        tray = self.tray
        container = tray.containers.get(uuid, None)

        # Show progress - disable buttons and add progress indicator
        self.show_progress("Unmount/Close device...")
        try:
            # No menu refresh between the umount and the cryptsetup close.
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

                    # cryptsetup close takes the *mapper* name, which lives in
                    # the child filesystem entries. An opened container may
                    # legitimately have none -- the '‼' open-but-unmounted
                    # anomaly -- so fall back to the container's own name (file
                    # containers are their own mapper) and, failing that,
                    # report a usable error.
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
            # Hide progress indicator
            self.hide_progress()

        if errs:
            if not busy_warns:
                # Name the container first, as the old placeholder element did.
                heading = [f'{container.name}'] if container else []
                self.alert_errors(heading + errs)
            return # don't close dialog box

        tray.update_menu()
        self.accept()
