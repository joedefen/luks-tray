> **THIS IS A WORK IN PROGRESS.  BE PATIENT.**

# LUKS Tray

A system tray applet for Linux that simplifies mounting and unmounting LUKS-encrypted containers (partitions and files).

> *This tool is designed for users who regularly work with ad hoc, encrypted LUKS containers, and it does not not full disk encryption management.*


## Features

- **System tray integration** - Simple click-to-mount/unmount interface
- **Visual status indicators** - Clear icons showing mounted (✅), unmounted (🔳), and open-but-unmounted (‼️) states
- **Password management** - Optional master password to encrypt stored credentials
- **Auto-unmount** - Configurable automatic unmounting
- **File container support** - Mount encrypted files as loop devices
- **Mount point history** - Remembers previous mount locations for convenience
- **Background monitoring** - Automatically detects newly inserted devices with LUKS containers

## Quick Start

1. Install and run `luks-tray`
2. Click the tray icon to see available containers. You may:
    - Insert a disk with LUKS devices to detect them automatically.
    - Add an existing or create a new encrypted file to manage it.
4. Click a container to mount (🔳) or unmount (✅ or ‼️)
5. When mounting a container, enter its password and choose mount point in the dialog
    - if the mount point is empty, then the mount point is automatically chosen.

## Visual Interface

The tray icon shaped like a shield changes based on container states:
- <img src="src/luks_tray/resources/white-shield-v04.svg" alt="White Shield Icon" width="24" height="24"> - All containers are locked and unmounted (i.e., all data is secure).
- <img src="src/luks_tray/resources/alert-shield-v04.svg" alt="Alert Shield Icon" width="24" height="24"> - Some containers are unlocked but unmounted (i.e., one or more anomalies).
- <img src="src/luks_tray/resources/green-shield-v04.svg" alt="Green Shield Icon" width="24" height="24"> - Some containers are mounted w/o any anomalies (i.e., some of the encrypted data is available)

Here is an sample menu:
<img src="images/sample-menu.png" alt="Sample Menu">
Notes:
- the first section shows LUKS devices, and the second shows LUKS files.
- click a ✅ entry to dismount and lock a mounted,
-  unlocked LUKS container
- click a 🔳 entry to mount a locked LUKS container
- click a ‼️ entry to lock an unmounted, unlocked container (considered an anomaly)
- or click of the action lines to perform the described action
- LUKS files are only automatically detected in its history; when you add or create new LUKS files, they are added to the history.
- LUKS devices must be created with other tools such as Gnome Disks.


## Limitations

- **Not for whole disk encryption** - Excludes system mount points like `/`, `/home`, `/var` to avoid interfering with boot-time encrypted volumes
- **No udisks2 integration** - May not always play nicely with desktop auto-mounting tools; so mount and unmount containers with the same tool.
- **Loop device requirement** - File containers require `lsblk` to show them as loop devices (standard on most distros)
- **Single filesystem focus** - Containers with multiple filesystems are out of scope of this tool and get very limited support (i.e., mostly handling only the first filesystem).

## Configuration

Settings are stored in `~/.config/luks-tray/`:
- **History file** - Encrypted storage of passwords and mount preferences (when master password enabled)
- **Configuration file** - allows configuration of:

    - whether passwords are shown by default.
    - whether ‼️ entries (i.e., anomalies) cause the tray icon to change to the alert shield.

## Security Notes

- Passwords are only stored when master password feature is enabled
- History file is encrypted using the master password
- System mount points are excluded by default to prevent interference with disk encryption

## Requirements

- Linux with LUKS/cryptsetup support
- PyQt6
- Standard utilities: `lsblk`, `cryptsetup`, `mount`, `umount`

---

# DESIGN/TEST NOTE (THIS SECTION IS TEMPORARY)

## Immediate TODOs
- updating the master password to '' should clear it
- remove the clear master password line item

## Design
- regularly updates its menu with a list of crypto_LUKS containers (known by UUID and current device name)
- each entry will be show as the device name (e.g., "sda1") + basename of current or previous mount point (if any)
  - whether mounted (shows ■ for mounted, □ for not mounted, ❢ for trying to dismount  TODO)
  - whether open/unlocked or closed/locked (🗹 for open, — for closed)
- each time the menu is opened, the state is re-establish TODO;  and that is done periodically in the background
- optionally, the user can enabled a "master password" using the "Set Master Password" menu item.
  - w/o a master password, adding one is a menu option
  - with a master password and locked, the only menu item is "Enter master password"
