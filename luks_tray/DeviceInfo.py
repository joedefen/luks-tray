#!/usr/bin/env python3
"""Device discovery for LUKS TRAY."""
# pylint: disable=broad-exception-caught, too-few-public-methods

import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

from luks_tray import Utils
from luks_tray.Utils import PROBE_TIMEOUT, prt, prt_once


class DeviceInfo:
    """Extract the container information the tray and dialogs need."""

    bans = ('/', '/home', '/var', '/usr', '/tmp', '/opt', '/srv',
            '/boot', '/sys', '/proc', '/dev', '/run')

    def __init__(self, opts, tray):
        """Store the options and tray, and reset the parsed state.

        Args:
            opts: Parsed command-line options.
            tray: The running tray instance.
        """
        self.opts = opts
        self.tray = tray
        self.DB = opts.debug
        self.wids = None
        self.partitions = None
        self.entries = {}
        self.prev_entries_str = ''

    @staticmethod
    def make_partition_namespace(name, size_str):
        """Return the blank per-device record used by the tray and dialogs.

        Args:
            name: Device name.
            size_str: Human-readable size.

        Returns:
            SimpleNamespace: A record with every expected field present.
        """
        return SimpleNamespace(name=name,
            opened=None,
            upon='',
            uuid='',
            size_str=size_str,
            type='',
            fstype='',
            label='',
            mounts=[],
            parent=None,
            filesystems=[],
            back_file='',
            meta_type='',
            vital=None,
            readonly=False,
            )

    @staticmethod
    def get_device_vendor_model(device_name):
        """Return the hardware model of `device_name`, or `''`.

        Args:
            device_name: Block device name.

        Returns:
            str: The model string from `/sys`.
        """
        def get_str(device_name, suffix):
            """Read one `/sys` attribute, or return `''`.

            Args:
                device_name: Block device name.
                suffix: Attribute file name.

            Returns:
                str: The attribute value.
            """
            fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
            try:
                with open(fullpath, encoding='utf-8') as f:
                    return f.read().strip()
            except (FileNotFoundError, NotADirectoryError):
                # Devices without hardware have no `device/` node.
                return ''
            except (OSError, UnicodeDecodeError) as e:
                prt_once(f'unreadable {fullpath}',
                         f'WARN: cannot read the model of {device_name!r} '
                         f'({fullpath}): {e}; listing it without one')
                return ''

        rv = f'{get_str(device_name, "model")}'
        return rv.strip()

    @staticmethod
    def is_banned(mounts):
        """Return True when any mount point is banned.

        Args:
            mounts: One mount point or a list of them.

        Returns:
            bool: True when a mount point is banned.
        """
        if not isinstance(mounts, list):
            mounts = [mounts]
        for mount in mounts:
            if mount in DeviceInfo.bans:
                return True
        return False

    def parse_lsblk(self):
        """Rebuild `self.entries` from one `lsblk -J` snapshot.

        Returns:
            dict: The parsed entries keyed by container identity.
        """
        def get_backing_file(loop_device):
            """Return the file behind a loop device, or `''`."""
            node = f'/sys/block/{loop_device}/loop/backing_file'
            try:
                with open(node, encoding='utf-8') as f:
                    backing_file = f.read().strip()
                return backing_file
            except FileNotFoundError:
                return ''
            except OSError as e:
                prt_once(f'unreadable {node}', f'WARN: cannot read {node}: {e}')
                return ''

        def eat_one(device):
            """Turn one `lsblk -J` node into a populated namespace."""
            entry = self.make_partition_namespace('', '')
            entry.name=device.get('name', '')
            entry.type = device.get('type', '')
            entry.readonly = bool(device.get('ro', 0))
            entry.fstype = device.get('fstype', '')
            if entry.fstype is None:
                entry.fstype = ''
            entry.label = device.get('label', '')
            if not entry.label:
                entry.label=device.get('partlabel', '')
            if entry.label is None:
                entry.label = ''
            entry.size_str=device.get('size', '')
            entry.uuid = device.get('uuid', '')
            mounts = device.get('mountpoints', [])
            while len(mounts) >= 1 and mounts[0] is None:
                del mounts[0]
            entry.mounts = mounts
            if entry.type == 'loop':
                entry.back_file = get_backing_file(entry.name)
            return entry

        try:
            result = subprocess.run(['lsblk', '-J', '-o',
                        'NAME,MAJ:MIN,TYPE,RO,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,UUID,MOUNTPOINTS', ],
                        stdout=subprocess.PIPE, text=True, check=False,
                        timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Keep the previous snapshot so the poller stays responsive.
            prt(f'lsblk timed out after {PROBE_TIMEOUT}s {Utils.where()}')
            return self.entries
        parsed_data = json.loads(result.stdout)
        dev_cons, file_cons = {}, {}

        for device in parsed_data['blockdevices']:
            parent = eat_one(device)
            if device.get('children'):
                parent.fstype = self.get_device_vendor_model(parent.name)
            for child in device.get('children', []):
                entry = eat_one(child)
                entry.parent = parent
                if not parent.fstype:
                    parent.fstype = 'DISK'
                if parent.type == 'loop':
                    entry.uuid = parent.uuid
                    entry.back_file = parent.back_file
                elif 'luks' not in entry.fstype.lower():
                    continue
                if entry.type == 'crypt':
                    file_cons[entry.uuid] = entry
                else:
                    dev_cons[entry.uuid] = entry
                grandchildren = child.get('children', None)
                if entry.type == 'crypt':
                    if entry.mounts:
                        if self.is_banned(entry.mounts):
                            continue
                        entry.upon = entry.mounts[0]
                elif not isinstance(grandchildren, list):
                    entry.opened = False
                    continue
                entry.opened = True
                grandchildren = child.get('children', [])
                for grandchild in grandchildren:
                    subentry = eat_one(grandchild)
                    subentry.parent = entry.name
                    entry.filesystems.append(subentry)
                    if len(grandchildren) == 1 and len(subentry.mounts) == 1:
                        entry.upon = subentry.mounts[0]
                        ns = self.tray.mount_infos.get(f'/dev/mapper/{subentry.name}',
                                              self.tray.mount_infos.get(subentry.name, None))
                        if ns:
                            entry.readonly = ns.readonly
                            if self.is_banned(entry.mounts):
                                continue

        self.entries = dev_cons | file_cons
        if self.DB:
            temps = []
            for entry in self.entries.values():
                tmp_row = {}
                row = vars(entry)
                for key, value in row.items():
                    tmp_row[key] = str(value)
                temps.append(tmp_row)
            entries_str = json.dumps(temps, indent=4)
            if entries_str != self.prev_entries_str:
                dt = datetime.now().strftime('%m-%d^%H:%M:%S')
                print(f'\n\nDB: {dt} --->>> after parse_lsblk:')
                print(entries_str)
                self.prev_entries_str = entries_str

        return self.entries

    def get_relative(self, name):
        """Return the parsed entry called `name`, or None.

        Args:
            name: Entry key.

        Returns:
            SimpleNamespace | None: The entry, if known.
        """
        return self.entries.get(name, None)
