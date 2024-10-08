> **THIS IS A WORK IN PROGRESS.  BE PATIENT.**

# luks-tray
System tray applet to help mount/unmount LUKS containers (on Linux)

## Design
- regularly updates its menu with a list of crypto_LUKS partitions (known by UUID and current device name)
- it will presume that any given LUKS partition has a fixed, unique mount point (two can share, say, /mnt but only one can be mounted there)
- when shown, it will be shown as the device name (e.g., sda1) + basename of mount point (if known)
- it will show the state: "mounted and unlocked" or "unmount and locked"
  - Locked: use 🔒 (U+1F512)
  - Mounted and Unlocked: use   📂 (U+1F4C2) + 🔓 (U+1F513).
  - Mounted and Unlocked but trying to dismount: use   ⚠️ (U+26A0)  + 🔓 (U+1F513).
  - Unmounted and Unlocked: use 🔓 (U+1F513).
  - Uncertain: use ❓ (U+2753) 
  - OR Open Book (📖 U+1F4D6) and Closed Book (📕 U+1F4D5) and Trophy (🏆 U+1F3C6) for mounted
- each time the menu is opened, the state is re-establish;  and that is done periodically in the background
- a json file will be updated when anything persistent is changed.   it will have entries with:
  - UUID
  - mount point (if known)
  - preferred delay minutes (before starting auto-unmount attempts)
  - preferred repeat minutes (to retry auto-unmounts)
- if a known or unknown UUID discovered, then it will be added
- if a UUID has a mount point which is different than persisted, then it is updated
- when a unmounted/unlocked partition is clicked, there will be a pop-up dialog showing:
  - mount point (pre-filled in but changeable)
  - password (pre-filled if known)
  - delay minutes for auto-unmount
  - repeat minutes for auto-unmount
  - OK + CANCEL buttons
- there are persistent options in a .ini file set from the menu:
  - "mask password" (True or False) ... a menu item toggles the value
  - "retain passwords in session" another menu item persistent option (that is only shown if no master password).
- there are .ini file options:
  - for "unlocked and unmounted" a repeat interval in minutes to automatically unlock; if 0, then n/a
- in the case that the app opens and finds an opened container mounted at (/,/home,/var,/var/log,/swap,/tmp,/svr,/opt,/usr) by default, then the container will be "unlisted" [to cover simple and complex full disk encryption cases where you don't want to alert "exposed" data, etc, because is always present].   This list will be in the .ini file.
- for handling files, there will be a section in the list for "registered" file containers and a menu item to add one. If registered and not present, it is not shown but not unregistered. File handling requires that the distro shows mounted file containers as "loop" devices by lsblk.
- the "mount container" dialog will have a "Hide" button in addition to OK and cancel.  The hide will unregister a file container and remove it from the "Registered" section of .ini file and for partitions, will put the UUID in a "Hidden" section.  To take a partition out of the "Hidden" section, then you have to edit the .ini and know the UUID.  If opened, unregistered and hidden items are shown in the menu, affect the icon state, and can be dismounted/closed.
- Edge cases (with caveats in the README):
  - no filesystems in a LUKS container
  - multiple filesystem in a LUKS container ... pretend there is just one
  - have a lazy, soft whether open state if no child filesystems so ... it checked before an unlock/mount dialog
- "master password" for the app that used to encrypt a password/json file with uuid and known passwords.
  - It would be optional ... so click a menu item that says "set master password" (if no master password) or
  - "change master password" that allows changing it ... each with the appropriate dialog.
- ICON:  a shield with these variations:
  - yellow with with big red exclamation inside - some are in dismounting in progress or unlocked but unmounted
  - yellow - some mounted (info exposed) w/o any being dismounted or unlocked but unmounted
  - green - none mounted (no info exposed)

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

"Full disk encryption" is usually just / (but might also include /home, /var, /var/log, /swap/, /tmp/, /svr, /opt, /usr, /var/log)

