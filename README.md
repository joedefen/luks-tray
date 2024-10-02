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
- there are persistent options in a .ini file:
  - "mask password" (True or False) ... a menu item toggles the value
  - "retain passwords in session" another menu item persistent option (that is only shown if no master password).
- "master password" for the app that used to encrypt a password/json file with uuid and known passwords.
  - It would be optional ... so click a menu item that says "set master password" (if no master password) or
  - "change master password" that allows changing it ... each with the appropriate dialog.
- ICON:  a shield with four variations:
  - yellow - all mounted (info exposed)
  - orange - some mounted (some info exposed)
  - green - none mounted (not info exposed)
  - white - no LUKS partitions present
