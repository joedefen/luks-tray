# LUKS Tray

**luks-tray** is a QT-based tray utility for managing ad hoc LUKS-encrypted files: mount and unmount with ease from your Linux desktop environment.

## Features

- **System tray integration** - Simple click-to-mount/unmount interface for LUKS devices and files
- **Visual status indicators** - Symbols mounted (▣), mounted read-only (⧈), unmounted (▽), and open-but-unmounted (‼) states
- **File container support** - Add, create, and manage LUKS file containers from the tray.
- **Mount point history** - Remembers previous mount locations for convenience and clarity
- **Background monitoring** - Automatically detects newly inserted devices with LUKS containers
- **Password management** - Optional master password to encrypt stored credentials
- **Single instance** - A second copy refuses to start (exit status 3) instead of racing the running one; `--replace` terminates the holder and takes over.
- **Restart from the menu** - The bottom menu entry reads `↺ Restart luks-tray v<version>`, so the running build is visible without leaving the menu.
- **Late or restarting panels** - If the bar starts after the applet, or restarts later, the tray icon registers itself with it instead of staying invisible until the applet is restarted.

## Quick Start

1. Install `luks-tray`  using `pipx`; e.g. `pipx install luks-tray`. See [pipx docs](https://pypa.github.io/pipx/).
2. Setup passwordless `sudo` if not already (see notes below if needed).
3. From a terminal:
    - run `luks-tray --check-deps` and install any required commands.
    - run `luks-tray` to ensure it finds your system tray and appears.
4. Right-click the tray icon to see available containers. You may:
    - Insert a disk with LUKS devices to detect them automatically.
    - Add an existing or create a new encrypted file to manage it.
5. Left-click a ▽ container to mount it; or a ▣ or ⧈ or ‼ container to unmount it.
6. When mounting a container, enter its password and choose mount point in the dialog
    - if the app has a mount point in its history, it will fill in that mount point
    - otherwise, the app will generate a mount point in `~/Vaults`
7. Finally, for convenience when working, add `luks-tray` to your "auto-start" apps.

Only one copy can run at a time: a second launch reports the running instance and exits with status 3. Use `luks-tray --replace` to terminate it and take over, or the `↺ Restart luks-tray` entry at the bottom of the menu to restart it in place.

## Visual Interface

The tray icon is shaped like a shield changes based on container states:
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/white-shield-v04.svg" alt="White Shield Icon" width="24" height="24"> - All containers are locked and unmounted (i.e., all data is secure).
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/alert-shield-v04.svg" alt="Alert Shield Icon" width="24" height="24"> - Some containers are unlocked but unmounted (i.e., one or more anomalies).
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/green-shield-v04.svg" alt="Green Shield Icon" width="24" height="24"> - Some containers are mounted w/o any anomalies (i.e., some of the encrypted data is available)

Here is an sample menu as seen when you right-click the tray shield icon:

<img src="https://github.com/joedefen/luks-tray/raw/main/images/sample-menu.png" alt="Sample Menu"></center>

Notes:

- the first section shows LUKS devices, and the second shows LUKS files.
- click a ▣ or ⧈ entry to dismount and lock a mounted, unlocked LUKS container

  - note that ▣ indicates full access; ⧈ indicates readonly
  - if busy, you are shown the PIDs and names of processes preventing dismount

- click a ▽ entry to unlock and mount a locked LUKS container
- click a ‼ entry to lock an unmounted, unlocked container (considered an anomaly)
- or click of the action lines to perform the described action
- LUKS devices must be created with other tools such as Gnome Disks.
- LUKS files are only automatically detected in its history; when you add or create new LUKS files, they are added to the history.
- When creating LUKS files, the default folder is `~/.Vaults`.
- When mounting LUKS files for the first time, the default folder is `~/Vaults/{basename}` where `{basename}` is the basename of the encrypted LUKS file (less any `.luks`, `.luks2`, or `.crypt` suffix).


## Configuration

Settings and data files are stored in `~/.config/luks-tray/`:
- **History file** - Stored of passwords and mount preferences; it is encrypted when the master password enabled.
- **Lock file** - `luks-tray.lock` holds the PID of the running instance; the kernel releases the lock when that process exits, so a crash never blocks the next start.
- **INI file** - the default .ini file looks like:

      [ui]
      show_passwords_by_default = True
      show_anomaly_alerts = True
      auto_mount_folder = ~/Vaults

  You can thus change
    - whether passwords are shown by default when being first entered.
    - whether ‼️ entries (i.e., anomalies) cause the tray icon to change to the alert shield.
    - where the automatically generated mount points live

## Security Notes

- Passwords are only stored when master password feature is enabled
- History file is encrypted using the master password
- System mount points are excluded by default to prevent interference with disk encryption
- When creating LUKS file containers, password strength is not enforced; use due care.

---
## Limitations

- **Not for whole disk encryption** - Designed for fixed and removable partition and file containers. Thus, excludes system mount points like `/`, `/home`, `/var` to avoid interfering with boot-time encrypted volumes.
- **No udisks2 integration** - Manages its own mount/unmount state. Mixing mounts/unmounts desktop auto-mounting tools may not produce the best results.
- **Loop device requirement** - File containers require `lsblk` to show them as loop devices (standard on most distros)
- **Single filesystem focus** - Containers with multiple filesystems are out of scope of this tool and get very limited support (i.e., mostly handling only the first filesystem).
---
## Requirements
#### Additional System Utilities may be Needed
This program requires `cryptsetup`, `fuser`, and other system utilities. After install, run `luks-tray --check-deps` to get a report on what dependencies are found and missing. If any are missing, install those using your distro package manager.
    
#### Passwordless `sudo` Setup (Required)

The tray runs its privileged steps as `sudo -n …`, which never prompts. Grant passwordless access to the commands it actually needs -- not to `ALL` -- by running `sudo visudo` and adding (replacing `{yourusername}` with your actual username):

```
Cmnd_Alias LUKS_TRAY = /usr/bin/cryptsetup, /usr/bin/mount, /usr/bin/umount, \
                       /usr/bin/losetup, /usr/bin/fuser, \
                       /usr/bin/mkdir, /usr/bin/rmdir, /usr/bin/chown, \
                       /usr/bin/truncate, /usr/bin/mkfs.ext4, /usr/bin/bindfs

{yourusername} ALL=(ALL) NOPASSWD: LUKS_TRAY
```

💡 `sudo` matches the *resolved* command path, so confirm each entry on your distro with `command -v cryptsetup mount umount losetup fuser mkdir rmdir chown truncate mkfs.ext4 bindfs`, and correct any that live under `/usr/sbin` (common for `mkfs.ext4`). The rest of the `luks-tray --check-deps` list -- `lsblk`, `blkid`, `touch` -- runs unprivileged and needs no rule; `sudo` itself must of course be installed. See `man sudoers` for details.

#### A Working System Tray
It works best with DEs/WMs that offer **first-class tray support**, such as:

  - **KDE Plasma** (X11 or Wayland)
  - **i3wm** with **polybar**
  - **sway** with **waybar** (actually, quite 2nd class ... see "Workarounds for SWAY" below)

> ⚠️ **GNOME**: Requires a third-party extension (such as AppIndicator support) to show tray icons. Results may vary across GNOME versions.<br>
> ⚠️ **Xfce** and similar lightweight DEs: Tray menus may open off-screen or be partially cut off, depending on panel layout and screen resolution.


####  Workarounds for SWAY
Required Workarounds for ~/.config/sway/config:
* Environment variables: `sway` must be in `$XDG_CURRENT_DESKTOP` or `$DESKTOP_SESSION`
 
* The user must deal with missing titlebars for moving/closing.
* It is best to add configuration rules to ensure dialogs float:
```
  for_window title "luks-tray.%" floating enable, center, border normal, no_focus
```

---

Test Notes:
  - for no filesystems:
    - sudo dd if=/dev/zero of=/tmp/test_luks_container bs=1M count=100
    - sudo cryptsetup luksFormat /tmp/test_luks_container
    - sudo cryptsetup open /tmp/test_luks_container test_luks
  - for two file systems:
    - sudo pvcreate /dev/mapper/test_luks
    - sudo vgcreate test_vg /dev/mapper/test_luks
    - sudo lvcreate -L 20M -n lv1 test_vg
    - sudo lvcreate -L 20M -n lv2 test_vg
    - sudo mkfs.ext4 /dev/test_vg/lv1
    - sudo mkfs.ext4 /dev/test_vg/lv2

---

## Development

Run from the source tree without installing: `./runner`.

To install the development toolchain (ruff, pytest, pytest-cov, build, twine, packaging) alongside the runtime dependencies:

```
python3 -m venv lt-venv
lt-venv/bin/python -m pip install -e '.[dev]'
```

The gate is three commands — `lt-venv/bin/python -m ruff check .`,
`lt-venv/bin/python -m compileall -q luks_tray conftest.py tests`, and
`lt-venv/bin/python -m pytest -q --cov=luks_tray --cov-report=term-missing` —
and the only path to an upload is `lt-venv/bin/python ./bless`, which runs them
all and then asks whether to rehearse on TestPyPI, publish, or stop.

The version lives in one place, `luks_tray/__init__.py`; do not add a second
copy to `pyproject.toml`.

Release procedure, the `bless` stages, credentials and the CI pipeline are
documented in [tests/README-DEV.md](tests/README-DEV.md).  Known defects and the
items still open are in [tests/AUDIT.md](tests/AUDIT.md).