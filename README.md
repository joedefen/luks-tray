# LUKS Tray

**luks-tray** is a Qt tray app for mounting and unmounting LUKS-encrypted devices and file containers. Legacy TrueCrypt/VeraCrypt file containers are supported for existing files.

Version 3.0.0.

## Features

- Tray menu with one click to mount, unmount, lock and unlock containers.
- Shield colors report state: locked, mounted, read-only, or unlocked but not mounted.
- Remembers mount points and, optionally, container passwords under a master password.
- Detects inserted LUKS devices and manages LUKS and TrueCrypt/VeraCrypt files.
- Single instance, restart from the menu, and a self-explaining debug log.
- `systemd --user` unit installer and Wayland window rules.

## Quick Start

1. Install with `pipx install luks-tray`.
2. Configure passwordless `sudo` for the commands in [Requirements](#requirements).
3. Run `luks-tray --check-deps`, then `luks-tray`.
4. Right-click the shield to see containers; left-click one to mount or unmount.
5. Start it at login with `luks-tray --svc-install` or a desktop autostart entry.

Only one copy runs at a time. A second launch exits with status 3; use `luks-tray --replace` to take over.

## Interface

| Symbol | Meaning |
| --- | --- |
| `▽` | locked and unmounted |
| `▣` | mounted |
| `⧈` | mounted read-only |
| `‼` | unlocked but not mounted (an anomaly) |

The shield is white when every container is locked, green when something is mounted, alert-colored for an anomaly, and orange when the history is locked and the state is unknown.

The menu also offers create/add crypt file, options, master password, open log, exit and restart. Open Log appears only when a terminal viewer is available and shows `[n errors]` after a failure in this session.

## Configuration

Files live in `~/.config/luks-tray/`:

- `config.ini` -- settings.
- `history.json` -- mount points and optional passwords.
- `debug.log` -- log, rotated once to `debug.log.old`.
- `luks-tray.lock` -- single-instance lock.

Default `config.ini`:

```ini
[ui]
show_passwords_by_default = True
show_anomaly_alerts = True
auto_mount_folder = ~/Vaults
log_viewer =
```

`auto_mount_folder` is validated when changed. `log_viewer` is a command prefix such as `foot -e`, or empty to auto-detect a terminal. The same settings are in `⚙ Options…`.

## Security

- Passwords are stored only when a master password is enabled.
- History encryption uses Fernet with PBKDF2-HMAC-SHA256, a per-file salt and 600,000 iterations.
- The master password is held in memory only; restarting or locking the session locks the history.
- There is no recovery for a forgotten master password. Delete `history.json` to start over; the containers themselves are untouched.
- Clearing the master password stores the history in clear text and forgets the remembered container passwords.
- The log never contains passwords, but it does record container paths and mount points.

## Limitations

- Whole-disk and system encryption are out of scope.
- No udisks2 integration.
- LUKS file containers need a loop device reported by `lsblk`.
- TrueCrypt/VeraCrypt support covers existing file containers with the default PIM only.
- Containers with several filesystems get limited support.

## Requirements

`luks-tray --check-deps` reports missing commands. The applet uses `cryptsetup`, `fuser`, `losetup`, `mkfs.ext4`, `mount`, `umount`, `bindfs` and related tools.

Passwordless `sudo` is required for the commands it runs:

```
Cmnd_Alias LUKS_TRAY = /usr/bin/cryptsetup, /usr/bin/mount, /usr/bin/umount, \
                       /usr/bin/losetup, /usr/bin/fuser, \
                       /usr/bin/mkdir, /usr/bin/rmdir, /usr/bin/chown, \
                       /usr/bin/truncate, /usr/bin/mkfs.ext4, /usr/bin/bindfs

{yourusername} ALL=(ALL) NOPASSWD: LUKS_TRAY
```

Confirm paths with `command -v`; some tools live under `/usr/sbin`.

A StatusNotifier tray host is required, for example Plasma, waybar with niri or sway, or polybar with i3. GNOME needs an AppIndicator extension.

### Wayland tiling compositors

`--show-window-rules` prints the rule for the current session. On niri, add it to `~/.config/niri/config.kdl` and run `niri msg action load-config-file`:

```kdl
window-rule {
    match app-id="luks-tray"
    open-floating true
}
```

On sway, add it to `~/.config/sway/config` and run `swaymsg reload`:

```
for_window [app_id="luks-tray"] floating enable, move position center, border normal
```

Do not add `no_focus`; password dialogs need keyboard focus.

## Starting at Login

Use one mechanism only.

| Command | Effect |
| --- | --- |
| `--svc-status` | Report state: 0 active, 1 installed not running, 2 not installed. |
| `--svc-install` | Write and enable the unit; does not start it. |
| `--svc-start` | Start the installed unit now. |
| `--svc-restart` | Restart it so it runs the current code. |
| `--svc-stop` | Stop it; it remains enabled. |
| `--svc-uninstall` | Stop, disable and delete it. |
| `--show-svc-file` | Print the rendered unit. |

The unit runs `~/.local/bin/luks-tray` and pins the display variables of the installing session. At startup the applet re-derives them from the listening display server, so switching between Wayland and X11 needs no reinstall.

Alternatively use a desktop autostart entry with `Exec=luks-tray`.

## License

See [LICENSE](LICENSE).
