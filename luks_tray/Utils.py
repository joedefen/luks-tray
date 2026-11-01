#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utility functions
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import os
import sys
import stat
import shutil
import inspect
from datetime import datetime
from io import StringIO

import importlib.resources as pkg_resources

prt_kb = 512
prt_path = ''
prt_to_init = True
# Keys already emitted through prt_once(). Bounded, because the tray runs for
# weeks: when the set fills up it is cleared wholesale, which at worst repeats
# a warning the user has already seen.
_prt_seen = set()
PRT_ONCE_MAX = 256

# Deadline, in seconds, for external commands that change system state
# (cryptsetup, mount, umount, mkfs, losetup). They all run on the Qt main
# thread, so a wedged child freezes the tray menu until it returns -- and
# main() points stdin at /dev/null, so there is no terminal left to
# interrupt it. 60s leaves room for a slow argon2id unlock on old hardware.
CMD_TIMEOUT = 60
# Deadline for read-only probes (lsblk, blkid, fuser). They are expected to
# answer at once; a hung probe just keeps the previous snapshot and is
# retried on the next menu refresh.
PROBE_TIMEOUT = 15
# Deadline for the one-shot setup commands that legitimately take a while
# (mke2fs on a multi-terabyte container, luksFormat's PBKDF benchmark on a
# slow machine). Still bounded -- just far more generous than CMD_TIMEOUT.
FORMAT_TIMEOUT = 900
# Status reported when a deadline is hit (the value coreutils `timeout` uses);
# every caller already treats a non-zero status as failure.
TIMEOUT_RC = 124

def copy_to_folder(resource_name, dest):
    """ Get the path of a resource """
    with pkg_resources.path('luks_tray.resources', resource_name) as file_path:
        shutil.copy(file_path, os.path.join(dest, resource_name))
    return file_path


def where(above=0):
    """Get the file and line of the caller. Arguments:
     -  above -- how many frames to go up (or down) from the reference
        frame (which is 2 above). above=0 means the caller of the
        caller of the function (which is the frame ofkjk interest usually).
    Then keep going up until we get out of this file if possible
    Returns:
        [file:line]
    """
    stack, frameNo = inspect.stack(), 2 + above
    if frameNo < 0 or len(stack) < frameNo + 2:
        return '[n/a]'
    filename, line_number = stack[frameNo][1:3]
    return f'[{filename.split("/")[-1]}:{line_number}]'


def prt(*args, **kwargs):
    """ Our custom print routine ...
     - use instead of print() to get time stamps.
     - unless stdout is a tty, say for debugging, ~/.config/luks-tray/debug.log is used for stdout
     - if we create a log file, its size is limited to 512K and then it is truncated
     """
    def check_stdout(use_stdout=None):
        """ Make sure stdout points at a terminal or at the debug log. """
        global prt_to_init
        # The two probes below deliberately stay silent: they run *inside*
        # prt(), so logging a failure here would call prt() again and recurse
        # until the stack runs out. "Not a tty" / "not a regular file" is also
        # the normal answer for a tray started by a desktop session.
        def is_tty():
            """ True if stdout is a terminal. """
            try:
                os.ttyname(sys.stdout.fileno())
                return True
            except Exception:                       # pylint: disable=broad-except
                return False
        def is_reg():
            """ True if stdout is already an open regular file (the log). """
            try:
                return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
            except Exception:                       # pylint: disable=broad-except
                return False
        def reopen():
            """ Point stdout and stderr at the debug log in append mode. """
            global prt_to_init
            sys.stdout = open(prt_path, "a+", encoding='utf-8')
            sys.stderr = sys.stdout
            os.dup2(sys.stdout.fileno(), 1)
            os.dup2(sys.stderr.fileno(), 2)
            prt_to_init = False

        if prt_kb > 0 and prt_path: # non-positive disables stdout "tuning"
            if use_stdout is False:
                reopen()
            elif prt_to_init and sys.stdout.closed: # Check if stdout is closed
                reopen()
            elif prt_to_init and not is_tty() and not is_reg():
                reopen()
            if os.fstat(sys.stdout.fileno()).st_size > prt_kb*1024:
                shutil.move(prt_path, f'{prt_path}1')
                reopen()

    to_stdout = None
    if 'to_stdout' in kwargs:
        to_stdout = bool(kwargs['to_stdout'])
        del kwargs['to_stdout']
    # prt_once() wraps this function, so it asks for one extra frame in the
    # [file:line] footer -- otherwise every one-time note would point at the
    # wrapper instead of at the handler that actually noticed the failure.
    above = 0
    if '_above' in kwargs:
        above = int(kwargs['_above'])
        del kwargs['_above']
    check_stdout(to_stdout)

    dt = datetime.now().strftime('%m-%d^%H:%M:%S')
    s = StringIO()
    print(dt, end=' ', file=s)
    kwargs['end'] = ' '
    kwargs['file'] = s
    print(*args, **kwargs)
    string = f'{s.getvalue()} {where(above)}'
    print(string, flush=True)


def prt_once(key, *args, **kwargs):
    """ prt() at most once per `key` for the lifetime of this process.

        The menu refresh runs every 3 s, and several handlers on that path
        raise the *same* exception on every tick -- a block device without a
        `/sys/.../device/model` node, a loop device with no `backing_file`, an
        auto-mount folder that is not empty. Logging those with plain prt()
        would rotate the 512 KB debug log within minutes and bury the message
        that actually matters, so they log through here instead: the key is
        the identity of the failure (usually the path plus what failed), so a
        genuinely *new* failure still gets printed.
    """
    if key in _prt_seen:
        return
    if len(_prt_seen) >= PRT_ONCE_MAX:
        _prt_seen.clear()
    _prt_seen.add(key)
    kwargs['_above'] = kwargs.get('_above', 0) + 1
    prt(*args, **kwargs)
