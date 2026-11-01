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
from types import SimpleNamespace

# Where prt() sends its output, and the size that rotates the log. One private
# object replaces the pair of bare globals this used to be (`prt_path` plus an
# "already redirected" flag): the flag could only ever be set, so assigning the
# path a second time was a silent no-op and the target was decided by whichever
# prt() call happened to run first. `configure_log()` owns that transition now.
# `opened_path` is the file *this process* redirected fd 1 onto ('' = never), so
# a later `configure_log()` may take the stream over again -- while a stdout the
# caller redirected themselves (a regular file we did not open) stays untouched.
_log = SimpleNamespace(path='', opened_path='', max_kb=512)
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


def _is_tty():
    """ True if stdout is a terminal. Silent by design: called from prt(). """
    try:
        os.ttyname(sys.stdout.fileno())
        return True
    except Exception:                               # pylint: disable=broad-except
        return False


def _is_reg():
    """ True if stdout is already an open regular file. Silent, same reason. """
    try:
        return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
    except Exception:                               # pylint: disable=broad-except
        return False


def _reopen_log():
    """ Point stdout and stderr at the configured log, in append mode. """
    try:
        sys.stdout = open(_log.path, "a+", encoding='utf-8')
    except OSError:
        return              # Cannot log there; keep whatever stdout already is.
    sys.stderr = sys.stdout
    os.dup2(sys.stdout.fileno(), 1)
    os.dup2(sys.stderr.fileno(), 2)
    _log.opened_path = _log.path


def configure_log(path, max_kb=512):
    """ Point prt() at `path` and redirect stdout/stderr onto it.

        Called from `main()` in place of assigning `Utils.prt_path` directly.
        The redirection happens here rather than lazily on the next prt(), so a
        message logged while the program is still coming up (the `IniTool`
        constructor, for instance) reaches the file instead of a stdout a
        desktop launch has already pointed nowhere.

        A second call *moves* the log: `opened_path` records the file this
        process opened, so the stream can be taken over again, where the old
        globals kept the very first decision for the lifetime of the process. A
        terminal is left alone (prt() keeps printing to it), and so is a stdout
        the caller redirected to a file of their own. `max_kb <= 0` disables the
        redirection and the size cap, so prt() just prints.
    """
    _log.path = path or ''
    _log.max_kb = max_kb
    if not _log.path or _log.max_kb <= 0:
        return
    if _is_tty():
        return
    if _is_reg() and not _log.opened_path:
        return              # Someone redirected stdout; not ours to replace.
    _reopen_log()


def prt(*args, **kwargs):
    """ Our custom print routine ...
     - use instead of print() to get time stamps.
     - unless stdout is a tty, say for debugging, ~/.config/luks-tray/debug.log is used for stdout
     - if we create a log file, its size is limited to 512K and then it is truncated
    """
    def check_stdout(use_stdout=None):
        """ Make sure stdout points at a terminal or at the debug log.

            The probes above (`_is_tty()` / `_is_reg()`) stay silent on purpose:
            they run *inside* prt(), so reporting a failure here would call prt()
            again and recurse until the stack runs out. "Not a tty" / "not a
            regular file" is also the normal answer for a tray started by a
            desktop session.
        """
        if _log.max_kb > 0 and _log.path: # non-positive disables stdout "tuning"
            if use_stdout is False:
                _reopen_log()
            elif not _log.opened_path and sys.stdout.closed: # stdout is closed
                _reopen_log()
            elif not _log.opened_path and not _is_tty() and not _is_reg():
                _reopen_log()
            if os.fstat(sys.stdout.fileno()).st_size > _log.max_kb*1024:
                shutil.move(_log.path, f'{_log.path}1')
                _reopen_log()

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
