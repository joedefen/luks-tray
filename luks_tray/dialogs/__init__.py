#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Dialog classes for LUKS TRAY.

    Every dialog receives the running tray instance explicitly, so this
    package never has to import ``luks_tray.Main`` (which imports the
    dialogs) and there is no circular-import dance.
"""
from luks_tray.dialogs.Common import CommonDialog, requires_manual_title
from luks_tray.dialogs.MasterPassword import MasterPasswordDialog
from luks_tray.dialogs.MountDevice import MountDeviceDialog
from luks_tray.dialogs.MountFile import MountFileDialog

__all__ = [
    'CommonDialog',
    'MasterPasswordDialog',
    'MountDeviceDialog',
    'MountFileDialog',
    'requires_manual_title',
]
