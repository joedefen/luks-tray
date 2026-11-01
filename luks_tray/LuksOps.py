#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Privileged command helpers and LUKS primitives for LUKS TRAY.

    Two layers live here:

    - process execution: [`run_cmd_capture()`](luks_tray/LuksOps.py:1) and
      its ``sudo -n`` variant return real output, while
      [`run_cmd()`](luks_tray/LuksOps.py:1) / ``sudo_cmd()`` keep the
      ``None``-or-``'FAIL: ...'`` contract every call site tests;
    - LUKS work: unlock, mount (optionally through bindfs), loop setup and the
      single ``mount_luks_container()`` entry point used by both dialogs.

    Nothing here touches Qt widgets (except the busy-mount warning in
    ``run_unmount()``) or the tray class, so the primitives can be called from
    anywhere by passing the tray in as an argument.
"""
# pylint: disable=broad-exception-caught, invalid-name, too-many-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements

import os
import hashlib
import subprocess
import traceback

from PyQt6.QtWidgets import QMessageBox

from luks_tray.ContainerType import CONTAINER_TYPES, LUKS, TCRYPT, salt_identity
from luks_tray.Utils import prt, CMD_TIMEOUT, PROBE_TIMEOUT, FORMAT_TIMEOUT, TIMEOUT_RC


def generate_uuid_for_file_path(file_path):
    """ Use SHA-256 to hash the file path """
    file_hash = hashlib.sha256(file_path.encode('utf-8')).hexdigest()

    # Take the first 32 characters of the hash and format it as a UUID
    # UUID is normally 32 hexadecimal digits, formatted as 8-4-4-4-12
    generated_uuid = f"{file_hash[:8]}-{file_hash[8:12]}-{file_hash[12:16]}-{file_hash[16:20]}-{file_hash[20:32]}"

    return generated_uuid

def get_luks_uuid_for_file(file_path):
    """ Return the LUKS UUID *inside* a LUKS file ('' if it cannot be read).

        History entries for file containers are keyed by this UUID -- the same
        value lsblk reports for the loop device and the same value
        HistoryClass re-checks with blkid on restore. A path-derived UUID
        (generate_uuid_for_file_path) never matches, so such an entry is
        silently purged the next time the history is loaded.
    """
    if not file_path:
        return ''
    rc, stdout, _stderr = run_cmd_capture(
            ['blkid', '-o', 'value', '-s', 'UUID', file_path])
    return stdout.strip() if rc == 0 else ''

def get_container_identity(file_path):
    """ The history key for the container stored in `file_path`.

        A LUKS file is keyed by the UUID inside it -- the same value
        HistoryClass re-checks on restore. A TrueCrypt/VeraCrypt file has no
        such UUID (its header is encrypted), so it is keyed by its salt; see
        ContainerType.py. The path-derived UUID is the last resort: nothing
        re-derives it, so such an entry is dropped on the next restore rather
        than silently inheriting another volume's password.
    """
    if not file_path:
        return ''
    return (get_luks_uuid_for_file(file_path)
            or salt_identity(file_path)
            or generate_uuid_for_file_path(file_path))

def sudo_cmd(args, errs=None, input_str=None, timeout=None):
    """ run sudo -n {args}; the -n will avoid prompting for a
        sudo password and fail if not allowed
    """
    args = ['sudo', '-n'] + args
    return run_cmd(args, errs, input_str, timeout)

def run_cmd(args, errs=None, input_str=None, timeout=None):
    """ Run args and return None on success, or a 'FAIL: ...' string on failure.

        stdout is deliberately NOT returned here -- this helper is a
        success/failure predicate. Use run_cmd_capture() when you need the
        child's output.

        `timeout` is forwarded to run_cmd_capture() (default CMD_TIMEOUT);
        pass FORMAT_TIMEOUT for the slow, one-shot setup commands.
    """
    rc, stdout, stderr = run_cmd_capture(args, input_str, timeout)
    if rc == 0:
        return None
    # NB: keep the outer quote different from the one used inside the
    # expression -- quote reuse is PEP 701 / Python 3.12+ only.
    err = f"FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]"
    # NOTE: test against None, not truthiness -- callers routinely pass a
    # freshly created [] to collect errors and an empty list is falsy.
    if errs is not None:
        errs.append(err)
    return err

def _as_text(value):
    """ Coerce TimeoutExpired.stdout/.stderr to str.

        Depending on the Python version those attributes hold None, raw bytes
        (the child was killed mid-write) or already-decoded text.
    """
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode(errors='replace')
    return value

def run_cmd_capture(args, input_str=None, timeout=None):
    """ Run args via subprocess.run(); return (returncode, stdout, stderr).

        `timeout` defaults to CMD_TIMEOUT. A child that overruns the deadline
        is killed (subprocess.run() does that for us) and reported through
        TIMEOUT_RC, so callers need no extra branch: run_cmd()/sudo_cmd()
        just render it as their usual 'FAIL: ...' string. Without this, a
        hung cryptsetup/mount blocked the Qt main thread forever and the tray
        had no terminal left (stdin is /dev/null) to interrupt it.
    """
    if timeout is None:
        timeout = CMD_TIMEOUT
    try:
        sub = subprocess.run(args, input=input_str, capture_output=True,
                             text=True, check=False, timeout=timeout)
        return sub.returncode, sub.stdout, sub.stderr
    except subprocess.TimeoutExpired as exce:
        # sudo execs the real command in the same process, so the kill lands
        # on cryptsetup/mount itself rather than on a wrapper.
        return TIMEOUT_RC, _as_text(exce.stdout), (
            f'{_as_text(exce.stderr)}timed out after {timeout}s')

def sudo_run(args, input_str=None, timeout=None):
    """ Like run_cmd_capture(), but with sudo -n (never prompts). """
    return run_cmd_capture(['sudo', '-n'] + args, input_str, timeout)


def ensure_dir(path):
    """ Create `path` (and any missing parents) as the *user*, not as root.

        Auto-mount points live inside the user's own vault area, and the
        startup cleanup removes the empty ones with a plain `os.rmdir()`, which
        needs write permission on their *parent*. Creating them with
        `sudo mkdir -p` therefore left directories the unprivileged tray could
        never tidy up again -- and the `-p` also created the vault root itself
        as root, which blocked every later removal below it.

        `mount` does not care who owns the mount point, so the user's own tree
        is created directly, and sudo is only used as a fallback for a path
        outside it (e.g. a manual mount point below /mnt).
    """
    if os.path.isdir(path):
        return None
    try:
        os.makedirs(path, exist_ok=True)
        return None
    except PermissionError:
        return sudo_cmd(['mkdir', '-p', path])
    except OSError as exc:
        # Existing non-directory, read-only parent, bad path component, ...
        return f'FAIL: cannot create {path!r}: {exc}'


def run_unmount(mount_point: str, busy_warns: set) -> str | None:
    """Attempts to unmount the given mount point.
    If it fails due to 'busy', show a popup with the list of processes using it.
    Returns error string or None on success.
    """
    try:
        sub = subprocess.run(['sudo', "umount", mount_point],
                    capture_output=True, text=True, check=False,
                    timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f'FAIL: umount {mount_point}: timed out after {CMD_TIMEOUT}s'
    except Exception as e:
        return f"FAIL: umount {mount_point}: Exception: {e}"

    if sub.returncode == 0:
        return None  # success

    err = f"FAIL: umount {mount_point}: {sub.stdout.strip()} {sub.stderr.strip()} [rc={sub.returncode}]"

    if 'busy' not in sub.stderr.lower() or mount_point in busy_warns:
        return err  # Not a 'busy' error — no popup needed

    # Try to get processes using the mount point
    try:
        fuser = subprocess.run(
                ['sudo', "fuser", "-vm", mount_point],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=PROBE_TIMEOUT
            )
        fuser_out = fuser.stderr.strip()  # This is the real data

    except Exception as e:
        fuser_out = f"(Could not get process info: {e})"

    busy_warns.add(mount_point)

    # Extract just PID and COMMAND, skip 'kernel' and header lines
    process_lines = []
    for line in fuser_out.splitlines():
        if line.startswith("USER") or mount_point in line:
            continue  # Skip header and mount line
        process_lines.append(line.strip())

    info = "\n - ".join(process_lines) if process_lines else "(No user-space processes found using the mount)"

    # Show user-friendly popup.  `setWindowTitle`, not `set_title`: QMessageBox
    # has no `set_title`, so this path raised AttributeError on the first
    # "target is busy" -- the most ordinary unmount failure there is -- instead
    # of naming the processes holding the mount open.
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setWindowTitle("Unmount Failed — Device Busy [luks-tray]")
    msg.setText(f"'{mount_point}' busy by these processes:\n - {info}")
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()

    return err


####################################################
# LUKS Primitives
####################################################
def _open_argv(device_path, luks_device, readonly, meta_type):
    """ The `cryptsetup open` command for one declared header type. """
    args = ['cryptsetup', 'open', '--type', meta_type]
    if meta_type == TCRYPT:
        # A TCRYPT header carries a PIM. cryptsetup's "use the default
        # iteration count" is 0, which is also the only thing a
        # TrueCrypt-era container has, and a wrong PIM is reported exactly
        # like a wrong password -- so passing 0 is safe for every container
        # this applet creates and for the legacy ones it is aimed at.
        args += ['--veracrypt-pim', '0']
    if readonly:
        args.append('--readonly')
    return args + ['--key-file', '-', device_path, luks_device]


def _unlock_luks(device_path, password, luks_device, readonly=False,
                 meta_type=LUKS):
    """ One `cryptsetup open` attempt, for one declared header type. """
    # No "already unlocked?" short-circuit here on purpose. It used to be
    # `if hasattr(self, 'opened') and self.opened`, but `self` was the
    # dialog, which never defines `opened`, so the test never fired.
    # Making it live (e.g. "the mapper path exists") would skip the
    # password check and mount a volume somebody else has open, so a
    # repeat `cryptsetup open` is left to fail with its own message.
    return sudo_cmd(_open_argv(device_path, luks_device, readonly, meta_type),
                    input_str=password)


def unlock_container(device_path, password, luks_device, readonly=False,
                     meta_type=''):
    """ Open a container, asking for the declared type first.

        Returns `(err, used_type)`, where `used_type` is the type that opened
        the container (or the last one tried when nothing did).

        The type cannot be read out of the file -- a LUKS header is cleartext,
        a TrueCrypt/VeraCrypt one is encrypted (see ContainerType.py) -- so it
        is asked for, and the *failure* decides whether the other type is
        worth a turn:

        - rc=1 is cryptsetup's "is not a valid LUKS device". That is a
          deterministic verdict about the header, so the same password is
          handed to `--type tcrypt` next;
        - anything else (rc=2 "no key available with this passphrase", a
          timeout, a missing device node) is about the password or the system,
          and a second attempt would only replace a useful message with a
          confusing one.

        `meta_type=''` means "not known yet", which starts with LUKS: that is
        what every container this applet creates is, and rc=1 makes the
        fallback to TCRYPT cheap.
    """
    first = meta_type if meta_type in CONTAINER_TYPES else LUKS
    order = [first] + [kind for kind in CONTAINER_TYPES if kind != first]
    failure, tried = '', first
    for kind in order:
        tried = kind
        argv = ['sudo', '-n'] + _open_argv(device_path, luks_device, readonly,
                                           kind)
        rc, stdout, stderr = run_cmd_capture(argv, input_str=password)
        if rc == 0:
            return None, kind
        # Same shape run_cmd() gives every other failure, so the dialog shows
        # this one the same way.
        failure = f"FAIL: {' '.join(argv)}: {stdout} {stderr} [rc={rc}]"
        if rc != 1:
            return failure, tried
    return failure, tried


def _mount_manual(tray, mapper_path, upon, do_bindfs=False, readonly=False):
    """Manual mounting with bindfs"""
    if readonly:
        err = sudo_cmd(['mount', '-o', 'ro', mapper_path, upon])
    else:
        err = sudo_cmd(['mount', mapper_path, upon])
    if do_bindfs and not err:
        err = sudo_cmd(['bindfs', '-u', str(tray.uid), '-g', str(tray.gid),
                      upon, upon])
    return err

def _setup_loop_device(container):
    """Set up loop device for file-based containers"""
    # The same dead `hasattr(self, 'opened')` test lived here while this was
    # a method: `self` was the dialog, so it never fired -- and it tested the
    # wrong object anyway, since the loop state ends up on `container.name`.

    # Use --show to get the loop device name; we need its stdout, so call
    # subprocess directly instead of sudo_cmd() (which only reports errors).
    args = ['losetup', '-f', '--show', container.back_file]
    rc, stdout, stderr = sudo_run(args)
    if rc != 0:
        return (f"FAIL: {' '.join(args)}: "
                f'{stdout.strip()} {stderr.strip()} [rc={rc}]'), None

    loop_device = stdout.strip()
    if not loop_device:
        return f"FAIL: {' '.join(args)}: no loop device returned", None

    # Update container.name to match the loop device (e.g., 'loop0')
    container.name = os.path.basename(loop_device)

    return None, loop_device


####################################################
# LUKS Generic Mounter
####################################################
def mount_luks_container(tray, container, password, upon=None, luks_device=None,
                        readonly=False, luks_file=None, size=None,
                        dot_vault_dir=None, meta_type=''):
    """
    Unified function to mount any LUKS container (device or file).

    Args:
        tray: the running LuksTray (for uid/gid of the bindfs mount)
        container: Container object
        password: LUKS password
        upon: Manual mount point (None for auto-mounting)
        luks_device: Device mapper name (for devices)
        luks_file: Path to LUKS file (for files)
        size: Size for new file creation
        dot_vault_dir: directory new crypt files default to (dialog's
            ``~/.Vaults``); only used when creating a container
        meta_type: 'luks', 'tcrypt', or '' when it is not known yet (a new
            file, or one added before this was recorded). '' falls back to
            `container.meta_type`, which is where the dialog found it. Ignored
            for a file that is being created: that one is always LUKS2. The
            type that actually opened the container is stored back on
            `container.meta_type`, which is how the dialog learns it.
    """
    if not upon:
        # This function reports failure by returning a string (callers do
        # `if err:`), and must keep doing so under `python -O`, where an
        # `assert` would vanish and let a bogus mount point reach `mount`.
        return 'ERR: cannot mount without a mount point'
    try:
        # Both dialogs come through here, so the mount point is created in one
        # place and always as the user (see ensure_dir()). Doing this before
        # the unlock means a failure leaves no open mapper behind.
        err = ensure_dir(upon)
        if err:
            return err

        # Determine if this is a file or device container
        is_file_container = luks_file is not None

        if is_file_container:
            # Handle file container setup
            needs_filesystem = False
            luks_device = os.path.basename(luks_file) + '-luks'
            device_path = luks_file

            # Create file if needed
            if size is not None:
                # this ensures the luks file is creatable by the user,
                # and if so, exists
                luks_file = os.path.abspath(luks_file)
                luks_dirname = os.path.dirname(luks_file)
                err = None
                if luks_dirname == dot_vault_dir:
                    if not os.path.exists(luks_dirname):
                        err = run_cmd(['mkdir', '-p', luks_dirname])
                if not err:
                    err = run_cmd(['touch', luks_file])
                if not err:
                    err = sudo_cmd(['truncate', '-s', f'{size}M', luks_file])
                if not err:
                    # Run luksFormat through sudo like every other
                    # privileged operation: `sudo -n` never prompts and
                    # returning rc/stderr keeps the real failure visible
                    # instead of swallowing it in the outer except.
                    args = ['cryptsetup', 'luksFormat', '--type', 'luks2']
                    args += ['--batch-mode', '--key-file', '-', luks_file]
                    rc, stdout, stderr = sudo_run(args, input_str=f'{password}',
                                                  timeout=FORMAT_TIMEOUT)
                    if rc != 0:
                        err = (f"FAIL: {' '.join(args)}: {stdout} {stderr} "
                               f'[rc={rc}]')
                if err:
                    return err
                needs_filesystem = True
                # `luksFormat --type luks2` just wrote the header, so the file
                # is LUKS whatever an earlier container at this path was.
                meta_type = LUKS
            else:
                # Nothing in the file says which type it is (see
                # ContainerType.py), so fall back to what the history
                # remembered the last time it was opened.
                meta_type = meta_type or getattr(container, 'meta_type', '')
        else:
            # Handle device container setup
            # Only LUKS devices are ever discovered (see DeviceInfo), so there
            # is nothing to ask here.
            meta_type = LUKS
            luks_device = luks_device or f'{container.name}-luks'
            device_path = f'/dev/{container.name}'
            needs_filesystem = False

            # Setup loop device if needed
            if hasattr(container, 'back_file') and container.back_file:
                err, device_path = _setup_loop_device(container)
                if err:
                    return err

        # Manual mounting always: unlock with cryptsetup, then mount manually
        err, meta_type = unlock_container(device_path, password, luks_device,
                                          readonly=readonly, meta_type=meta_type)
        if err:
            return err
        # The dialog copies this into the history entry, so the next attempt
        # asks for the type that worked instead of rediscovering it.
        container.meta_type = meta_type

        mapper_path = f'/dev/mapper/{luks_device}'

        # Create filesystem if needed (for new files)
        if needs_filesystem:
            # mke2fs writes every inode/bitmap up front: minutes on a
            # large container, so it gets the generous deadline.
            err = sudo_cmd(['mkfs.ext4', mapper_path], timeout=FORMAT_TIMEOUT)
            if err:
                return err

        err = _mount_manual(tray, mapper_path, upon,
                do_bindfs=bool(luks_file), readonly=readonly)
        return err

    except Exception as e:                          # pylint: disable=broad-except
        # The dialog only ever shows this one-liner, so the traceback has to
        # reach the log or a non-command failure here is undiagnosable.
        prt(f'ERROR: mount_luks_container: {e}')
        prt(traceback.format_exc())
        return f"An error occurred: {str(e)}"
