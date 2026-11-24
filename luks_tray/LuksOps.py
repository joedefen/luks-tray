#!/usr/bin/env python3
"""Privileged command helpers and LUKS primitives for LUKS TRAY."""
# pylint: disable=broad-exception-caught, invalid-name, too-many-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements

import hashlib
import os
import subprocess
import traceback

from PyQt6.QtWidgets import QMessageBox

from luks_tray.AppInfo import window_title
from luks_tray.ContainerType import CONTAINER_TYPES, LUKS, TCRYPT, salt_identity
from luks_tray.Utils import CMD_TIMEOUT, FORMAT_TIMEOUT, PROBE_TIMEOUT, TIMEOUT_RC, prt


def generate_uuid_for_file_path(file_path):
    """Return a UUID-shaped digest of `file_path`.

    Args:
        file_path: Container file path.

    Returns:
        str: The generated identifier.
    """
    file_hash = hashlib.sha256(file_path.encode('utf-8')).hexdigest()
    generated_uuid = f"{file_hash[:8]}-{file_hash[8:12]}-{file_hash[12:16]}-{file_hash[16:20]}-{file_hash[20:32]}"

    return generated_uuid

def get_luks_uuid_for_file(file_path):
    """Return the LUKS UUID inside `file_path`, or `''`.

    Args:
        file_path: Container file path.

    Returns:
        str: The UUID reported by `blkid`.
    """
    if not file_path:
        return ''
    rc, stdout, _stderr = run_cmd_capture(
            ['blkid', '-o', 'value', '-s', 'UUID', file_path])
    return stdout.strip() if rc == 0 else ''

def get_container_identity(file_path):
    """Return the history key for the container stored in `file_path`.

    Args:
        file_path: Container file path.

    Returns:
        str: A LUKS UUID, a salt identity, or a path-derived identifier.
    """
    if not file_path:
        return ''
    return (get_luks_uuid_for_file(file_path)
            or salt_identity(file_path)
            or generate_uuid_for_file_path(file_path))

def sudo_cmd(args, errs=None, input_str=None, timeout=None):
    """Run `args` through `sudo -n`, which never prompts.

    Args:
        args: Command and arguments.
        errs: Optional list that receives the failure text.
        input_str: Text passed to the command's stdin.
        timeout: Deadline in seconds; defaults to `CMD_TIMEOUT`.

    Returns:
        str | None: The failure text, or None on success.
    """
    args = ['sudo', '-n'] + args
    return run_cmd(args, errs, input_str, timeout)

def run_cmd(args, errs=None, input_str=None, timeout=None):
    """Run `args` and return None on success, or a `'FAIL: ...'` string.

    Args:
        args: Command and arguments.
        errs: Optional list that receives the failure text.
        input_str: Text passed to the command's stdin.
        timeout: Deadline in seconds; defaults to `CMD_TIMEOUT`.

    Returns:
        str | None: The failure text, or None on success.
    """
    rc, stdout, stderr = run_cmd_capture(args, input_str, timeout)
    if rc == 0:
        return None
    err = f"FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]"
    if errs is not None:
        errs.append(err)
    return err

def _as_text(value):
    """Return `value` as text, whatever `TimeoutExpired` stored."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode(errors='replace')
    return value

def run_cmd_capture(args, input_str=None, timeout=None):
    """Run `args` and return `(returncode, stdout, stderr)`.

    Args:
        args: Command and arguments.
        input_str: Text passed to the command's stdin.
        timeout: Deadline in seconds; defaults to `CMD_TIMEOUT`.

    Returns:
        tuple: Return code, standard output and standard error.
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
    """Create `path` as the user, falling back to `sudo` for system paths.

    Args:
        path: Directory to create.

    Returns:
        str | None: The failure text, or None on success.
    """
    if os.path.isdir(path):
        return None
    try:
        # The mount point is owner-only on purpose: the umask a desktop hands us
        # must not decide who can see (or create entries in) the mount area.
        os.makedirs(path, mode=0o700, exist_ok=True)
        return None
    except PermissionError:
        return sudo_cmd(['mkdir', '-p', '-m', '0700', path])
    except OSError as exc:
        # Existing non-directory, read-only parent, bad path component, ...
        return f'FAIL: cannot create {path!r}: {exc}'


def run_unmount(mount_point: str, busy_warns: set) -> str | None:
    """Unmount `mount_point`, showing busy processes when needed.

    Args:
        mount_point: Path to unmount.
        busy_warns: Mount points already reported as busy.

    Returns:
        str | None: The failure text, or None on success.
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
        fuser = subprocess.run(  # noqa: UP022
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

    # QMessageBox has `setWindowTitle`, not `set_title`.
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setWindowTitle(window_title('Unmount Failed — Device Busy'))
    msg.setText(f"'{mount_point}' busy by these processes:\n - {info}")
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()

    return err


def _open_argv(device_path, luks_device, readonly, meta_type):
    """Return the `cryptsetup open` argv for one declared header type."""
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
    """Run one `cryptsetup open` attempt for a declared header type."""
    return sudo_cmd(_open_argv(device_path, luks_device, readonly, meta_type),
                    input_str=password)


def unlock_container(device_path, password, luks_device, readonly=False,
                     meta_type=''):
    """Open a container, trying the declared header type first.

    Args:
        device_path: Device or file to open.
        password: Container password.
        luks_device: Mapper name.
        readonly: Open read-only.
        meta_type: Declared type, `'luks'`, `'tcrypt'` or `''`.

    Returns:
        tuple: `(error, used_type)`.
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
    """Mount `mapper_path` at `upon`, optionally through bindfs."""
    if readonly:
        err = sudo_cmd(['mount', '-o', 'ro', mapper_path, upon])
    else:
        err = sudo_cmd(['mount', mapper_path, upon])
    if do_bindfs and not err:
        err = sudo_cmd(['bindfs', '-u', str(tray.uid), '-g', str(tray.gid),
                      upon, upon])
    return err

def _setup_loop_device(container):
    """Attach `container.back_file` to a loop device.

    Returns:
        tuple: `(error, loop device path)`.
    """
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


def mount_luks_container(tray, container, password, upon=None, luks_device=None,
                        readonly=False, luks_file=None, size=None,
                        dot_vault_dir=None, meta_type=''):
    """Mount any LUKS container, device or file.

    Args:
        tray: Running tray, for uid/gid and mount state.
        container: Container record.
        password: Container password.
        upon: Manual mount point; None to auto-mount.
        luks_device: Mapper name for devices.
        readonly: Mount read-only.
        luks_file: Container file path for file containers.
        size: Size in MiB when creating a new file.
        dot_vault_dir: Directory new crypt files default to.
        meta_type: Declared header type, or `''` when unknown.

    Returns:
        str | None: The error text, or None on success.
    """
    if not upon:
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
