#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Mount/unmount (and create/add) dialog for LUKS *file* containers. """
# pylint: disable=broad-exception-caught, invalid-name, too-many-locals
# pylint: disable=too-many-branches,too-many-statements

import os

from PyQt6.QtWidgets import QCheckBox

from luks_tray.Utils import prt
from luks_tray.device_info import DeviceInfo
from luks_tray.dialogs.common import CommonDialog
from luks_tray.luks_ops import (generate_uuid_for_file_path,
                                get_luks_uuid_for_file,
                                mount_luks_container, run_cmd, run_unmount,
                                sudo_cmd)


class MountFileDialog(CommonDialog):
    """ One dialog for a LUKS *file* container, in any of its four states.

        `container` is the parsed file container, or None for a file the tray
        does not know yet; `create=True` then means "make a new crypt file"
        instead of "add an existing one". The four layouts differ only in
        their fields, and `mount_file()` handles all of them:

        - mounted/opened container -> unmount and detach (`unmount_file()`);
        - known container -> mount form (password, mount point, read-only);
        - no container, `create=False` -> add form (plus the crypt file path);
        - no container, `create=True` -> create form (size, overwrite check).

        A file container is its own loop device and mapper, which is why the
        unmount path closes it by name and detaches the loop afterwards.
    """
    def __init__(self, tray, container, create=False):
        super().__init__(tray)

        if container and container.opened:  # unmount dialog
            if container.mounts:
                self.set_title('Unmount/Close Crypt File [luks-tray]')
                self.add_line(f'{container.back_file}')
                self.add_line(f'Unmount {container.upon}')
            else:
                self.set_title('Close Crypt File [luks-tray]')
                self.add_line(f'{container.back_file}')
            self.add_push_button('OK', self.unmount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif container:
            self.set_title('Mount Crypt File [luks-tray]')
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
            if container.uuid:
                self.add_line(f'UUID: {container.uuid}')

            self.add_push_button('OK', self.mount_file, container.uuid)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        elif not create: # no container ... use existing file (not creating)
            self.set_title('Add Existing Crypt File [luks-tray]')
            self.add_input_field('password', "Enter Password", '',
                                24, add_on='password')
            self.add_input_field('back_file', "Crypt File", '', 48, add_on='file')
            # Prefilled with the vault area: `mount_file()` turns that back
            # into `<vault_dir>/<file name without .luks>` when it validates.
            self.add_input_field('upon', "Mount At", self.vault_dir, 36, add_on='folder')
            self.add_input_field('readonly', "Read-only",
                                 '', 48, field_type='checkbox')

            self.add_push_button('OK', self.mount_file, None)
            self.add_push_button('Cancel', self.cancel)
            self.main_layout.addLayout(self.button_layout)

        else: # no container ... create crypt file
            self.set_title('Create New Crypt File [luks-tray]')
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
        """ Unmount the file container, close the mapper and detach the loop.

            The unmount loop runs twice because a crypt file can be mounted
            twice -- the plain mount plus the bindfs overlay used for
            read-only access -- and the second pass picks up the layer the
            first one leaves behind. A busy mount stops the retry early and
            skips the `cryptsetup close`, so nothing is closed underneath the
            process still using it.
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
                        for _ in range(2):
                            # it may take two dismounts, one for the regular
                            # mount, and one for the bindfs mount
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
                        # If this is a file container with a loop device,
                        # detach it
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
        """ Validate the form, then create/add/mount the crypt file.

            `uuid` is None when the dialog was opened from "Add Existing" or
            "Create New", in which case a blank container record stands in for
            the one lsblk would have produced. Validation covers the password,
            the crypt file (its directory has to exist), the mount point
            (`check_upon()` against `tray.upons`) and the size in MiB; the
            overwrite checkbox is only consulted when it is present.

            After a successful mount the history entry is keyed by the LUKS
            UUID *inside* the file, because that is what `restore()` verifies
            on every load.
        """

        tray, container = self.tray, None
        errs, values = [], {}
        if tray is None:
            # `LuksTray.singleton` is assigned in LuksTray.__init__; the old
            # `assert` (stripped by `python -O`) merely deferred the failure
            # to the first `tray.` dereference below.
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
        # Label for the error dialog; kept out of `errs` so that list contains
        # errors only (the `if not errs` / `if errs` tests below stay honest).
        context = (container.back_file
                   if (not container.name and container.back_file)
                   else container.name)

        tray.update_mounts()
        mount_point = None

        for key, field in self.inputs.items():
            if isinstance(field, QCheckBox):
                values[key] = field.isChecked()
                continue

            # Assume text fields..
            text = field.text().strip()
            values[key] = text
            if key == 'password':
                if not text:
                    errs.append('ERR: cannot leave password empty')

            elif key == 'back_file':
                path = os.path.abspath(text)
                values[key] = path
                dirname = os.path.dirname(path)
                if not os.path.isdir(dirname):
                    if os.path.basename(dirname) == self.dot_vault_dir:
                        err = run_cmd(['mkdir', '-p', dirname])
                        if err:
                            errs.append(err)
                if not os.path.isdir(os.path.dirname(dirname)):
                    errs.append(f'ERR: Crypt File {path} must be in an existing directory')

            elif key == 'upon':
                path = os.path.abspath(text)
                if 'back_file' in values or container.back_file:
                    back_file = container.back_file if container.back_file else values['back_file']
                    if path == self.vault_dir:
                        basename = os.path.basename(back_file)
                        for suffix in ('.luks', '.luks2', '.crypt'):
                            if basename.endswith(suffix):
                                if len(basename) > len(suffix):
                                    basename = basename[:-len(suffix)]
                        path = os.path.join(self.vault_dir, basename)

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
                    # The user sees the append()ed line; the exception detail
                    # goes to the log for the "why" (e.g. "" vs "12M").
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
                    # The mount point is created inside mount_luks_container(),
                    # as the user, so an empty one can be cleaned up later.
                    # The "create new crypt file" layout has no Read-only
                    # checkbox, so `readonly` is only present for the
                    # mount/add layouts -- indexing it raised KeyError.
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

        # update history with new values if mount worked
        #
        # A new/added file still has uuid=None here. File entries are keyed by
        # the LUKS UUID inside the file (see get_luks_uuid_for_file), so look
        # it up now that the file exists; the path-derived UUID is a
        # best-effort fallback only (such an entry is culled on the next
        # restore if the real UUID cannot be verified).
        if not uuid:
            uuid = (container.uuid or get_luks_uuid_for_file(back_file)
                    or generate_uuid_for_file_path(back_file))
        tray.update_history(uuid, values, back_file=back_file)

        tray.update_menu()
        self.accept()
