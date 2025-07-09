# LUKS Tray

**luks-tray** is a GTK-based tray utility for managing ad hoc LUKS-encrypted files: mount and unmount with ease from your Linux desktop environment.


## Features

- **System tray integration** - Simple click-to-mount/unmount interface for LUKS devices and files
- **Visual status indicators** - Clear icons showing mounted (✅), unmounted (🔳), and open-but-unmounted (‼️) states
- **File container support** - Add, create, and manage LUKS file containers from the tray.
- **Mount point history** - Remembers previous mount locations for convenience and clarity
- **Background monitoring** - Automatically detects newly inserted devices with LUKS containers
- **Password management** - Optional master password to encrypt stored credentials

## Quick Start

1. Install and run `luks-tray`.  Use `pipx` to install, update and remove; e.g. `pipx install luks-tray`. See [pipx docs](https://pypa.github.io/pipx/) for more details.
2. Click the tray icon to see available containers. You may:
    - Insert a disk with LUKS devices to detect them automatically.
    - Add an existing or create a new encrypted file to manage it.
4. Click a container to mount (🔳) or unmount (✅ or ‼️)
5. When mounting a container, enter its password and choose mount point in the dialog
    - if the mount point is empty, then the mount point is automatically chosen.

For best results, the Noto Color Emoji fonts should be installed if not already:

    sudo pacman -S noto-fonts-emoji          # EndeavourOS/Arch
    sudo dnf install google-noto-emoji-fonts # Fedora
    sudo zypper install noto-emoji-fonts     # openSUSE Tumbleweed
    sudo apt install fonts-noto-color-emoji  # Ubuntu


## Visual Interface

The tray icon is shaped like a shield changes based on container states:
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/white-shield-v04.svg" alt="White Shield Icon" width="24" height="24"> - All containers are locked and unmounted (i.e., all data is secure).
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/alert-shield-v04.svg" alt="Alert Shield Icon" width="24" height="24"> - Some containers are unlocked but unmounted (i.e., one or more anomalies).
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/green-shield-v04.svg" alt="Green Shield Icon" width="24" height="24"> - Some containers are mounted w/o any anomalies (i.e., some of the encrypted data is available)

Here is an sample menu as seen when you right-click the tray shield icon:

<img src="https://github.com/joedefen/luks-tray/raw/main/images/sample-menu.png" alt="Sample Menu"></center>

Notes:

- the first section shows LUKS devices, and the second shows LUKS files.
- click a ✅ entry to dismount and lock a mounted, unlocked LUKS container

  - if busy, you are shown the PIDs and names of processes preventing dismount
- click a 🔳 entry to unlock and mount a locked LUKS container
- click a ‼️ entry to lock an unmounted, unlocked container (considered an anomaly)
- or click of the action lines to perform the described action
- LUKS files are only automatically detected in its history; when you add or create new LUKS files, they are added to the history.
- LUKS devices must be created with other tools such as Gnome Disks.



## Configuration

Settings and data files are stored in `~/.config/luks-tray/`:
- **History file** - Encrypted storage of passwords and mount preferences (when master password enabled)
- **Configuration file** - allows configuration of:

    - whether passwords are shown by default.
    - whether ‼️ entries (i.e., anomalies) cause the tray icon to change to the alert shield.

## Security Notes

- Passwords are only stored when master password feature is enabled
- History file is encrypted using the master password
- System mount points are excluded by default to prevent interference with disk encryption
- When creating LUKS file containers, password strength is not enforced; use due care.

---
## Limitations

- **Not for whole disk encryption** - Excludes system mount points like `/`, `/home`, `/var` to avoid interfering with boot-time encrypted volumes
- **No udisks2 integration** - May not always play nicely with desktop auto-mounting tools; so mount and unmount containers with the same tool for the best results.
- **Loop device requirement** - File containers require `lsblk` to show them as loop devices (standard on most distros)
- **Single filesystem focus** - Containers with multiple filesystems are out of scope of this tool and get very limited support (i.e., mostly handling only the first filesystem).
---
## Requirements
#### Additional System Utilities may be Needed
This program requires `cryptsetup`, `fuser`, and other system utilities. After install, run `luks-tray --check-deps` to get a report on what dependencies are found and missing. If any are missing, install those using your distro package manager.

    
#### Passwordless `sudo` Setup (Required)

To allow the tray app to manage LUKS containers without prompting for your password each time, configure passwordless `sudo` for specific commands. For example, in a terminal, run `sudo visudo`, and then add this line at the end of the file (replacing {yourusername} with your actual username):

    {yourusername} ALL=(ALL) NOPASSWD: ALL

💡 This grants your user passwordless access to all commands. You may be able to limit it to just the `luks-tray` app or subset of commands. See `man sudoers` for details.

#### A Working System Tray
It works best with DEs/WMs that offer **first-class tray support**, such as:

  - **KDE Plasma**
  - **Sway** with **waybar**
  - **i3wm** with **polybar**

> ⚠️ **GNOME**: Requires a third-party extension (such as AppIndicator support) to show tray icons. Results may vary across GNOME versions.
> ⚠️ **Xfce** and similar lightweight DEs: Tray menus may open off-screen or be partially cut off, depending on panel layout and screen resolution.



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