- a "history" file (json) will be updated with retained info. It will have entries with:
  - UUID
  - last successful password (only if "master password feature enabled TODO)
  - previous mount point (if known)
  - preferred delay minutes (before starting auto-unmount attempts)
  - preferred repeat minutes (to retry auto-unmounts)
  - the history file is encrpyted using the master password if the master password features is enabled TODO
- when a unmounted/unlocked container is clicked, there will be a pop-up dialog showing:
  - password (pre-filled if known and obscured ) ONLY shown when closed/locked TODO
  - mount point (pre-filled if retained and otherwise empty)
  - delay minutes for auto-unmount
  - repeat minutes for auto-unmount
  - info about the container if known (device or file name, size, label, ..)
  - OK + CANCEL buttons
- there are persistent options in a .ini file set from the menu:
  - "mask password" (True or False) ... a menu item toggles the value ... applies only when entering password (retained passwords are always hidden) TODO
  - default delay minutes for auto-lock (0 means none, default=60) TODO
  - default repeat minutes for auto-lock (0 means none, default=60) TODO
  - a list of mount points that hides containers (mostly for full disk encryption). Default is (/,/home,/var,/var/log,/swap) TODO
  - a section which is a list of hidden file containers TODO
- handling of files that are luks containers
  - there will be a section in the menu for "registered" file containers and a menu item
    ("Add Crypt File") to add one.
  - Registered file containers are recorded the history file.
  - If registered and not present, it is not shown but not unregistered.
  - NOTE: file handling requires that the distro shows mounted file containers as "loop" devices by lsblk.
- Edge cases (with caveats in the README):
  - no filesystems in a LUKS container
  - multiple filesystem in a LUKS container ... pretend there is just one
  - have a lazy, soft whether open state if no child filesystems so ... it checked before an unlock/mount dialog
- ICON:  a shield with these variations:
  - yellow with with big red exclamation inside - some are in dismounting in progress or unlocked but unmounted
  - yellow - some mounted (info exposed) w/o any being dismounted or unlocked but unmounted
  - green - none mounted (no info exposed)
  
- Your tool can definitely be smart enough to detect and work with udisks2-managed LUKS containers.
  - Here's what you can detect and handle:
    - Parse /proc/mounts - This shows all mounted filesystems regardless of how they were mounted.
      You can identify LUKS volumes by their /dev/mapper/ entries and see where they're actually
      mounted (including udisks2's /run/media/username/ paths).
    - Check /dev/mapper/ - List all active device mapper entries. LUKS containers appear here
      whether opened by cryptsetup or udisks2.
    - Query udisks2 directly - You can call udisksctl info -b /dev/sdX or use D-Bus to check if
      a device is managed by udisks2 and get its status.
    - Cross-reference with your tracking - Compare what's actually on the system vs.
      what your tool thinks it has managed.
  - What you can handle:

    - Detect udisks2 mounts - Even if mounted to /run/media/username/volume-name instead of your preferred location
    - Unmount udisks2 volumes - Use udisksctl unmount -b /dev/mapper/luks-uuid or regular umount
    - Close udisks2 containers - Use udisksctl lock -b /dev/sdX or cryptsetup luksClose
    - Update your state tracking - Incorporate externally-managed volumes into your three-state model

  - Smart behavior:
    - Your tool could show these "foreign" LUKS volumes with a different icon or indicator
      (like "managed externally") and still allow you to unmount/close them.
    - This gives you a unified view of all LUKS activity on your system while preserving
      your preferred workflow for volumes you manage directly.

---
Test Notes:
  - for no filesystems:
      sudo dd if=/dev/zero of=/tmp/test_luks_container bs=1M count=100
      sudo cryptsetup luksFormat /tmp/test_luks_container
      sudo cryptsetup open /tmp/test_luks_container test_luks
  - for 2 file systems:
      sudo pvcreate /dev/mapper/test_luks
      sudo vgcreate test_vg /dev/mapper/test_luks
      sudo lvcreate -L 20M -n lv1 test_vg
      sudo lvcreate -L 20M -n lv2 test_vg
      sudo mkfs.ext4 /dev/test_vg/lv1
      sudo mkfs.ext4 /dev/test_vg/lv2
  - "Full disk encryption" is usually just / (but might also include /home,
    /var, /var/log, /swap/, /tmp/, /svr, /opt, /usr, /var/log)

---
Discarded Features
- the "mount container" dialog will have a "Hide" button in addition to OK and cancel. TODO  The hide will unregister a file container and remove it from the "Registered" section of .ini file and for partitions, will put the UUID in a "Hidden" section of the .ini.  To take a partition out of the "Hidden" section, then you have to edit the .ini and know the UUID.  If opened, unregistered and hidden items are shown in the menu, affect the icon state, and can be dismounted/closed.