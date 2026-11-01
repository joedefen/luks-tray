# LUKS Tray

**luks-tray** is a QT-based tray utility for managing ad hoc LUKS-encrypted files: mount and unmount with ease from your Linux desktop environment. Legacy TrueCrypt/VeraCrypt *file* containers can be added and managed the same way -- see [Limitations](#limitations) for exactly how far that goes.

Version 3.0.0 (v3) added and enhanced features and improved quality.

## Features

- **System tray integration** - Simple click-to-mount/unmount interface for LUKS devices and files
- **Visual status indicators** - Symbols mounted (▣), mounted read-only (⧈), unmounted (▽), and open-but-unmounted (‼) states
- **Options dialog** - `⚙ Options…` edits the settings below and reaches the master password; the fixed entries carry a symbol too (`✦` create, `✚` add, `⚙` options, `☓` exit, `↺` restart) [**added in v3**]
- **File container support** - Add, create, and manage LUKS file containers from the tray.
- **TrueCrypt/VeraCrypt file containers** - `✚ Add Existing Crypt File` also opens a legacy TrueCrypt or VeraCrypt container. Which header it has is worked out from `cryptsetup`'s answer rather than asked for, the history keys such an entry by its header salt (there is no readable UUID), and the next unlock asks for the type that worked [**added in v3**]
- **Mount point history** - Remembers previous mount locations for convenience and clarity
- **Background monitoring** - Automatically detects newly inserted devices with LUKS containers
- **Password management** - Optional master password to encrypt stored credentials
- **Single instance** - A second copy refuses to start (exit status 3) instead of racing the running one; `--replace` terminates the holder and takes over [**added in v3**]
- **Restart from the menu** - The bottom menu entry reads `↺ Restart luks-tray v<version>`, so the running build is visible without leaving the menu [**added in v3**]
- **A log that explains itself** - `≡ Open Log` pages `debug.log` live, and the entry becomes `≡ Open Log [n errors]` once *this session* has reported a failure, so a dialog that says what failed is one click away from the log that says why. The log starts with a `=== luks-tray v<version> started, pid <pid> ===` line and ends with the matching one [**added in v3**]
- **Late or restarting panels** - If the bar starts after the applet, or restarts later, the tray icon registers itself with it instead of staying invisible until the applet is restarted [**improved in v3**]
- **Wayland dialog support** - On niri, sway and the other tiling compositors a dialog is a client, so the compositor decides its size and placement. Every dialog therefore carries one stable `app-id` (`luks-tray`), which a single window rule can match to make the dialogs float, and `--show-window-rules` prints the rule for the current session to paste into the compositor's config. Because sway hides title bars, each dialog also repeats its purpose as a heading inside the dialog itself, so nothing depends on the title bar being visible. See [Wayland tiling compositors](#wayland-tiling-compositors-niri-sway) [**improved in v3**]
- **Start at login** - The `--svc-*` verbs write, render, enable and manage a `systemd --user` unit for the session. See [Starting at Login](#starting-at-login) [**added in v3**]

## Competitive Analysis (as of November 2026)

This tool exists because of a specific mismatch, and the date in this heading is
there because competitor GUIs change. The VeraCrypt GUI was the model: a list of
containers *you* own, each with a mount point *you* chose, all of it one click
from the tray. The Linux options at the time were either whole-disk tools that
happened to accept a file, or GUIs that covered only one container type and
remembered nothing -- so keeping a handful of file containers in rotation meant
re-entering a passphrase and a path every time, and no single place ever showed
what was open.

The use-case this applet is written for: **many ad hoc encrypted file
containers, mounted and unmounted often, on a desktop or tiling WM that gives
per-container state nowhere else.** Judged against that, the alternatives fall
into four groups. This is not a benchmark; it is what the author compared, and
what each group still can and cannot do. Corrections are welcome as issues.

| Group | Examples | Strong at | Stops where it matters here |
| --- | --- | --- | --- |
| **Desktop file managers and auto-mount** | GNOME Files (Nautilus), Dolphin, Thunar, PCManFM, GNOME Disks -- all on udisks2 | Everything a desktop is expected to do: removable media, whole disks, system partitions; a LUKS partition is usually one click and one passphrase prompt, and some will loop-mount a LUKS image too | The passphrase is prompted per mount, never remembered for an ad-hoc container; the mount point is udisks2's choice under `/media/…` or `/run/media/…` and is not remembered per container; a container that is *opened but not mounted* looks like nothing at all; and TrueCrypt/VeraCrypt file containers are outside the model entirely. It is also a file manager, not a posture indicator: you learn the state of your vaults by browsing |
| **VeraCrypt's own GUI** | VeraCrypt (TrueCrypt's successor; TrueCrypt mode for old volumes) | The closest thing to this tool's workflow, and its direct inspiration: favourites that pin a mount point, a tray presence, container creation | VeraCrypt volumes (and TrueCrypt ones in TrueCrypt mode) only -- a LUKS container cannot be opened at all, as far as this author knows. It also carries no master password of its own, so it can remember *where* a container mounts but not *how to open* it, and it is not packaged by most distros, so it arrives out of band |
| **`cryptsetup` front-ends** | zuluCrypt/zuluMount, cryptmount, plain `cryptsetup` in a terminal | zuluCrypt is the closest open-source match on format coverage: LUKS, TrueCrypt and VeraCrypt in one GUI, with a volume list | No master password over the stored entries, no per-container mount-point history in the form this tool uses, and no overall posture in the tray. The CLI tools it wraps are excellent and fully scriptable -- which is exactly why this applet calls them, rather than replacing them |
| **Transparent folder encryption** | Plasma Vault, GNOME Vaults, Cryptomator (gocryptfs/cryfs underneath) | A folder that is simply *there* once unlocked, with no mount step to forget | A different problem: these encrypt a directory in place, usually through FUSE, and are not a single container file you can copy, move, hand to another machine, or open with plain `cryptsetup` elsewhere. For "my documents directory is encrypted" they are the better answer |

What is left, and why this tool is not redundant:

- **A master password over the history.** The history file stores passphrases
  *and* mount preferences under one master password, entered once per session
  (or not stored at all, if you set none). A file manager never stores a
  container passphrase and VeraCrypt's GUI stores no secret at all, so this is
  the feature that most distinguishes the tool -- and it is deliberately a
  *single* file you can delete to revoke everything.
- **Mount points are pinned and remembered.** Each container carries its last
  mount point, the default root is a single configurable, validated
  `auto_mount_folder` (`~/Vaults`), and both are visible in the dialog before
  the mount happens. VeraCrypt favourites do the same for VeraCrypt files; no
  file manager does it for a LUKS container.
- **State, not just availability.** The shield colour is a posture: locked,
  mounted, read-only, and the `‼` case of *opened but not mounted* -- a volume
  whose passphrase succeeded and whose filesystem did not get mounted, which is
  both leaked and lost. A file manager has no place to report that.
- **One list, one set of verbs.** A discovered LUKS partition, a LUKS file and a
  legacy TrueCrypt/VeraCrypt file appear side by side and are mounted,
  unmounted and inspected by the same two clicks. zuluCrypt is the only other
  entry above that spans those formats.
- **No daemon and no root GUI.** It is a tray process plus narrowly scoped
  `sudo` rules (see [Requirements](#requirements)); the privileged work is the
  same `cryptsetup`/`mount`/`umount` a shell would run, with no udisks2 policy
  in between and no privilege boundary to learn.

Where a different tool is the right answer: whole-disk and `/home` encryption
(the file managers, `systemd-cryptsetup`, or your distro's installer); hidden or
system VeraCrypt volumes, or a non-default PIM (VeraCrypt itself -- see
[Limitations](#limitations)); a directory that should simply be encrypted all
the time (the folder-encryption tools); and anything scripted or headless
(`cryptsetup` directly).
This is also a one-author project without a security audit or distro packages,
which is a real difference from every group above -- judge it accordingly.

## Quick Start

1. Install `luks-tray`  using `pipx`; e.g. `pipx install luks-tray`. See [pipx docs](https://pypa.github.io/pipx/).
2. Setup passwordless `sudo` if not already (see notes below if needed).
3. From a terminal:
    - run `luks-tray --check-deps` and install any required commands.
    - run `luks-tray` to ensure it finds your system tray and appears.
4. Right-click the tray icon to see available containers. You may:
    - Insert a disk with LUKS devices to detect them automatically.
    - Add an existing or create a new encrypted file to manage it.
    - Open `⚙ Options…` to change the settings below, or to set/change/remove a master password.
5. Left-click a ▽ container to mount it; or a ▣ or ⧈ or ‼ container to unmount it.
6. When mounting a container, enter its password and choose mount point in the dialog
    - if the app has a mount point in its history, it will fill in that mount point
    - otherwise, the app will generate a mount point in `~/Vaults`
7. Finally, start it automatically at login -- `luks-tray --svc-install` on a
   systemd system, or your DE's autostart entry.  See [Starting at Login](#starting-at-login).

Only one copy can run at a time: a second launch reports the running instance and exits with status 3. Use `luks-tray --replace` to terminate it and take over, or the `↺ Restart luks-tray` entry at the bottom of the menu to restart it in place.

## Visual Interface

The tray icon is a shield, and its colour reports the state of the containers:
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/white-shield-v04.svg" alt="White Shield Icon" width="24" height="24"> - All containers are locked and unmounted (i.e., all data is secure). This is only shown when the applet can actually see the containers.
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/alert-shield-v04.svg" alt="Alert Shield Icon" width="24" height="24"> - Some containers are unlocked but unmounted (i.e., one or more anomalies).
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/green-shield-v04.svg" alt="Green Shield Icon" width="24" height="24"> - Some containers are mounted w/o any anomalies (i.e., some of the encrypted data is available)
- <img src="https://github.com/joedefen/luks-tray/raw/main/luks_tray/resources/orange-shield-v04.svg" alt="Orange Shield Icon" width="24" height="24"> - The history file is encrypted and its master password has not been entered yet, so the containers cannot be inspected at all: the state is *unknown*, not "everything locked". Enter the password with the menu entry `✎ Click to enter master password`.

Here is an sample menu as seen when you right-click the tray shield icon:

<img src="https://github.com/joedefen/luks-tray/raw/main/images/sample-menu.png" alt="Sample Menu"></center>

Notes:

- the first section shows LUKS devices, and the second shows LUKS files.
- click a ▣ or ⧈ entry to dismount and lock a mounted, unlocked LUKS container

  - note that ▣ indicates full access; ⧈ indicates readonly
  - if busy, you are shown the PIDs and names of processes preventing dismount

- click a ▽ entry to unlock and mount a locked LUKS container
- click a ‼ entry to lock an unmounted, unlocked container (considered an anomaly)
- the fixed entries at the bottom carry a symbol: `✦` create a new crypt file, `✚` add an existing one, `⚙` options, `≡` open the log, `☓` exit, `↺` restart
- `≡ Open Log` only appears when it can work: the applet's own output *is* the log file, so `less` needs a terminal emulator to be started in. If none is found, set `log_viewer` in `config.ini` (see below).
- once this session has reported a failure, that entry reads `≡ Open Log [n errors]`; the count is of this session only, so a restart clears it, and the dialogs' own errors (a mount that did not happen, a viewer that would not start) are included, not only the ones that reached the file.
- LUKS devices must be created with other tools such as Gnome Disks.
- LUKS files are only automatically detected in its history; when you add or create new LUKS files, they are added to the history.
- A TrueCrypt/VeraCrypt file is managed like a LUKS file once added, except that it can never be *discovered*: its header is encrypted, so nothing can tell it apart from random data without the password. Add it once with `✚`, and the history carries it from then on.
- When creating LUKS files, the default folder is `~/.Vaults`.
- When mounting LUKS files for the first time, the default folder is `~/Vaults/{basename}` where `{basename}` is the basename of the encrypted LUKS file (less any `.luks`, `.luks2`, or `.crypt` suffix).


## Configuration

Settings and data files are stored in `~/.config/luks-tray/`:
- **History file** - Stored of passwords and mount preferences; it is encrypted when the master password enabled.
- **Lock file** - `luks-tray.lock` holds the PID of the running instance; the kernel releases the lock when that process exits, so a crash never blocks the next start.
- **Log file** - `debug.log` is where the applet writes everything it does (the command it ran, what it refused, with a timestamp and the code location). It is the destination whether a desktop, a systemd unit or a terminal started the applet: started from a terminal the lines are printed *and* mirrored into the file, so `≡ Open Log` shows that session as well; `luks-tray -o` is the opt-out that keeps them on the terminal only (a systemd unit ignores `-o`: there stdout is the unit's own). The file opens with `=== luks-tray v<version> started, pid <pid> ===` and closes with the matching `stopped` line — also after `systemctl --user stop` or `--replace`, which end the process with a signal — so a file holding several runs is still readable. It is created owner-only, since it names container paths and mount points, and at 512 KB it rotates to `debug.log.old` — one generation, which the viewer pages as its second file. Lines Qt itself produces arrive as `qt: …` with Qt's own `[file:line]`, each reported once, while a platform theme's chatter (the qt5ct/qt6ct palette dump) and Qt's `QThreadStorage … destroyed` shutdown notice are dropped. `luks-tray --follow-log` tails the file from a shell.
- **INI file** - the default .ini file looks like:

      [ui]
      show_passwords_by_default = True
      show_anomaly_alerts = True
      auto_mount_folder = ~/Vaults
      log_viewer =

  You can thus change
    - whether passwords are shown by default when being first entered.
    - whether ‼️ entries (i.e., anomalies) cause the tray icon to change to the alert shield.
    - where the automatically generated mount points live
    - which terminal to show the log in: `log_viewer` is a command prefix such as `foot -e` or `xterm -maximized -e`. Empty means "use the first terminal emulator found".

  The same four settings are in the tray menu under `⚙ Options…`, which is also where the master password is set, changed or removed (`Master Password…`). The dialog checks `auto_mount_folder` before saving: a path that is not a dedicated, absolute, user-owned folder is refused with the reason in a message box, instead of being quietly replaced by the default at mount time. `log_viewer` is checked too — a command line that cannot be parsed would otherwise silently leave the menu without its `≡ Open Log` entry. Saving rewrites `config.ini` — settings this version does not know are kept, comments are not.

## Security Notes

- Passwords are only stored when master password feature is enabled
- History file is encrypted using the master password
- The log file is owner-readable only, and never contains passwords or passphrases; it does record container paths and mount points
- System mount points are excluded by default to prevent interference with disk encryption
- When creating LUKS file containers, password strength is not enforced; use due care.

---
## Limitations

- **Not for whole disk encryption** - Designed for fixed and removable partition and file containers. Thus, excludes system mount points like `/`, `/home`, `/var` to avoid interfering with boot-time encrypted volumes.
- **No udisks2 integration** - Manages its own mount/unmount state. Mixing mounts/unmounts desktop auto-mounting tools may not produce the best results.
- **Loop device requirement** - LUKS file containers are found through their loop device, so `lsblk` must report one (standard on most distros). A TrueCrypt/VeraCrypt file has no loop device -- `cryptsetup` reads the file itself -- and is carried by the history instead.
- **TrueCrypt/VeraCrypt: add-only, default PIM** - These are supported for *legacy file containers*: they must be added by hand (no discovery, see above) and this applet never creates one. Only the default PIM (0) is used, so a container created with a non-default PIM is refused with the same message as a wrong password, and hidden or system (whole-disk) volumes are out of scope.
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
  - **niri** with **waybar**
  - **sway** with **waybar** (see the compositor rule below for floating dialogs)

> ⚠️ **GNOME**: Requires a third-party extension (such as AppIndicator support) to show tray icons. Results may vary across GNOME versions.<br>
> ⚠️ **Xfce** and similar lightweight DEs: Tray menus may open off-screen or be partially cut off, depending on panel layout and screen resolution.


#### Wayland tiling compositors (niri, sway)

A Wayland compositor, not the app, decides where a window goes and whether it
floats. luks-tray therefore sets a stable Wayland `app_id` of `luks-tray` and
asks you to add the one rule your compositor needs. The same `app_id` covers the
mount/options dialogs, the error boxes and the file pickers.

Run this in the session you are configuring to print the matching snippet:

```sh
luks-tray --show-window-rules
```

**niri** -- add to `~/.config/niri/config.kdl`, then run
`niri msg action load-config-file`:

```kdl
window-rule {
    match app-id="luks-tray"
    open-floating true
}
```

**sway** -- add to `~/.config/sway/config`, then run `swaymsg reload`:

```
for_window [app_id="luks-tray"] floating enable, move position center, border normal
```

Do **not** add `no_focus`: the password dialogs need keyboard focus.

Sway hides title bars by default; niri and X11 draw them. Every luks-tray dialog
also shows the same purpose as a heading inside the dialog, so nothing depends
on the compositor drawing a title bar. Setting `XDG_CURRENT_DESKTOP` (or
`DESKTOP_SESSION`) lets `--show-window-rules` detect the compositor and keeps
the logged session banner identifiable.

---
## Starting at Login

luks-tray is a tray applet: something has to start it once your desktop session
exists. Pick the mechanism your session already uses, and use only one of them
-- two starts fight over the single-instance lock and the loser exits 3.

### A `systemd --user` unit (recommended on systemd distros)

Two units ship with the package, one per display server, and one command
installs the right one for the session it runs in:

```
luks-tray --svc-install     # write ~/.config/systemd/user/luks-tray.service and enable it
luks-tray --svc-status      # 0 = running, 1 = installed but not running, 2 = not installed
```

`--svc-install` renders the unit, reloads systemd and enables it -- it does
**not** start the applet, because you may already be running one from a desktop
autostart; the unit takes over at your next login. Use `--svc-start` to start it
now.

| Verb | What it does |
| --- | --- |
| `--svc` / `--svc-status` | Report the unit's state. Exit code 0 active, 1 installed but not running, 2 not installed, so it is usable from `if`. |
| `--svc-install` | Render the unit for this session, write it, reload, enable. The only verb that writes the unit file. |
| `--svc-start` | Start the installed unit now, and enable it if it was disabled. Never writes the unit. |
| `--svc-restart` | Restart it, so it runs the code `~/.local/bin/luks-tray` resolves to -- run this after `git pull` on an editable install. |
| `--svc-stop` | Stop it now. It stays enabled, so it returns at your next login. |
| `--svc-uninstall` | Stop, disable and delete the unit. Your config, history and logs stay. |
| `--show-svc-file` | Print the rendered unit and exit: a starter for a hand-installed or customised unit. |

The unit runs `~/.local/bin/luks-tray`, where `pipx` and `pip install --user`
both put it, so installing from a virtualenv writes the same file as installing
from any other shell. It also pins the session variables systemd cannot inherit
from the compositor -- `WAYLAND_DISPLAY` (or `DISPLAY`) and
`XDG_CURRENT_DESKTOP`/`DESKTOP_SESSION`, which Qt and the dialogs need -- filled
in from the shell that ran `--svc-install`. Run it from a terminal *inside* the
graphical session, not over `ssh`.

> ⚠️ If the tray only appears when you start it by hand, the user manager has no
> display for the unit. Add
> `dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP`
> to your compositor's config (sway users usually already have it), then
> `luks-tray --svc-restart`.

Under a unit, the `↺ Restart luks-tray` menu entry asks systemd to restart the
unit instead of spawning a second process, and `--replace` refuses to kill an
instance systemd supervises (it would come straight back) -- it points you at
`luks-tray --svc-restart` instead. Start-up failures are logged to
`~/.config/luks-tray/debug.log`; `systemctl --user status luks-tray.service`
shows what systemd saw.

### Desktop autostart

Every DE/WM has its own mechanism; use whichever you already use for other
applets:

- **XDG autostart** -- put a `luks-tray.desktop` in `~/.config/autostart/`:

  ```
  [Desktop Entry]
  Type=Application
  Name=luks-tray
  Exec=luks-tray
  ```

  Add `OnlyShowIn=KDE;` (or another DE) to restrict it to one desktop.
- **KDE Plasma** -- System Settings → Autostart → Add Application.
- **sway / i3** -- `exec luks-tray` in the config (sway's `exec_always`, ideally
  from a script that waits for the bar).  The tray registers itself when the
  StatusNotifierWatcher appears, so start order does not matter.
- **niri** -- `spawn-at-startup "luks-tray"`.
- **Xfce / LXQt / MATE / Cinnamon** -- the session's "Application Autostart"
  settings dialog.

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

For developing `luks-tray`, see [tests/README-DEV.md](tests/README-DEV.md); known
defects and the items still open are in [tests/AUDIT.md](tests/AUDIT.md).