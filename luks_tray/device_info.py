#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Device discovery for LUKS TRAY.

    Everything in this module turns the system's view of the block devices
    (``lsblk -J`` and ``/sys``) into the ``SimpleNamespace`` entries the tray
    and the dialogs work with. It deliberately knows nothing about Qt or
    about mount/unmount operations, so it can be exercised on its own.
"""
# pylint: disable=broad-exception-caught, too-few-public-methods

import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

from luks_tray.Utils import prt, prt_once, PROBE_TIMEOUT
from luks_tray import Utils


class DeviceInfo:
    """ Class to dig out the info we want from the system."""
    bans = ('/', '/home', '/var', '/usr', '/tmp', '/opt', '/srv',
            '/boot', '/sys', '/proc', '/dev', '/run')

    def __init__(self, opts, tray):
        self.opts = opts
        self.tray = tray
        self.DB = opts.debug
        self.wids = None
        self.partitions = None
        self.entries = {}
        self.prev_entries_str = ''

    @staticmethod
    def make_partition_namespace(name, size_str):
        """ TBD """
        return SimpleNamespace(name=name,       # /proc/partitions
            opened=None,    # or True or False
            upon='',        # primary mount point
            uuid='',
            size_str=size_str,  # /sys/block/{name}/... (e.g., 3.5T)
            type='',        # e.g., loop, crypt, disk, part
            fstype='',      # fstype OR /sys/class/block/{name}/device/model
            label='',       # blkid
            mounts=[],      # /proc/mounts
            parent=None,    # a partition
            filesystems=[],        # child file systems
            back_file='', # backing file
            vital=None, # history if any
            readonly=False, # whether readonly
            )


    @staticmethod
    def get_device_vendor_model(device_name):
        """ Gets the vendor and model for a given device from the /sys/class/block directory.
        - Args: - device_name: The device name, such as 'sda', 'sdb', etc.
-       - Returns: A string containing the vendor and model information.
        """
        def get_str(device_name, suffix):
            try:
                rv = ''
                fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
                with open(fullpath, 'r', encoding='utf-8') as f: # Read information
                    rv = f.read().strip()
            except (OSError, UnicodeDecodeError) as e:
                # Not every block device has a .../device/model node (loops,
                # dm devices, some partitions). This runs for every device on
                # every 3 s refresh, so say it once per node: the empty string
                # is still the right answer, it just is not a silent one.
                rv = ''
                prt_once(f'no {suffix} for {device_name}',
                         f'note: cannot read {fullpath}: {e}')
            return rv

        # rv = f'{get_str(device_name, "vendor")}' #vendor seems useless/confusing
        rv = f'{get_str(device_name, "model")}'
        return rv.strip()

    @staticmethod
    def is_banned(mounts):
        """ Is a mount point (or any in a list of mountpoints) banned?
            Returns True if so.
        """
        if not isinstance(mounts, list):
            mounts = [mounts]
        for mount in mounts:
            if mount in DeviceInfo.bans:
                return True
        return False

    def parse_lsblk(self):
        """ Parse ls_blk for all the goodies we need """
        def get_backing_file(loop_device):
            node = f'/sys/block/{loop_device}/loop/backing_file'
            try:
                with open(node, 'r', encoding='utf-8') as f:
                    backing_file = f.read().strip()
                return backing_file
            except FileNotFoundError as e:
                # A loop device set up on a block device (or one created by
                # systemd) has no backing_file node at all. Expected, but it is
                # exactly what makes the tray treat the loop as file-less.
                prt_once(f'no {node}', f'note: {node}: {e}')
                return ''
            except OSError as e:
                prt_once(f'unreadable {node}', f'WARN: cannot read {node}: {e}')
                return ''

        def eat_one(device):
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

               # Run the `lsblk` command and get its output in JSON format with additional columns
        try:
            result = subprocess.run(['lsblk', '-J', '-o',
                        'NAME,MAJ:MIN,TYPE,RO,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,UUID,MOUNTPOINTS', ],
                        stdout=subprocess.PIPE, text=True, check=False,
                        timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # This runs from the menu-refresh timer: block the event loop for
            # the whole deadline and the tray looks dead. Keep the previous
            # snapshot instead (it is {} on the very first poll) and let the
            # next tick try again.
            prt(f'lsblk timed out after {PROBE_TIMEOUT}s {Utils.where()}')
            return self.entries
        parsed_data = json.loads(result.stdout)
        dev_cons, file_cons = {}, {}

        # Parse each block device and its properties
        for device in parsed_data['blockdevices']:
            parent = eat_one(device)
            parent.fstype = self.get_device_vendor_model(parent.name)
            for child in device.get('children', []):
                entry = eat_one(child)
                # entry.parent = parent.name
                entry.parent = parent
                if not parent.fstype:
                    parent.fstype = 'DISK'
                if parent.type == 'loop':
                    # IN the case of the loop device (or file container)
                    # use the UUID of the container
                    entry.uuid = parent.uuid
                    entry.back_file = parent.back_file
                elif 'luks' not in entry.fstype.lower():
                    continue
                # if parent.name not in entries:
                    # entries[parent.name] = parent
                if entry.type == 'crypt':
                    file_cons[entry.uuid] = entry
                else:
                    dev_cons[entry.uuid] = entry
                grandchildren = child.get('children', None)
                if entry.type == 'crypt':
                    if entry.mounts:
                        if self.is_banned(entry.mounts):
                            continue # skip whole disk entries
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
                    # entries[subentry.name] = subentry
                    if len(grandchildren) == 1 and len(subentry.mounts) == 1:
                        entry.upon = subentry.mounts[0]
                        # The device name in /proc/mounts is the subentry's name (e.g., /dev/mapper/luks_vol)
                        ns = self.tray.mount_infos.get(f'/dev/mapper/{subentry.name}',
                                              self.tray.mount_infos.get(subentry.name, None))
                        if ns:
                            entry.readonly = ns.readonly
                            # Optionally, you can also set entry.upon here if needed,
                            # but it's often better to rely on lsblk's MOUNTPOINTS data first.
                            if self.is_banned(entry.mounts):
                                continue # skip whole disk entries

        self.entries = dev_cons | file_cons
        if self.DB:
            #s = StringIO()
            temps = []
            for entry in self.entries.values():
                tmp_row = {}
                row = vars(entry)
                for key, value in row.items():
                #   if isinstance(value, SimpleNamespace):
                #       tmp_row[key] = str(vars(value))
                #   else:
                    tmp_row[key] = str(value)
                temps.append(tmp_row)
                #print(vars(entry), file=s)
            entries_str = json.dumps(temps, indent=4)
            if entries_str != self.prev_entries_str:
                dt = datetime.now().strftime('%m-%d^%H:%M:%S')
                print(f'\n\nDB: {dt} --->>> after parse_lsblk:')
                print(entries_str)
                self.prev_entries_str = entries_str
#               print(f'{back_files=}')

        return self.entries


    def get_relative(self, name):
        """ TBD """
        return self.entries.get(name, None)
