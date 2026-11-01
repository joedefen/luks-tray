#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Dialog classes for LUKS TRAY.

    Every dialog receives the running tray instance explicitly, so this
    package never has to import ``luks_tray.main`` (which imports the
    dialogs) and there is no circular-import dance.
"""
from luks_tray.dialogs.common import (IS_SWAY_LIKE_ENV, CommonDialog,
                                      requires_manual_title)
from luks_tray.dialogs.master_password import MasterPasswordDialog
from luks_tray.dialogs.mount_device import MountDeviceDialog
from luks_tray.dialogs.mount_file import MountFileDialog

__all__ = [
    'IS_SWAY_LIKE_ENV',
    'CommonDialog',
    'MasterPasswordDialog',
    'MountDeviceDialog',
    'MountFileDialog',
    'requires_manual_title',
]
