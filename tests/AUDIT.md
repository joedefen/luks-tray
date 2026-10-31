# Audit: `luks-tray` (v2.0.5)

**Scope:** [`pyproject.toml`](../pyproject.toml), [`runner.py`](../runner.py), [`deploy`](../deploy), [`luks_tray/main.py`](../luks_tray/main.py), [`luks_tray/History.py`](../luks_tray/History.py), [`luks_tray/IniTool.py`](../luks_tray/IniTool.py), [`luks_tray/Utils.py`](../luks_tray/Utils.py), [`README.md`](../README.md).

**Method:** manual review, `python -m py_compile` (passes on 3.13), `ruff check` (64 findings), and contract tracing of `run_cmd`/`sudo_cmd`.

**Progress:** 3 of 9 critical items fixed and re-verified — **#1** (env-var guard), **#2** (`run_cmd`/`sudo_cmd` contract) and **#3** (anomaly-case `cryptsetup close` crash). Items 4–9 are still open. Line links for #1/#2 were re-verified against the current tree; the later sections predate those edits and may drift by a few lines (the #3 fix added 12 lines at `main.py:1535`, so links into code below ~line 1567 are stale by +12).

**Overall:** A functional PyQt6 tray app with clean separation of concerns (device discovery, history, config, UI). However it has **multiple crash-level defects on normal code paths**, **a weak password-encryption scheme**, a **declared-vs-actual Python version mismatch**, and no tests. Not production-ready as-is.

---

## 🔴 Critical

Status legend: ✅ fixed · ⬜ open. Line links point at the current tree.

### ✅ 1. Startup crash when desktop env vars are unset — FIXED

