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
        """ Return the blank per-device record the tray and dialogs expect.

            `parse_lsblk()` fills these in from `lsblk -J`; the dialogs also
            build one for a file container that no block device represents
            yet. Every field is present up front so callers never need
            `getattr(..., default)` just because a device was not parsed.
        """
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
        """ Return the hardware model of `device_name` from /sys ('' if none).

            Reads `/sys/class/block/<name>/device/model`, which is what
            distinguishes real disks in the menu ('Samsung SSD 980 ...'). Most
            block devices have no such attribute at all and that is normal -- see
            `get_str()` -- so the empty string is an answer, not a failure.
        """
        def get_str(device_name, suffix):
            """ Read one /sys attribute; '' when the device has no such node. """
            fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
            try:
                with open(fullpath, 'r', encoding='utf-8') as f: # Read information
                    return f.read().strip()
            except (FileNotFoundError, NotADirectoryError):
                # The normal case, and not a problem to report: a block device
                # with no hardware behind it -- zram (swap held in RAM), a
                # device-mapper node, a loop device, md/raid, nbd -- has no
                # `device/` node at all, so there is no model to read and '' is
                # the right answer (the menu then shows 'DISK'). Most partitions
                # lack it too. This runs for every device on every 3 s refresh,
                # so it stays quiet.
                return ''
            except (OSError, UnicodeDecodeError) as e:
                # A node that exists but cannot be read is unusual -- a
                # locked-down /sys, an I/O error, garbage in the attribute --
                # and worth exactly one line, because the consequence is
                # invisible otherwise: the device is listed without a model.
                prt_once(f'unreadable {fullpath}',
                         f'WARN: cannot read the model of {device_name!r} '
                         f'({fullpath}): {e}; listing it without one')
                return ''

        # Only the model: the vendor string adds nothing the user recognises
        # ("Generic", "ATA", ...) and made the menu line too long.
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
        """ Rebuild `self.entries` from one `lsblk -J` snapshot.

            Walks the tree top-down: a top-level entry is a disk or a loop
            device (a file container), its children are partitions or crypt
            mappers, and the grandchildren are the filesystems inside them.
            Containers are keyed by UUID -- device containers and file
            containers are kept separately and merged at the end.

            Entries are split by `type`/`fstype`: anything that is not a LUKS
            child is skipped, mount points that are banned by `is_banned()` are
            dropped whole, and `opened` records whether a mapper exists at all.
            On a `lsblk` timeout the previous snapshot is returned unchanged.
        """
        def get_backing_file(loop_device):
            """ File behind a loop device, or '' when it is not a file loop. """
            node = f'/sys/block/{loop_device}/loop/backing_file'
            try:
                with open(node, 'r', encoding='utf-8') as f:
                    backing_file = f.read().strip()
                return backing_file
            except FileNotFoundError:
                # A loop device set up on a block device has no backing_file
                # node at all, and a detached one loses it. Expected: '' is the
                # right answer, and it is simply not a file container, so there
                # is nothing to tell the user.
                return ''
            except OSError as e:
                prt_once(f'unreadable {node}', f'WARN: cannot read {node}: {e}')
                return ''

        def eat_one(device):
            """ Turn one `lsblk -J` node into a populated namespace. """
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

        # Run the `lsblk` command and get its output in JSON format with
        # additional columns
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
            if device.get('children'):
                # Only a device with children can become a menu entry (see the
                # loop below), so reading the model for the rest -- zram, dm,
                # an unattached disk -- was a /sys lookup per device per 3 s
                # tick whose answer is thrown away.
                parent.fstype = self.get_device_vendor_model(parent.name)
            for child in device.get('children', []):
                entry = eat_one(child)
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
                    if len(grandchildren) == 1 and len(subentry.mounts) == 1:
                        entry.upon = subentry.mounts[0]
                        # The device name in /proc/mounts is the subentry's name (e.g., /dev/mapper/luks_vol)
                        ns = self.tray.mount_infos.get(f'/dev/mapper/{subentry.name}',
                                              self.tray.mount_infos.get(subentry.name, None))
                        if ns:
                            # /proc/mounts is authoritative for the `ro` flag;
                            # lsblk's RO column describes the block device, not
                            # the way it happens to be mounted.
                            entry.readonly = ns.readonly
                            if self.is_banned(entry.mounts):
                                continue # skip whole disk entries

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
        """ Return the parsed entry called `name`, or None if unknown. """
        return self.entries.get(name, None)