[`requires_manual_title()`](../luks_tray/main.py#L60) did:

```python
if 'sway' in os.environ.get(param).lower():
```

If `XDG_CURRENT_DESKTOP` and `DESKTOP_SESSION` are both unset (headless, some autostart contexts, cron), `.get()` returns `None` → `AttributeError` **at import time**, when the module-level `IS_SWAY_LIKE_ENV` is evaluated ([main.py:72](../luks_tray/main.py#L72)).

**Fix applied** ([main.py:67](../luks_tray/main.py#L67)):

```python
if 'sway' in (os.environ.get(param) or '').lower():
    return True
```

**Verified:** the function was executed with both vars purged (`False`), with `XDG_CURRENT_DESKTOP=sway` (`True`), and with `XDG_CURRENT_DESKTOP=KDE` (`False`); the module now imports cleanly with no desktop vars set. A sweep of the remaining `os.environ` reads (`SUDO_USER`, `SUDO_UID`, `SUDO_GID`, `EDITOR`) confirmed they already supply defaults or guard with truthiness checks.

### ✅ 2. `run_cmd`/`sudo_cmd` contract misused — FIXED

[`run_cmd()`](../luks_tray/main.py#L91) returned `None` on success and an error string on failure — it never returned stdout. But:

- [`_setup_loop_device()`](../luks_tray/main.py#L1248) did `result = sudo_cmd(['losetup', '-f', '--show', ...])` then `result.strip()` → `AttributeError: 'NoneType'` **on every success**.
- [`mount_luks_container()`](../luks_tray/main.py#L1273) was therefore never reached for file/loop-backed containers — the `AttributeError` was swallowed by its `except Exception` and returned as `An error occurred: ...`.

**Fix applied** — all process execution now goes through `subprocess.run()`, and callers that need the child's output have a dedicated helper:

```python
def run_cmd(args, errs=None, input_str=None):        # None / 'FAIL: ...' (unchanged contract)
    rc, stdout, stderr = run_cmd_capture(args, input_str)
    if rc == 0:
        return None
    err = f'FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]'
    if errs is not None:
        errs.append(err)
    return err

def run_cmd_capture(args, input_str=None):           # (rc, stdout, stderr)
    sub = subprocess.run(args, input=input_str, capture_output=True,
                         text=True, check=False)
    return sub.returncode, sub.stdout, sub.stderr

def sudo_run(args, input_str=None):                  # sudo -n variant
    return run_cmd_capture(['sudo', '-n'] + args, input_str)
```

- [`run_cmd()`](../luks_tray/main.py#L91) / [`sudo_cmd()`](../luks_tray/main.py#L84) keep the `None`-or-error-string contract, so every existing call site (mount, `cryptsetup open`/`close`, `mkdir`, `truncate`, `mkfs`, loop detach, …) is unaffected; the `Popen`/`communicate` boilerplate is gone.
- [`_setup_loop_device()`](../luks_tray/main.py#L1248) now checks `rc` first and takes the loop device from real stdout via [`sudo_run()`](../luks_tray/main.py#L114), returning `(error, None)` on failure and rejecting empty output (which previously would have set `container.name = ''`).
- Latent bug fixed in the same helper: `if err and errs:` silently discarded errors whenever a caller passed a freshly created `errs = []` (an empty list is falsy), so collected-error dialogs never saw them. Now `if errs is not None`.

**Verified:** `run_cmd(['echo','hi'])` → `None`; `run_cmd(['false'])` → `'FAIL: false:   [rc=1]'` and appends to `errs` (`len == 1`); `run_cmd_capture(['echo','hi'])` → `(0, 'hi\n', '')`; `run_cmd_capture(['cat'], 'secret')` → `(0, 'secret', '')`; `_setup_loop_device` on a missing image → `('FAIL: losetup -f --show ... [rc=1]', None)` with no `AttributeError` and `container.name` untouched. `py_compile` passes and `ruff --select F,E9` findings dropped 10 → 8, all pre-existing (unused imports + the PEP 701 f-strings of item 6).

**Remaining, folded into item 6:** [main.py:1318](../luks_tray/main.py#L1318) still runs `cryptsetup luksFormat` via bare `subprocess.run(..., check=True)` without `sudo -n`, then tests `sub.returncode != 0` — a dead branch, since `check=True` raises into the surrounding `except`. It should become `sudo_run(args, input_str=password)`; the file was created by `sudo truncate`, so the unprivileged `luksFormat` would also fail on permissions.

### ✅ 3. Crash unmounting an "opened but no-filesystem" device (the anomaly case) — FIXED

[`unmount_device()`](../luks_tray/main.py#L1521) started `filesystem = ''`, then unconditionally called:

```python
sudo_cmd(["cryptsetup", "close", filesystem.name], errs)   # was main.py:1551
```

If `container.filesystems` is empty — exactly the `‼` open-but-unmounted state the UI advertises — this raised `AttributeError: 'str' object has no attribute 'name'`. This is a documented first-class use case ([README line 50](../README.md#L50)), so it is reachable.

**Fix applied** ([main.py:1535](../luks_tray/main.py#L1535)) — only well-formed filesystem entries are iterated (so a nameless entry can no longer reach the unmount loop either), and the `cryptsetup close` target is resolved defensively instead of assuming a child exists:

```python
filesystems = [fs for fs in container.filesystems if getattr(fs, 'name', '')]
...
close_name = filesystems[0].name if filesystems else ''
if not close_name and container.type == 'crypt':
    close_name = container.name      # file containers are their own mapper
if len(errs) <= 1:
    if close_name:
        sudo_cmd(["cryptsetup", "close", close_name], errs)
    else:
        errs.append(f'ERR: cannot close {container.name}: no mapper '
                    f'device name known (no filesystem detected)')
```

The normal device path is byte-for-byte the same command as before; the anomaly path now closes the mapping for file containers and, where no mapper name is derivable at all (partition container with no crypt child), reports an actionable error through the existing `alert_errors()` dialog and keeps the dialog open — same contract as every other failure branch.

**Verified:** `unmount_device()` was driven directly (Qt offscreen, `sudo_cmd` stubbed) with four fake containers: (a) partition + `filesystems=[luks-1111-2222]` → `cryptsetup close luks-1111-2222`; (b) partition + `opened=True` + `filesystems=[]` (the anomaly) → **no exception**, no `cryptsetup` call, `ERR: cannot close sdb1: ...` alert, dialog not accepted; (c) `type='crypt'` (file container) + `filesystems=[]` → `cryptsetup close mycrypt`; (d) `filesystems=[<name=''>]` → **no exception**, same actionable alert. `py_compile` passes. Note: `main.py` grew 12 lines, so links below to code after line 1567 (`unmount_file`, `mount_file`, …) shift by +12.

### 4. New/added file containers are never remembered

[`mount_file()`](../luks_tray/main.py#L1681) creates a container with `uuid=None`, mounts it, then calls [`update_history(uuid, ...)`](../luks_tray/main.py#L1790) with `uuid=None` → [`HistoryClass.get_vital(None)`](../luks_tray/History.py#L86) stores an entry under the `None` key and never records `back_file`.

The helper intended for this — [`generate_uuid_for_file_path()`](../luks_tray/main.py#L74) — is **dead code** (never called). After restart, file containers vanish from the menu and the JSON gains a `"null"` key.

### 5. Weak master-password KDF

[`_password_to_fernet_key()`](../luks_tray/History.py#L149) uses a **single unsalted SHA-256** of the password as the Fernet key:

```python
key = hashlib.sha256(self.master_password.encode()).digest()
```

No salt, no iteration count, no memory hardness → trivially brute-forceable offline if `history.json` is obtained. Use `PBKDF2HMAC` (≥600k iterations) or Argon2id with a random per-file salt.

### 6. Declared Python support is wrong (install/run breaks on 3.8–3.11)

[`pyproject.toml`](../pyproject.toml#L14) says `requires-python = ">=3.8"`, but:

- [main.py:101](../luks_tray/main.py#L101) and [main.py:1318](../luks_tray/main.py#L1318) use **PEP 701** quote reuse in f-strings (`f'... {' '.join(args)} ...'`) — parser error **before Python 3.12** (ruff flags `invalid-syntax`).
- [main.py:120](../luks_tray/main.py#L120) uses PEP 604 `str | None` without `from __future__ import annotations` → import-time `TypeError` on <3.10.

The effective floor is **3.12**.

### 7. Plaintext metadata + no file hardening / non-atomic writes

[`save()`](../luks_tray/History.py#L157) writes `history.json` with the default umask (typically `0644`) and no atomic replace. Consequences:

- Even without a master password it stores mount paths, UUIDs and backing-file paths in clear text (privacy metadata).
- A crash mid-write corrupts history.

Should `chmod 0600` the file and write to a temp file then `os.replace()`.

### 8. Destructive cleanup driven by unvalidated config

[`remove_unused_automounts()`](../luks_tray/main.py#L838) iterates **whatever** `auto_mount_folder` resolves to and runs `sudo rmdir` on every empty subdirectory ([main.py:854](../luks_tray/main.py#L854)). If a user sets `auto_mount_folder = /home` (or a typo such as `/`), it will remove empty directories under that path with root privileges. Validate that the folder is dedicated and user-owned before cleaning.

### 9. In-memory password wiped as a side effect

[`_namespaces_to_json_data()`](../luks_tray/History.py#L139):

```python
legit = vars(vital)          # alias to vital.__dict__
legit['password'] = ''       # mutates the live object
entries[uuid] = vars(vital)  # same dict again
```

The `legit` variable is redundant and the mutation zeroes the live password, affecting later code such as [`update_history`](../luks_tray/main.py#L765). Copy instead: `legit = dict(vars(vital))`.

---

## 🟠 High — functional bugs

| # | Location | Issue |
|---|----------|-------|
| 1 | [`browse_folder()`](../luks_tray/main.py#L1086) | `self.home_dir()` calls a **string** (`home_dir` is a `str` set by [`get_real_user_home_directory()`](../luks_tray/main.py#L1074)) → `TypeError` when the mount field is cleared. |
| 2 | [`update_history()`](../luks_tray/main.py#L765) | Sets `vital.upon` **after** `put_vital()` (which saves), so a new mount point is not persisted on that save. Move the assignment before `put_vital`. |
| 3 | [`check_upon()` callers](../luks_tray/main.py#L1482) | `mount_points = tray.update_mounts()` returns the **device key set** ([main.py:509](../luks_tray/main.py#L509)), not mount points, yet [`check_upon`](../luks_tray/main.py#L1186) treats it as mount points (`text in mount_points`). Duplicate-mount detection is broken; pass `tray.upons`. |
| 4 | [`add_input_field()`](../luks_tray/main.py#L995) | With a scalar label and multiple `keys`, it does `placeholder_texts.append(text_str)` while `placeholder_texts` may still be a `str` → `AttributeError`. Normalize both to lists first. |
| 5 | [`mount_luks_container()`](../luks_tray/main.py#L1273) | `luksFormat` runs **without `sudo`** (unlike everything else) and uses `check=True`, so the `if sub.returncode != 0` branch is unreachable and real stderr is swallowed by the outer `except` → generic "An error occurred". Convert to [`sudo_run()`](../luks_tray/main.py#L114) (see #2). |
| 6 | [`mount_device()`](../luks_tray/main.py#L1457) | `luks_device = ''` when `len(container.filesystems) != 1`; `cryptsetup open ... ''` then fails with a confusing error. |
| 7 | [`MountDeviceDialog`](../luks_tray/main.py#L1404) | Open-state branch renders `f'Close {",".join(mounts)}?'` where `mounts` is `[]` ([main.py:1427](../luks_tray/main.py#L1427)) → "Close ?". |
| 8 | Error sentinel idiom | `errs.append(f'{container.name}')` then `if len(errs) <= 1` treats the first element as a placeholder ([main.py:1452](../luks_tray/main.py#L1452), 1509, 1678). Use a plain `errs = []` and `if not errs`. |
| 9 | [CLI paths](../luks_tray/main.py#L1814) | `--edit-config` / `--follow-log` use `IniTool(paths_only=True)`, which skips [`ensure_ini_file()`](../luks_tray/IniTool.py#L70). On a fresh install the folder/file may not exist. |
| 10 | Runtime `assert` | Validation via `assert` at [main.py:398](../luks_tray/main.py#L398), [434](../luks_tray/main.py#L434), [802](../luks_tray/main.py#L802), [819](../luks_tray/main.py#L819), [1286](../luks_tray/main.py#L1286). Under `python -O` these vanish, so missing deps/icon-copy failures continue half-initialized. |
| 11 | [`_unlock_luks()`](../luks_tray/main.py#L1226) | `if hasattr(self, 'opened') and self.opened` — `self` is a dialog, which never has `opened`; the "already unlocked" guard is dead code. |
| 12 | [stdin redirect](../luks_tray/main.py#L1842) | Replaces `stdin` with `/dev/null`; acceptable, but combined with no `subprocess` timeouts, a hung `cryptsetup` freezes the UI indefinitely. |

---

## 🟡 Medium — design, performance, quality

1. **UI-thread blocking & re-entrancy.** The 3 s `QTimer` ([main.py:452](../luks_tray/main.py#L452)) runs [`update_menu()`](../luks_tray/main.py#L546): opens `/proc/mounts`, spawns `lsblk`, reloads/decrypts history, and runs `blkid` per backing file. [`show_progress()`](../luks_tray/main.py#L1157) calls `QApplication.processEvents()`, so the timer can **re-enter** `update_menu` mid-mount. Move polling to a worker thread and remove `processEvents`.
2. **`blkid` per file per refresh.** [`_json_data_to_namespaces()`](../luks_tray/History.py#L196) shells out `blkid` for every `back_file` on each restore (every 3 s). Cache by `(path, mtime)`.
3. **No subprocess timeouts** anywhere (`run_cmd`, `run_unmount`, `parse_lsblk`, `blkid`). One hung command stalls the tray.
4. **Silent failure.** 16 broad `except Exception` and several `try/except/pass` ([main.py:845](../luks_tray/main.py#L845), [:847](../luks_tray/main.py#L847), [Utils.py:70](../luks_tray/Utils.py#L70)). At minimum log via `prt`.
5. **Monolith.** [`main.py`](../luks_tray/main.py) is 1,859 lines mixing device discovery, LUKS ops, and 5 dialog classes. Split into `device_info.py`, `luks_ops.py`, `dialogs/`.
6. **26 `TBD` docstrings** ([main.py:90](../luks_tray/main.py#L90) etc.) and stale commented-out code ([main.py:51](../luks_tray/main.py#L51), [285](../luks_tray/main.py#L285), [380](../luks_tray/main.py#L380)).
7. **Dead code / unused imports:** `generate_uuid_for_file_path`, `rerun_module_as_root`, `Utils.copy_to_folder`, `History._load_initial_state`, `udisks_cmd`; `shlex`, `tempfile`, `Path`, `StringIO`, `QSizePolicy`, `QFontDatabase` (ruff F401).
8. **Global mutable state:** `LuksTray.singleton`, module-level `IS_SWAY_LIKE_ENV`, `Utils.prt_path`, and [`IniTool.__init__` calling `os.chdir()`](../luks_tray/IniTool.py#L52).
9. **Dependency check list** ([`check_dependencies`](../luks_tray/main.py#L457)): `bindfs` is listed twice; `blkid` and `mkdir` are used but not checked; `udisks` plumbing is dead.
10. **Module naming:** uppercase module files (`History.py`, `IniTool.py`, `Utils.py`) violate PEP 8 (`N999`).
11. **No tests, CI, or dev dependencies** in [`pyproject.toml`](../pyproject.toml#L24). No `[tool.ruff]` / `[tool.pytest]` config despite ruff being the obvious linter.

---

## 🔵 Documentation & packaging

- **Wrong folder names in README.** It says `~/.Crypts` and `~/Crypts/{basename}` ([README.md:54](../README.md#L54)), but the code uses `~/.Vaults` / `~/Vaults` ([main.py:1083](../luks_tray/main.py#L1083)).
- **Over-broad privilege guidance.** README recommends `{user} ALL=(ALL) NOPASSWD: ALL` ([README.md:97](../README.md#L97)) while the app only needs a fixed command set. Recommend a scoped sudoers entry (`cryptsetup`, `mount`, `umount`, `losetup`, `fuser`, `mkdir`, `rmdir`, `truncate`, `mkfs.ext4`, `bindfs`).
- Typos: "luk-tray" ([README.md:20](../README.md#L20)), "Waylan" ([README.md:104](../README.md#L104)), "Workaounds" ([README.md:113](../README.md#L113)).
- [`pyproject.toml`](../pyproject.toml#L13) uses the legacy `license = { file = ... }` table (deprecated in favor of an SPDX string); no classifiers for the actual Qt/Python floor.
- [`deploy`](../deploy) runs `pip install -e .` into the active environment rather than the `lt-venv` it references in its own echo output.

---

## ✅ What's done well

- All privileged calls go through explicit argv lists (no `shell=True`) → no shell-injection surface from container names/paths.
- LUKS passwords are fed via `--key-file -` on stdin, not argv (not visible in `ps`).
- Device discovery via structured `lsblk -J` and `/proc/mounts` rather than brittle text scraping; banned system mount points are excluded.
- [`IniTool`](../luks_tray/IniTool.py#L21) has a solid, defensive config parser (type-coerced, unknown keys skipped with messages).
- Sensible separation of `History`/`IniTool`/`Utils`, and the "clear_text / unlocked / locked" history state machine is a good design.
- README documents limitations honestly (udisks2, multi-FS, system mounts).

---

## Recommended fix order

1. **Startup + crash fixes:** ✅ env-var guard (#1), ✅ stdout-returning command helper + `_setup_loop_device` (#2), ⬜ `filesystem.name` guard (#3).
2. **Correct history identity:** use `generate_uuid_for_file_path`/back-file key for new files and persist `back_file` (#4).
3. **Crypto hardening:** PBKDF2/Argon2 with salt, `0600`, atomic write (#5, #7).
4. **Packaging:** bump `requires-python` to `>=3.12` (or rewrite the two f-strings + annotations) (#6).
5. **Safety:** validate `auto_mount_folder` before sudo `rmdir` (#8); scope the documented sudoers rule.
6. **High-list correctness items** (`browse_folder`, `check_upon` mount set, `update_history` ordering, `add_input_field`, `luksFormat` sudo, asserts).
7. **Refactor/perf:** worker thread + cached `blkid`, split `main.py`, add tests + a scoped ruff config.

`ruff check .` currently reports **64 issues** (16 blind `except`, 6 unused imports, 2 real `invalid-syntax` under the declared Python, 4 `try/except/pass`, plus style). Most are auto-fixable with `ruff check --fix`; the critical items above require manual work.

After the #1/#2 fixes the `ruff --select F,E9` subset of [`main.py`](../luks_tray/main.py) is **8 findings** (6 unused imports + the 2 PEP 701 f-strings), no new findings introduced, and `python -m py_compile` passes.
