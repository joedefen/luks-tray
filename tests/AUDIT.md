# Audit: `luks-tray` (v2.0.5)

**Scope:** [`pyproject.toml`](../pyproject.toml), [`runner`](../runner), [`deploy`](../deploy), [`luks_tray/Main.py`](../luks_tray/Main.py), [`luks_tray/History.py`](../luks_tray/History.py), [`luks_tray/IniTool.py`](../luks_tray/IniTool.py), [`luks_tray/Utils.py`](../luks_tray/Utils.py), [`README.md`](../README.md).

**Method:** manual review, `python -m py_compile` (passes on 3.13), `ruff check` (64 findings), and contract tracing of `run_cmd`/`sudo_cmd`.

**Progress:** **9 of 9** critical items fixed and re-verified — **#1** (env-var guard), **#2** (`run_cmd`/`sudo_cmd` contract), **#3** (anomaly-case `cryptsetup close` crash), **#4** (file containers not remembered), **#5** (master-password KDF), **#6** (declared Python floor, now 3.11), **#7** (atomic, owner-only history writes), **#8** (`auto_mount_folder` validated before cleanup) and **#9** (history serialization no longer mutates the live namespace). Every critical item is closed; the High-severity table below is what remains. Line links for #1–#7 were re-verified against the current tree; the later sections predate those edits and may drift (the #3/#4/#6 fixes added ~55 lines to `Main.py`, the #5/#7 fixes ~50 lines to `History.py`, and the #8 fix ~150 lines to `Main.py`, so links into other code are stale).

**Overall:** A functional PyQt6 tray app with clean separation of concerns (device discovery, history, config, UI). However it has **multiple crash-level defects on normal code paths**, **a weak password-encryption scheme**, a **declared-vs-actual Python version mismatch**, and no tests. Not production-ready as-is.

---

## 🔴 Critical

Status legend: ✅ fixed · ⬜ open. Line links point at the current tree.

### ✅ 1. Startup crash when desktop env vars are unset — FIXED

[`requires_manual_title()`](../luks_tray/Main.py#L60) did:

```python
if 'sway' in os.environ.get(param).lower():
```

If `XDG_CURRENT_DESKTOP` and `DESKTOP_SESSION` are both unset (headless, some autostart contexts, cron), `.get()` returns `None` → `AttributeError` **at import time**, when the module-level `IS_SWAY_LIKE_ENV` is evaluated ([Main.py:72](../luks_tray/Main.py#L72)).

**Fix applied** ([Main.py:67](../luks_tray/Main.py#L67)):

```python
if 'sway' in (os.environ.get(param) or '').lower():
    return True
```

**Verified:** the function was executed with both vars purged (`False`), with `XDG_CURRENT_DESKTOP=sway` (`True`), and with `XDG_CURRENT_DESKTOP=KDE` (`False`); the module now imports cleanly with no desktop vars set. A sweep of the remaining `os.environ` reads (`SUDO_USER`, `SUDO_UID`, `SUDO_GID`, `EDITOR`) confirmed they already supply defaults or guard with truthiness checks.

### ✅ 2. `run_cmd`/`sudo_cmd` contract misused — FIXED

[`run_cmd()`](../luks_tray/Main.py#L91) returned `None` on success and an error string on failure — it never returned stdout. But:

- [`_setup_loop_device()`](../luks_tray/Main.py#L1248) did `result = sudo_cmd(['losetup', '-f', '--show', ...])` then `result.strip()` → `AttributeError: 'NoneType'` **on every success**.
- [`mount_luks_container()`](../luks_tray/Main.py#L1273) was therefore never reached for file/loop-backed containers — the `AttributeError` was swallowed by its `except Exception` and returned as `An error occurred: ...`.

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

- [`run_cmd()`](../luks_tray/Main.py#L91) / [`sudo_cmd()`](../luks_tray/Main.py#L84) keep the `None`-or-error-string contract, so every existing call site (mount, `cryptsetup open`/`close`, `mkdir`, `truncate`, `mkfs`, loop detach, …) is unaffected; the `Popen`/`communicate` boilerplate is gone.
- [`_setup_loop_device()`](../luks_tray/Main.py#L1248) now checks `rc` first and takes the loop device from real stdout via [`sudo_run()`](../luks_tray/Main.py#L114), returning `(error, None)` on failure and rejecting empty output (which previously would have set `container.name = ''`).
- Latent bug fixed in the same helper: `if err and errs:` silently discarded errors whenever a caller passed a freshly created `errs = []` (an empty list is falsy), so collected-error dialogs never saw them. Now `if errs is not None`.

**Verified:** `run_cmd(['echo','hi'])` → `None`; `run_cmd(['false'])` → `'FAIL: false:   [rc=1]'` and appends to `errs` (`len == 1`); `run_cmd_capture(['echo','hi'])` → `(0, 'hi\n', '')`; `run_cmd_capture(['cat'], 'secret')` → `(0, 'secret', '')`; `_setup_loop_device` on a missing image → `('FAIL: losetup -f --show ... [rc=1]', None)` with no `AttributeError` and `container.name` untouched. `py_compile` passes and `ruff --select F,E9` findings dropped 10 → 8, all pre-existing (unused imports + the PEP 701 f-strings of item 6).

**Remaining, folded into item 6:** [Main.py:1318](../luks_tray/Main.py#L1318) still runs `cryptsetup luksFormat` via bare `subprocess.run(..., check=True)` without `sudo -n`, then tests `sub.returncode != 0` — a dead branch, since `check=True` raises into the surrounding `except`. It should become `sudo_run(args, input_str=password)`; the file was created by `sudo truncate`, so the unprivileged `luksFormat` would also fail on permissions.

### ✅ 3. Crash unmounting an "opened but no-filesystem" device (the anomaly case) — FIXED

[`unmount_device()`](../luks_tray/Main.py#L1521) started `filesystem = ''`, then unconditionally called:

```python
sudo_cmd(["cryptsetup", "close", filesystem.name], errs)   # was Main.py:1551
```

If `container.filesystems` is empty — exactly the `‼` open-but-unmounted state the UI advertises — this raised `AttributeError: 'str' object has no attribute 'name'`. This is a documented first-class use case ([README line 50](../README.md#L50)), so it is reachable.

**Fix applied** ([Main.py:1535](../luks_tray/Main.py#L1535)) — only well-formed filesystem entries are iterated (so a nameless entry can no longer reach the unmount loop either), and the `cryptsetup close` target is resolved defensively instead of assuming a child exists:

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

**Verified:** `unmount_device()` was driven directly (Qt offscreen, `sudo_cmd` stubbed) with four fake containers: (a) partition + `filesystems=[luks-1111-2222]` → `cryptsetup close luks-1111-2222`; (b) partition + `opened=True` + `filesystems=[]` (the anomaly) → **no exception**, no `cryptsetup` call, `ERR: cannot close sdb1: ...` alert, dialog not accepted; (c) `type='crypt'` (file container) + `filesystems=[]` → `cryptsetup close mycrypt`; (d) `filesystems=[<name=''>]` → **no exception**, same actionable alert. `py_compile` passes. Note: this fix added 12 lines at `Main.py:1535`, so pre-existing links into code below that point (`unmount_file`, `mount_file`, …) are stale (the #4 fix shifted them further).

### ✅ 4. New/added file containers are never remembered — FIXED

[`mount_file()`](../luks_tray/Main.py#L1724) created a container with `uuid=None`, mounted it, then called `update_history(uuid, ...)` with `uuid=None` → [`HistoryClass.get_vital(None)`](../luks_tray/History.py#L98) stored an entry under the `None` key and never recorded `back_file`. After restart, file containers vanished from the menu and the JSON gained a `"null"` key.

The helper the audit originally pointed at — [`generate_uuid_for_file_path()`](../luks_tray/Main.py#L74) — is **the wrong key** for this job: [`_json_data_to_namespaces()`](../luks_tray/History.py#L271) re-derives every `back_file`'s LUKS UUID with `blkid` on restore and **purges** the entry unless it equals the dict key, so a path-hash key would be culled on the very next load. The entry must be keyed by the LUKS UUID *inside* the file.

**Fix applied** — three parts:

1. New helper [`get_luks_uuid_for_file()`](../luks_tray/Main.py#L84) (next to `generate_uuid_for_file_path`, which is no longer dead and is now only the last-resort fallback):

```python
rc, stdout, _stderr = run_cmd_capture(
        ['blkid', '-o', 'value', '-s', 'UUID', file_path])
return stdout.strip() if rc == 0 else ''
```

2. [`mount_file()`](../luks_tray/Main.py#L1836) resolves the key after a successful mount and hands the backing file to the history:

```python
if not uuid:
    uuid = (container.uuid or get_luks_uuid_for_file(back_file)
            or generate_uuid_for_file_path(back_file))
tray.update_history(uuid, values, back_file=back_file)
```

3. [`update_history()`](../luks_tray/Main.py#L780) gained `back_file=None` (device containers, which omit it, keep an empty `back_file`), sets `vital.uuid`, and now writes `upon` **before** `put_vital()` instead of after, so the mount point is actually persisted on the first save. It also only saves when something changed (`dirty`), covering a `back_file`-only change.

4. [`History.py:296`](../luks_tray/History.py#L296) drops legacy damage on load — keys `None`/`""`/`"None"`/`"null"` are skipped and the file flagged dirty, so an already-polluted `history.json` is rewritten without the `"null"` key.

**Verified** (Qt offscreen, `run_cmd`/`sudo_cmd`/`blkid`/`mount_luks_container` stubbed, real `HistoryClass` on a temp file):

- `get_luks_uuid_for_file()` → UUID for a LUKS file, `''` for an empty path, a missing file and a non-LUKS file (never raises).
- `update_history(uuid, values, back_file=...)` → entry stored **under the LUKS UUID**, JSON contains `back_file` and `upon`, and keys are exactly `[<uuid>]` (no `"null"`); device containers still get `back_file == ''`; repeating an update adds no key.
- `mount_file(uuid=None)` (new/added file) → `update_history` receives the real LUKS UUID and `back_file=/home/joe/Crypts/my.luks`, never `None`; `mount_luks_container` still gets the same `upon`/`luks_file`/`size`.
- `mount_file(uuid=<known>)` (the "Mount Crypt File" layout, which has no `back_file` field) → keeps the container's UUID and `back_file`.
- `restore()` on a hand-written legacy file with a `"null"` key → only the valid entry survives, `dirty` is set, and the re-saved JSON has no `"null"`.

`py_compile` passes on both files; `ruff --select F,E9` still reports the same 8 pre-existing findings.

**Side finding (fixed as critical item 9):** [`_namespaces_to_json_data()`](../luks_tray/History.py#L151) did `legit = vars(vital)` — the live `__dict__` — so the `legit['password'] = ''` clear-text zap also erased the in-memory password of every vital on each save (confirmed: `vitals[<uuid>].password == ''` after a clear-text save). The serialized output was correct, but the remembered password was lost in the running app (dialog prefill) and every subsequent update looked like a change. It now copies: `legit = dict(vars(vital))` plus `entries[uuid] = legit`.

### ✅ 5. Weak master-password KDF — FIXED

[`_password_to_fernet_key()`](../luks_tray/History.py#L170) used a **single unsalted SHA-256** of the password as the Fernet key:

```python
key = hashlib.sha256(self.master_password.encode()).digest()
```

No salt, no iteration count, no memory hardness → trivially brute-forceable offline if `history.json` is obtained.

**Fix applied** — PBKDF2-HMAC-SHA256, 600 000 rounds, fresh 16-byte random salt per save ([`History.py:35`](../luks_tray/History.py#L35), [`_password_to_fernet_key()`](../luks_tray/History.py#L170), [`_encrypt_payload()`](../luks_tray/History.py#L188), [`_decrypt_payload()`](../luks_tray/History.py#L199)):

```python
ENCRYPTED_VERSION = 2
KDF_ITERATIONS = 600_000      # OWASP-style floor for PBKDF2-HMAC-SHA256
KDF_SALT_BYTES = 16
...
salt = os.urandom(self.KDF_SALT_BYTES)
cipher = Fernet(self._password_to_fernet_key(salt))
header = f'v{self.ENCRYPTED_VERSION}:{self.KDF_ITERATIONS}:'.encode()
return header + salt + cipher.encrypt(json_data)
```

Design points:

- **The file header is unchanged** (`{{{ENCRYPTED}}}`); the salt and iteration count are carried in the payload as `b'v2:600000:' + salt + token`. Existing files still look encrypted to every reader, and the parameters can be raised later without a format break.
- **Backward compatible reads:** a payload without the `v2:` prefix is still decrypted with the legacy unsalted SHA-256 key, then `restore()` sets `dirty` so the next `save()` rewrites the file with PBKDF2 ([`History.py:368`](../luks_tray/History.py#L368)). Users are migrated silently on first unlock.
- **Downgrade-safe:** because the marker lives *inside* the header, an older luks-tray opening a v2 file fails authentication and reports `status='locked'` instead of falling through to the clear-text path, where a non-JSON file is treated as corrupt and **re-initialised** (i.e. wiped). Verified: the legacy key raises `InvalidToken` on a v2 payload.
- Salted and unsalted derivations both produce 32 bytes, so `Fernet` is unchanged; no new dependency.

**Verified** (`HistoryClass` on a temp file): v2 round-trip (save → bytes start with header + `v2:600000:` + 16-byte salt, no plaintext password in the file, restore unlocks with all fields intact, `dirty` stays `False`); salted key byte-identical to `pbkdf2_hmac('sha256', pw, salt, 600_000)` and different from the legacy key; wrong password → `locked`, file bytes untouched; no master password → `locked`, file untouched; legacy SHA-256 file → unlocks, data intact, `dirty=True`, next `save()` produces a v2 file that re-unlocks; three consecutive saves use three distinct salts; clear-text path unchanged. Cost measured on this machine: **115 ms** per 600k-round derivation (only on save/restore). `py_compile` passes; `ruff --select F,E9 luks_tray/History.py` → *All checks passed*.

### ✅ 6. Declared Python support was wrong (install/run broke on 3.8–3.11) — FIXED, floor is now 3.11

[`pyproject.toml`](../pyproject.toml#L14) claimed `requires-python = ">=3.8"`, but:

- [Main.py:116](../luks_tray/Main.py#L118) and [Main.py:1318](../luks_tray/Main.py#L1351) used **PEP 701** quote reuse in f-strings (`f'FAIL: {' '.join(args)} ...'`) — a parser error **before Python 3.12** (ruff `invalid-syntax`).
- [Main.py:120](../luks_tray/Main.py#L137) uses PEP 604 `str | None` without `from __future__ import annotations` → import-time `TypeError` on <3.10.

**Fix applied:**

- Both f-strings now use an outer double quote — `f"FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]"` ([Main.py:118](../luks_tray/Main.py#L118), [Main.py:1351](../luks_tray/Main.py#L1351)) — which every supported interpreter parses; a short comment at the first site records why the quote characters must differ.
- PEP 604 annotations need Python ≥3.10, so with the floor at 3.11 nothing has to change: [`run_unmount(..., busy_warns: set) -> str | None`](../luks_tray/Main.py#L137) stays as-is and no `__future__` import is needed.
- [`pyproject.toml`](../pyproject.toml#L14) now declares `requires-python = ">=3.11"` and lists 3.11/3.12/3.13 classifiers.

**Verified:** `ruff --target-version py311 --select F,E9 luks_tray runner` → **0** `invalid-syntax` findings (was 2); the 6 remaining findings are pre-existing `F401` unused imports (`shlex`, `tempfile`, `pathlib.Path`, `io.StringIO`, `QSizePolicy`, `QFontDatabase`). A sweep for other 3.12-only APIs (`itertools.batched`, `typing.override`, `sys.monitoring`, `Path.walk`, PEP 695 `type` aliases/generics) found none. `py_compile` passes for every module on 3.13.

**Caveat:** only CPython 3.13 is installed in this environment, so 3.11 compatibility is established by ruff's `py311` parser, not by execution — note that `ast.parse(..., feature_version=(3, 11))` does **not** catch PEP 701 quote reuse (it accepts the 3.12 tokenizer), so ruff is the authority here. A CI matrix on 3.11 would settle it.

### ✅ 7. No file hardening / non-atomic writes — FIXED (metadata privacy remains by design)

[`save()`](../luks_tray/History.py#L243) wrote `history.json` with the default umask (typically `0644`) and no atomic replace. Consequences:

- ~~A crash mid-write corrupts history.~~
- Even without a master password it stores mount paths, UUIDs and backing-file paths in clear text (privacy metadata by design — see below).

**Fix applied** — all writes now go through [`_write_atomic()`](../luks_tray/History.py#L215):

```python
dirname = os.path.dirname(os.path.abspath(self.path)) or '.'
fd, tmp_path = tempfile.mkstemp(dir=dirname,
        prefix=os.path.basename(self.path) + '.', suffix='.tmp')
with os.fdopen(fd, 'wb' if binary else 'w', ...) as file:
    file.write(data)
    file.flush()
    os.fsync(file.fileno())      # flush before the rename
os.chmod(tmp_path, 0o600)        # mkstemp's default, made explicit
os.replace(tmp_path, self.path)  # same-directory rename => atomic
```

plus a `finally` that removes the temp file if anything failed before the rename.

- **Owner-only:** `mkstemp` creates the file `0600` and the explicit `chmod` keeps it that way through the rename, so a pre-existing world-readable `history.json` is tightened on the next save (the umask no longer matters).
- **Atomic:** the temp file lives in the *same* directory, so `os.replace()` is a single rename — a crash or a short write leaves the previous (valid) file in place.
- **Failure-safe:** a write error returns the usual `'failed saving history: ...'` string, deletes the temp file, and does not touch the real file.

**Verified** (`HistoryClass` in a temp dir with `umask 0o022`): clear-text and encrypted saves both land at mode `0o600`; an existing `0644` file is tightened to `0600`; no `*.tmp` files remain after a successful or a failed save; a simulated exception inside `_encrypt_payload()` leaves the previous file **byte-identical** and returns an error string; saving into a non-existent directory returns an error string instead of raising; encrypted output still starts with `{{{ENCRYPTED}}}` + `v2:600000:` and restores; clear-text content and restore unchanged. The item-5 crypto harness still passes end to end.

**Not changed (deliberate):** clear-text mode still stores UUIDs, mount points and backing-file paths in the JSON — that is the documented purpose of the file (it must be readable without a master password), so "privacy metadata" is a policy decision, not a bug: use a master password, or move the file, if that matters. The `~/.config/luks-tray` directory itself is still created with default (0755) permissions by [`IniTool`](../luks_tray/IniTool.py#L72) — tightening it to `0700` would also cover `config.ini` and `debug.log`, and is left as a separate decision.

### ✅ 8. Destructive cleanup driven by unvalidated config — FIXED

[`remove_unused_automounts()`](../luks_tray/Main.py#L1033) iterated **whatever** `auto_mount_folder` resolved to and ran `sudo rmdir` on every empty subdirectory. If a user set `auto_mount_folder = /home` (or a typo such as `/`), it removed empty directories under that path **with root privileges**.

**Fix applied** — a new gate, [`validate_auto_mount_root()`](../luks_tray/Main.py#L872), is consulted by *both* deletion paths ([`remove_unused_automounts()`](../luks_tray/Main.py#L1039) and [`remove_if_auto()`](../luks_tray/Main.py#L1026)); any returned string means "do nothing, log why". The folder must look like a **dedicated** vault area:

- absolute, at least two levels deep, and not one of the exact system paths in [`FORBIDDEN_AUTO_MOUNT_ROOTS`](../luks_tray/Main.py#L384) (`/`, `/home`, `/tmp`, `/mnt`, `/usr`, …) nor anywhere inside a system tree listed in [`FORBIDDEN_AUTO_MOUNT_PREFIXES`](../luks_tray/Main.py#L395) (`/etc`, `/usr`, `/var`, `/boot`, `/opt`, `/srv`, …). Paths inside the real user's home are exempt from the prefix list, so non-standard layouts (`/var/home/<user>/Vaults`) still work;
- not a symlink (which could point anywhere), not the real user's home, not an ancestor of it, and not inside *another* user's home — `/home` is thus checked as a multi-user tree explicitly (`/home/other/Vaults` is rejected while `~/Vaults` is not), which also holds when the tray's home is `/root`;
- if it exists: a plain directory, not a mount point, owned by the real user ([`get_real_user_ids()`](../luks_tray/Main.py#L843), sudo-aware) **or root** — root stays accepted because an install whose folders were created by `sudo mkdir` has a root-owned `~/Vaults`; the cleanup now repairs that ownership on its next run and, until it succeeds, still removes the empty folders through `sudo -n rmdir` (see the follow-up below) — and not writable by group/other (which also rejects `/tmp`).

Cleanup starts unprivileged: [`remove_empty_dir()`](../luks_tray/Main.py#L835) uses plain `os.rmdir` (which refuses non-empty directories), skipping symlinks and mount points, and only retries with `sudo -n rmdir` when the *parent* is not writable -- which, after the follow-up below, only happens on a root that predates this fix. The subdirectory loop also skips dot-entries, symlinks and directories owned by a third uid, and reports each removal via `prt`. Containment is now checked by [`is_strictly_within()`](../luks_tray/Main.py#L994) instead of `os.path.commonpath(...) == parent`, which treated "target == parent" as "inside the parent".

**Verified** (`QT_QPA_PLATFORM=offscreen`, `LuksTray.singleton` stubbed with a fake `ini_tool` — that harness is older than medium #8, which removed the singleton; `fix16`/`cleanup` were migrated to pass a stand-in tray —, real temp directories):

- Rejected: `/`, `/home`, `/tmp`, `/etc`, `/usr`, `/var`, `/mnt`, `/boot`, `/dev`, `/proc`, `/sys`, `/root`, the home directory itself, `/etc/luks`, `/usr/local/Vaults`, `/var/lib/Vaults`, `/opt/Vaults`, `/srv/Vaults`, `/root/Vaults`, `/home/notjoe/Vaults`, a symlinked root, a `0777` root, and a regular file used as the root.
- Accepted: the shipped default `~/Vaults` (= `/home/joe/Vaults`, root-owned `0755`), a not-yet-existing `~/Vaults`-style path, `~/Vaults` written without expansion, `/mnt/Vaults`, `/tmp/<random>/Vaults`, and — with `HOME=/root` — `/root/Vaults` (while `/home/<any-user>/Vaults` stays rejected).
- Cleanup on a valid root: exactly the one empty subdirectory removed; a non-empty directory, a regular file, a dot-directory and a symlink were all left untouched, and the symlink's **target** outside the root was not deleted.
- Hostile configs: `auto_mount_folder=/home` and `/` both log `WARN: skipping auto-mount cleanup: …` and return `0` (nothing inspected, nothing deleted); an invalid (group-writable) root disables both `remove_unused_automounts()` and `remove_if_auto()` while the child directories survive.
- `remove_if_auto()`: `True` for an empty dir inside the root (and the dir is gone), `False` for the root itself, for a path outside the root, and for any path when the root fails validation.

`python -m py_compile` passes; `ruff check --select F,E9 luks_tray/Main.py` still reports the same 6 pre-existing `F401` unused imports and nothing new. **Trade-off:** on a folder that fails validation, stale empty mount directories are no longer cleaned (a log line explains why) — a safe failure, and the shipped default passes all checks.

**Follow-up, in three rounds, each found by running the split tree as an ordinary user:** round one added a writability check to [`remove_unused_automounts()`](../luks_tray/Main.py#L893), because `rmdir` needs write+execute on the *parent*, not ownership of the child; that collapsed a per-directory `note: not removing '...': [Errno 13] Permission denied` storm into a single note. That note was still the symptom: mount points *were* created with `sudo mkdir -p`, so `~/Vaults` (created by the `-p` itself) and every folder below it were root-owned `0755`, and the unprivileged cleanup could never remove anything at all. Round two removed the cause: `ensure_dir()` in [`LuksOps.py`](../luks_tray/LuksOps.py#L124) creates mount points **as the user** (`sudo mkdir -p` only as the fallback for a path outside the user's tree), and both dialogs reach it through [`mount_luks_container()`](../luks_tray/LuksOps.py), so every folder the cleanup is expected to remove belongs to the process that removes it; [`generate_auto_mount_folder()`](../luks_tray/Main.py#L768) also reports a failed root `mkdir` instead of handing out mount points inside a directory that does not exist. Round three handles installs that already carry the damage, without asking the user to run anything: the new [`ensure_auto_mount_root()`](../luks_tray/Main.py#L732) hands a root that is not owned by the real user back to them with one **non-recursive** `chown` -- never `-R`, which would rewrite ownership inside whatever is mounted there -- and only after [`validate_auto_mount_root()`](../luks_tray/Main.py#L642) accepts the directory, so a path read from config.ini cannot turn it into a privileged chown of an arbitrary tree; and [`remove_empty_dir()`](../luks_tray/Main.py#L835) retries with `sudo -n rmdir` when `rmdir` hits `EACCES`/`EPERM`. That is the same escalation the rest of the mount handling already uses, it does not widen *what* may be deleted (`rmdir` still refuses non-empty directories and mount points), and it says nothing when it works. Folders that are not empty are never passed to `rmdir` at all, so a normal in-use vault no longer produces a note on every startup, and [`remove_if_auto()`](../luks_tray/Main.py#L880) needs no special case either. Verified with `/tmp/luks_fix16_check.py` (28 checks: created dirs are user-owned, sudo used only on `EACCES` and never before the mount point exists, the ownership repair gated by validation and never recursive, empty vs non-empty cleanup, both removal paths escalating exactly once per folder under an unwritable root, and a single `note:` only when the escalation itself fails); `/tmp/luks_cleanup_check.py` and `/tmp/luks_split_check.py` still pass, and `py_compile`, `ruff --select F,E9` and `ruff --select F401,F811,F841` are clean. On this machine, whose `~/Vaults` was `drwxr-xr-x root root 0755`, the pass now reports `removed unused auto-mount folder '/home/joe/Vaults/nvme0n1p5'`, leaves the live `nvme0n1p3` mount alone, and ends with the root owned by `joe` again -- a mount point created afterwards belongs to uid 1000 and needs no privilege to create or remove.

### ✅ 9. In-memory password wiped as a side effect — FIXED

[`_namespaces_to_json_data()`](../luks_tray/History.py#L151):

```python
legit = vars(vital)          # alias to vital.__dict__
legit['password'] = ''       # mutates the live object
entries[uuid] = vars(vital)  # same dict again
```

`vars()` hands back the namespace's **own** `__dict__`, so blanking the password "for the clear-text file" also erased it from the live object in `self.vitals`, and `entries[uuid]` was that same dict again. Each clear-text save therefore destroyed the remembered password:

- the next "Mount …" dialog prefilled an empty password ([`Main.py:1663`](../luks_tray/Main.py#L1663), [`Main.py:1829`](../luks_tray/Main.py#L1829));
- [`update_history()`](../luks_tray/Main.py#L823) compared `values['password'] != vital.password` against `''`, so it saw a change and re-saved on every call.

**Fix applied** — [`_namespaces_to_json_data()`](../luks_tray/History.py#L151) now copies each vital into a plain dict and serializes the copy:

```python
for uuid, vital in self.vitals.items():
    legit = dict(vars(vital))  # copy, never the live namespace
    if not self.master_password:
        legit['password'] = ''  # never persist it in clear text
    entries[uuid] = legit
```

The password is still withheld from a clear-text file (that part is deliberate — see item 7: the history is readable without a master password and holds only metadata), and with a master password it is still stored encrypted in the item-5 `v2` payload. Nothing else changes: same keys, same JSON shape, same `save()` contract — the live `SimpleNamespace` is simply no longer a serialization side effect.

**Verified** (`HistoryClass` on temp files, with and without a master password; 34 assertions, all passing):

- the returned structure holds plain `dict`s that are neither `vars(vital)` nor shared between entries; mutating it (`entries[uuid]['upon'] = ...`) leaves the live `vital.upon`/`vital.password` untouched;
- clear-text `save()` returns `None` and the file stores `"password": ""` while `vital.password` **and** `hist.vitals[uuid].password` still hold the real password; a repeated save keeps it, and the `update_history()` comparison now reports "unchanged" instead of "changed";
- encrypted `save()` produces `{{{ENCRYPTED}}}` + `v2:600000:`, leaks neither the password nor any plaintext (`password.encode() not in <file bytes>`), the decrypted payload does carry it, and a fresh instance's `restore()` returns `True` with `status == 'unlocked'` and the password recovered;
- clear-text `restore()` yields an empty password — by design, it was never stored;
- three vitals plus a duck-typed namespace all serialize to distinct copies and no live object is altered;
- swapping the **old** implementation back in reproduces the defect (live password `''`, dialog prefill `''`, `entries[uuid] is vars(vital)` → `True`), so the harness really exercises the bug.

`python -m py_compile` passes for every module; `ruff check --select F,E9` is unchanged at the 6 pre-existing `F401`s (none in `History.py`); the item-8 harness still passes (`FAILURES: none`), so the two fixes are independent.

**Note:** the audit's suggested one-liner (`legit = dict(vars(vital))`) would not have been sufficient on its own — `entries[uuid] = vars(vital)` also had to become `entries[uuid] = legit`, otherwise the clear-text entry handed to `json.dumps()` would still have been the live dict with its password blanked in place.

---

## 🟠 High — functional bugs

| # | Location | Issue |
|---|----------|-------|
| 1 | ✅ [`browse_folder()`](../luks_tray/Main.py#L1313) | ~~`self.home_dir()` calls a **string** (`home_dir` is a `str` set by [`get_real_user_home_directory()`](../luks_tray/Main.py#L1301)) → `TypeError` when the mount field is cleared.~~ **Fixed** — the empty-field fallback now calls `self.get_real_user_home_directory()`, which returns the `str` (and lazily initializes it). |
| 2 | ✅ [`update_history()`](../luks_tray/Main.py#L780) | ~~Sets `vital.upon` **after** `put_vital()` (which saves), so a new mount point is not persisted on that save.~~ **Fixed with critical item 4** — `upon` and `back_file` are now written before the single `put_vital()`. |
| 3 | ✅ [`check_upon()` callers](../luks_tray/Main.py#L1714) | ~~`mount_points = tray.update_mounts()` returns the **device key set** ([Main.py:563](../luks_tray/Main.py#L563)), not mount points, yet [`check_upon`](../luks_tray/Main.py#L1415) treats it as mount points (`text in mount_points`). Duplicate-mount detection is broken; pass `tray.upons`.~~ **Fixed** — both callers ([`mount_device()`](../luks_tray/Main.py#L1714), [`mount_file()`](../luks_tray/Main.py#L1985)) now call `tray.update_mounts()` for its side effect and pass `tray.upons`; `check_upon()` documents the required argument. |
| 4 | ✅ [`add_input_field()`](../luks_tray/Main.py#L1222) | ~~With a scalar label and multiple `keys`, it does `placeholder_texts.append(text_str)` while `placeholder_texts` may still be a `str` → `AttributeError`. Normalize both to lists first.~~ **Fixed** — `label_texts` and `placeholder_texts` are normalized to lists up front, single values are broadcast to every key, and mismatched lengths now raise a descriptive `ValueError` instead of an `IndexError`. |
| 5 | ✅ [`mount_luks_container()`](../luks_tray/Main.py#L1525) | ~~`luksFormat` runs **without `sudo`** (unlike everything else) and uses `check=True`, so the `if sub.returncode != 0` branch is unreachable and real stderr is swallowed by the outer `except` → generic "An error occurred". Convert to [`sudo_run()`](../luks_tray/Main.py#L132) (see #2).~~ **Fixed** — `luksFormat` now runs through [`sudo_run()`](../luks_tray/Main.py#L132) (`sudo -n`, `check=False`), and its real `rc`/stderr is returned immediately rather than overwritten by the later `_unlock_luks()` call. |
| 6 | ✅ [`mount_device()`](../luks_tray/Main.py#L1714) | ~~`luks_device = ''` when `len(container.filesystems) != 1`; `cryptsetup open ... ''` then fails with a confusing error.~~ **Fixed** ([Main.py:1747](../luks_tray/Main.py#L1747)) — the mapper name is taken from the single named child filesystem, otherwise derived as `f'{container.name}-luks'` (same convention as [`mount_luks_container()`](../luks_tray/Main.py#L1579)); an empty or missing `container.filesystems` can no longer reach `cryptsetup open`. |
| 7 | ✅ [`MountDeviceDialog`](../luks_tray/Main.py#L1661) | ~~Open-state branch renders `f'Close {",".join(mounts)}?'` where `mounts` is `[]` ([Main.py:1427](../luks_tray/Main.py#L1427)) → "Close ?".~~ **Fixed** ([Main.py:1680](../luks_tray/Main.py#L1680)) — the close branch (which by definition has no mount points) now names the mapper devices, falling back to `container.name`, so it never renders a bare "Close ?". |
| 8 | ✅ Error sentinel idiom | ~~`errs.append(f'{container.name}')` then `if len(errs) <= 1` treats the first element as a placeholder ([Main.py:1452](../luks_tray/Main.py#L1452), 1509, 1678). Use a plain `errs = []` and `if not errs`.~~ **Fixed** — the placeholder append is gone from [`mount_device()`](../luks_tray/Main.py#L1720), [`unmount_device()`](../luks_tray/Main.py#L1793) and [`mount_file()`](../luks_tray/Main.py#L1968); every `len(errs) <= 1` / `len(errs) > 1` test is now `if not errs` / `if errs` (e.g. [Main.py:1769](../luks_tray/Main.py#L1769), [1828](../luks_tray/Main.py#L1828), [2048](../luks_tray/Main.py#L2048)), and the container-name heading is prepended to [`alert_errors()`](../luks_tray/Main.py#L1389) only at the point of reporting. Side effects: a single error is now reported (the old off-by-one swallowed exactly one error), and `unmount_device()` no longer dereferences a `None` container (it reports "container not found"). |
| 9 | ✅ [CLI paths](../luks_tray/Main.py#L2119) | ~~`--edit-config` / `--follow-log` use `IniTool(paths_only=True)`, which skips [`ensure_ini_file()`](../luks_tray/IniTool.py#L76). On a fresh install the folder/file may not exist.~~ **Fixed** — [`ensure_folder()`](../luks_tray/IniTool.py#L70) was split out of `ensure_ini_file()` (which now calls it), `--edit-config` calls `ensure_ini_file()` ([Main.py:2125](../luks_tray/Main.py#L2125)) so `$EDITOR` always gets an existing `config.ini`, and `--follow-log` calls `ensure_folder()` and creates an empty log file ([Main.py:2142](../luks_tray/Main.py#L2142)) so `tail -F` does not report a missing file on first run. `paths_only=True` itself remains side-effect free, so `main()`'s normal startup path is unchanged. |
| 10 | ✅ Runtime `assert` | ~~Validation via `assert` at [Main.py:398](../luks_tray/Main.py#L398), [434](../luks_tray/Main.py#L434), [802](../luks_tray/Main.py#L802), [819](../luks_tray/Main.py#L819), [1286](../luks_tray/Main.py#L1286). Under `python -O` these vanish, so missing deps/icon-copy failures continue half-initialized.~~ **Fixed** — all seven `assert` statements in [`Main.py`](../luks_tray/Main.py) (and the `assert False` in [`IniTool.update_config()`](../luks_tray/IniTool.py#L179)) are now real validation. Missing system commands ([Main.py:437](../luks_tray/Main.py#L437)) and missing tray icons ([Main.py:478](../luks_tray/Main.py#L478)) raise `RuntimeError`, so the tray fails in `main()`'s handler instead of running half-initialized; the same runs for `generate_auto_mount_folder()` ([Main.py:987](../luks_tray/Main.py#L987), [1004](../luks_tray/Main.py#L1004)); `add_input_field()` raises `ValueError` for an unknown `field_type` ([Main.py:1297](../luks_tray/Main.py#L1297), which also no longer leaves `input_field` unbound); `mount_luks_container()` returns its usual error string when `upon` is empty ([Main.py:1557](../luks_tray/Main.py#L1557)) instead of letting a bogus mount point reach `mount`; `mount_file()` reports "tray is not initialized" through `alert_errors()` ([Main.py:1996](../luks_tray/Main.py#L1996)); and an unsupported config golden type now keeps the default and logs the skip ([IniTool.py:179](../luks_tray/IniTool.py#L179)). Verified under `python3 -O`, where the old `assert`s were stripped. |
| 11 | ✅ [`_unlock_luks()`](../luks_tray/Main.py#L1493) | ~~`if hasattr(self, 'opened') and self.opened` — `self` is a dialog, which never has `opened`; the "already unlocked" guard is dead code.~~ **Fixed** — the guard is gone from [`_unlock_luks()`](../luks_tray/Main.py#L1493), and the identical unreachable test in [`_setup_loop_device()`](../luks_tray/Main.py#L1519) went with it. `CommonDialog` never defines `opened` (only [`DeviceInfo`](../luks_tray/Main.py#L209) container namespaces do), so both branches could never fire; the `cryptsetup open` / `losetup` code beneath them now always runs. It was deliberately *not* replaced with a live "mapper path exists → skip" test: that would skip the password check and mount a volume another user already has open. |
| 12 | ✅ [stdin redirect](../luks_tray/Main.py#L2234) | ~~Replaces `stdin` with `/dev/null`; acceptable, but combined with no `subprocess` timeouts, a hung `cryptsetup` freezes the UI indefinitely.~~ **Fixed** — the redirect itself stays (it stops a child from stealing the shell's keystrokes; the reason is now spelled out at [Main.py:2226](../luks_tray/Main.py#L2226)), but every external command now has a deadline. [`run_cmd_capture()`](../luks_tray/Main.py#L141) forwards a `timeout` defaulting to `CMD_TIMEOUT` = 60 s ([Utils.py:29](../luks_tray/Utils.py#L29)), kills the child on overrun and reports `TIMEOUT_RC` = 124 ([Utils.py:40](../luks_tray/Utils.py#L40)) with a `timed out after Ns` message, so [`run_cmd()`](../luks_tray/Main.py#L107)/`sudo_cmd()`/`sudo_run()` ([Main.py:163](../luks_tray/Main.py#L163)) render it as their usual `FAIL: … [rc=124]` string and no caller needs a new branch. Read-only probes use `PROBE_TIMEOUT` = 15 s ([Utils.py:33](../luks_tray/Utils.py#L33)): `lsblk` keeps the previous snapshot and logs ([Main.py:333](../luks_tray/Main.py#L333), [339](../luks_tray/Main.py#L339)), `fuser` falls back to "(Could not get process info…)" ([Main.py:199](../luks_tray/Main.py#L199)), and `blkid` treats the entry as unverifiable ([History.py:284](../luks_tray/History.py#L284)). `umount` ([Main.py:177](../luks_tray/Main.py#L177)) is bounded too, and the one-shot setup commands use `FORMAT_TIMEOUT` = 900 s ([Utils.py:37](../luks_tray/Utils.py#L37)): `luksFormat` ([Main.py:1639](../luks_tray/Main.py#L1639)) and `mkfs.ext4` ([Main.py:1669](../luks_tray/Main.py#L1669)). Verified with a harness that really runs a hanging child: at `timeout=0.4` the call returns `rc=124` with the timeout text (and partial stdout coerced to `str`), stdin piping for `--key-file -` still works, `run_unmount`/`parse_lsblk` survive a hung `umount`/`lsblk`, and an AST scan shows no `subprocess.run` left in `Main.py`/`History.py` without `timeout=`. |

---

## 🟡 Medium — design, performance, quality

1. ✅ **UI-thread blocking & re-entrancy.** The 3 s `QTimer` ([Main.py:545](../luks_tray/Main.py#L550)) runs [`update_menu()`](../luks_tray/Main.py#L647): opens `/proc/mounts`, spawns `lsblk`, reloads/decrypts history, and runs `blkid` per backing file. ~~[`show_progress()`](../luks_tray/Main.py#L1505) calls `QApplication.processEvents()`, so the timer can **re-enter** `update_menu` mid-mount. Move polling to a worker thread and remove `processEvents`.~~ **Fixed (re-entrancy)** — `update_menu()` is now guarded: it returns immediately if a refresh is already running (`self.refreshing`, [Main.py:659](../luks_tray/Main.py#L659)) or if a privileged dialog action is in flight, which is what [`busy_operation()`](../luks_tray/Main.py#L690) marks around the mount/unmount work in both dialogs ([Main.py:1898](../luks_tray/Main.py#L1898), [1928](../luks_tray/Main.py#L1928), [2062](../luks_tray/Main.py#L2062), [2198](../luks_tray/Main.py#L2198)). Both flags are cleared in `finally`, so an exception cannot park the poller. `show_progress()` no longer spins the event loop — it activates the layout and calls `repaint()` ([Main.py:1527](../luks_tray/Main.py#L1528)), painting the progress widgets *without* dispatching the timer tick or a queued click; `processEvents` no longer appears in the code. Polling is still synchronous, but every command it runs is bounded by the deadlines from high item #12, and the per-refresh `blkid` cost is gone (see #2); moving it to a worker thread is now a latency refactor, not a correctness fix, and stays in the fix order.
2. ✅ **`blkid` per file per refresh.** [`_json_data_to_namespaces()`](../luks_tray/History.py#L353) ~~shells out `blkid` for every `back_file` on each restore (every 3 s). Cache by `(path, mtime)`.~~ **Fixed** — the probe now lives in [`_probe_back_file_uuid()`](../luks_tray/History.py#L293) and remembers its answer against [`_file_stamp()`](../luks_tray/History.py#L281) = `(st_mtime_ns, st_size)`, in a per-instance `_back_file_uuid_cache` ([History.py:55](../luks_tray/History.py#L55)) bounded by `BLKID_CACHE_MAX` = 64 ([History.py:41](../luks_tray/History.py#L41)). Writing, truncating or re-formatting a backing file moves at least one of those fields, so a remembered UUID cannot outlive the header it describes — and the re-parse that follows every history write ([History.py:377](../luks_tray/History.py#L377)) no longer forks `blkid` for files that did not change. Negative answers *are* cached when blkid says so cleanly (rc=2 "no signature"), but a timeout, a path that cannot be `stat`ed or an rc=2 carrying a complaint (permissions, I/O) is treated as **no verdict** and retried on the next refresh, so a momentarily busy disk can never purge a valid entry. Verified against a real LUKS2 backing file with the real `blkid`: six full re-parses of the history cost 2 executions in total (one per `back_file`), touching the container caused exactly one re-probe, and a `timeout`/permission-style failure re-probes every time.
3. ✅ **Subprocess deadlines** (fixed with high item #12): `run_cmd_capture()`, `run_unmount()`, `parse_lsblk()` and `History`'s `blkid` all pass `timeout=`; one hung command can no longer stall the tray indefinitely.
4. ✅ **Silent failure.** ~~16 broad `except Exception` and several `try/except/pass` ([Main.py:845](../luks_tray/Main.py#L845), [:847](../luks_tray/Main.py#L847), [Utils.py:70](../luks_tray/Utils.py#L70)). At minimum log via `prt`.~~ **Fixed** — every handler now reports what it caught. Handlers on the 3 s refresh path log through the new [`Utils.prt_once()`](../luks_tray/Utils.py#L137), which prints at most once per failure key (bounded by `PRT_ONCE_MAX`, [Utils.py:27](../luks_tray/Utils.py#L27)) so a repeating condition cannot rotate the 512 KB debug log; [`prt()`](../luks_tray/Utils.py#L122) gained an `_above` hint so the `[file:line]` footer still names the handler instead of the wrapper. Wired into: the `/sys/.../device/model` read ([Main.py:280](../luks_tray/Main.py#L280), narrowed to `(OSError, UnicodeDecodeError)` from `(FileNotFoundError, Exception)` at [Main.py:274](../luks_tray/Main.py#L274)), the loop `backing_file` probe ([Main.py:312](../luks_tray/Main.py#L312), [315](../luks_tray/Main.py#L315)), `get_real_user_ids()` ([Main.py:965](../luks_tray/Main.py#L965)), a `SUDO_USER` with no passwd entry ([Main.py:983](../luks_tray/Main.py#L983)), `is_strictly_within()` ([Main.py:1123](../luks_tray/Main.py#L1123)), `remove_empty_dir()` ([Main.py:1151](../luks_tray/Main.py#L1151)) and the cleanup `stat()` ([Main.py:1210](../luks_tray/Main.py#L1210)), `check_upon()`'s `abspath` ([Main.py:1590](../luks_tray/Main.py#L1590)) and `History._file_stamp()` ([History.py:295](../luks_tray/History.py#L295)). `mount_luks_container()`'s catch-all logs the exception **and its traceback** before returning the one-liner the dialog shows ([Main.py:1759](../luks_tray/Main.py#L1759)); `main()` reports through `prt()` instead of `print()` ([Main.py:2358](../luks_tray/Main.py#L2358)); `History._write_atomic()` reports a leftover temp file ([History.py:249](../luks_tray/History.py#L249)); and a failed decryption now names the exception, so a typo can be told from a truncated file ([History.py:449](../luks_tray/History.py#L449)). Catches that only shadowed a provable exception set were narrowed (`as_int`, `is_strictly_within`, `check_upon`, `size_str`, `IniTool.to_array`, `IniTool`'s `int()`). Two handlers stay silent **by necessity** — the `is_tty()`/`is_reg()` probes inside `prt()` itself ([Utils.py:88](../luks_tray/Utils.py#L88), [93](../luks_tray/Utils.py#L93)), where logging would recurse — and the reason is now written next to them. Verified with a harness that walks every `ExceptHandler` in the four modules (35 handlers, **0 silent**, 10 broad `except Exception`, down from 16) and drives the real paths: five polls of a missing `/sys` node produce one log line, `remove_empty_dir()` reports a non-empty directory once and an empty one not at all, and a corrupt encrypted history is logged with the exception text.
   **Follow-up — an expected gap is not an event.** The `prt_once()` pass above made every `/sys` read say something at least once per device, and the first line a user ever saw was `note: cannot read /sys/class/block/zram0/device/model: [Errno 2] …`, which reports nothing wrong: `zram0` is swap held in RAM, and zram, device-mapper, loop, md and nbd nodes have no `device/` subdirectory at all (here only `nvme0n1`, `nvme1n1`, `sda`, `sdb`, `sdc` and `sr0` have one). `''` is the correct model for those, so [`get_str()`](../luks_tray/DeviceInfo.py#L70) now returns it *silently* for `FileNotFoundError`/`NotADirectoryError`, and [`get_backing_file()`](../luks_tray/DeviceInfo.py#L133) does the same for a loop with no `loop/backing_file` node. A node that *exists* but cannot be read stays reportable, because there the consequence is worth naming — the device is listed without a model: `WARN: cannot read the model of 'sda' (…): [Errno 13] Permission denied; listing it without one`, still once per node. The lookup is also skipped for devices that can never reach the menu: [`parse_lsblk()`](../luks_tray/DeviceInfo.py#L187) reads a model only for top-level devices that have children, which drops a dozen discarded `/sys` opens per 3 s tick (`zram0`, `dm-0`, `dm-1`, `loop0-7`). Verified with `/tmp/luks_fix17_check.py` (25 assertions): a missing node and `zram0` both return `''` with no output, a real disk still yields its model silently, `PermissionError` and `UnicodeDecodeError` produce exactly one explained `WARN` each, the model is read for `sdc` but not for childless `zram0`/`dm-0`/`loop9`, a loop without `backing_file` is silent, and a full `parse_lsblk()` against the live `/sys` and `lsblk` prints nothing at all. `py_compile` and both ruff selections pass, and `luks_fix16`, `luks_cleanup` and `luks_split` still report `FAILURES: none`.

5. ✅ **Monolith.** [`Main.py`](../luks_tray/Main.py) was 2,365 lines mixing device discovery, LUKS ops, and 5 dialog classes. **Split by concern:**
   - [`DeviceInfo.py`](../luks_tray/DeviceInfo.py) (245 lines) — `DeviceInfo`, the `lsblk -J` parse and the banned-mount-point policy;
   - [`LuksOps.py`](../luks_tray/LuksOps.py) (336 lines) — `run_cmd_capture()` / `run_cmd()` / `sudo_cmd()` / `sudo_run()` / `run_unmount()`, `get_luks_uuid_for_file()`, the LUKS primitives (`_unlock_luks()`, `_mount_manual()`, `_setup_loop_device()`) and `mount_luks_container()`, which is now `mount_luks_container(tray, container, password, …, dot_vault_dir=None)` instead of a `CommonDialog` method;
   - [`dialogs/`](../luks_tray/dialogs/__init__.py) — `Common.py` (444 lines: `CommonDialog`, `requires_manual_title()`), `MasterPassword.py` (71), `MountDevice.py` (231), `MountFile.py` (305).

   [`Main.py`](../luks_tray/Main.py) is now **997 lines** (983 after the split, plus the docstrings from medium #6, minus the dead code removed in #7): `LuksTray` (menu building, refresh policy, `update_history()`), the auto-mount folder policy (`get_auto_mount_root()` … `expand_real_user()`) and `main()`.

   **Design:** each dialog takes the tray as a constructor argument (`CommonDialog(tray)`, `MountDeviceDialog(tray, container)`, `MountFileDialog(tray, container, create=False)`), so `luks_tray.dialogs` never imports `luks_tray.Main` and there is no circular import. `CommonDialog.check_upon()` became an instance method reading `self.tray.get_auto_mount_root()`, and policy helpers are reached through the tray (`tray.remove_if_auto()`). The old module-level names remain importable from `main` — its `__all__` re-exports `run_cmd`, `sudo_cmd`, `run_cmd_capture`, `sudo_run`, `run_unmount`, `get_luks_uuid_for_file`, `generate_uuid_for_file_path`, `DeviceInfo`, all dialogs and `requires_manual_title` (`IS_SWAY_LIKE_ENV` was dropped in medium #8) — so [`runner`](../runner) and ad-hoc harnesses are unaffected.

   **Drive-by fix:** [`MountFileDialog.mount_file()`](../luks_tray/dialogs/MountFile.py#L245) indexed `values['readonly']`, but the "Create New Crypt File" layout never adds that checkbox → `KeyError: 'readonly'` on the create path; it now reads `values.get('readonly', False)`.

   **Verified** (`QT_QPA_PLATFORM=offscreen`, fake tray, 29 assertions): every dialog builds in all its branches (mount/close/unmount device, add/create/mount/unmount file); `mount_device()` / `mount_file()` forward the container, `upon`, `luks_device`, `luks_file`, `size` and `dot_vault_dir` to `mount_luks_container()`; the file path records `back_file` in the history; `unmount_device()` / `unmount_file()` emit `cryptsetup close` / `losetup -d` and prune automount dirs through the tray; the "opened but no filesystem" anomaly still reports its actionable error; `check_upon()` still rejects banned, relative and auto-root paths. `python -m py_compile` passes for every module and `ruff check --select F,E9 luks_tray runner` → *All checks passed*.
6. ✅ **`TBD` docstrings and stale commented-out code.** ~~26 `TBD` docstrings ([Main.py:90](../luks_tray/Main.py#L90) etc.) and stale commented-out code ([Main.py:51](../luks_tray/Main.py#L51), [285](../luks_tray/Main.py#L285), [380](../luks_tray/Main.py#L380)).~~ **Fixed** — every `TBD` is gone (the split had moved them into the new modules). Written: `LuksTray`, `update_mounts()`, `is_mounted()`, `merge_containers_history()`, `update_menu_items()`, `replace_menu_if_different()` + its `replace_menu()` / `get_action_text()`, `handle_add_file_click()`, `handle_create_file_click()`, `generate_auto_mount_folder()` + its `available()`, `get_real_user_ids()` + its `as_int()` and `main()` ([Main.py](../luks_tray/Main.py)); `make_partition_namespace()`, `get_device_vendor_model()`, `parse_lsblk()`, `get_relative()` and its `get_str()` / `get_backing_file()` / `eat_one()` ([DeviceInfo.py](../luks_tray/DeviceInfo.py)); `CommonDialog`, `add_line()`, `add_push_button()` ([Common.py](../luks_tray/dialogs/Common.py)); all three `MasterPasswordDialog` methods; `MountDeviceDialog` (`mount_device()`, `unmount_device()`); `MountFileDialog` (`mount_file()`, `unmount_file()`); both module headers ([History.py](../luks_tray/History.py), [IniTool.py](../luks_tray/IniTool.py)) and `IniTool.update_config()`'s `to_array()`; the four `prt()` helpers in [Utils.py](../luks_tray/Utils.py); and [runner](../runner). Each says what the function does, what it returns and *why* the non-obvious part is there (the errors-only `errs` convention, `busy_operation()` around mount/close, keying file entries by the LUKS UUID, the Wayland/CSD workarounds). Two of them (`show_partition_details()` and `rerun_module_as_root()`) were written and then deleted as dead code under #7.

   **Stale code removed** (~45 lines): the abandoned `QMenu`-building path (13 `# menu.…` lines in `update_menu_items()`), the `QFontDatabase`/emoji-font attempt in `LuksTray.__init__()`, `# ??? self.load_data()`, `# os.makedirs(...)` next to the `run_cmd(['mkdir', ...])` that replaced it, both `# self.setFixedSize(300, 200)`, the four commented-out `Hide` buttons, `# self.accept()`, `# mainBasic()`, `# os.chdir(...)`, the `# prt(...)` debug lines, the disabled `upon.startswith(('/run/', '/media/'))` filter in `History.ensure_container()` (its prose claimed a filtering the code does not do), the `StringIO` dump in `parse_lsblk()`, two empty `# pylint: disable=` lines, and the commented-out call to `History._load_initial_state()` (the method itself is gone, #7). Comments that explain a decision were kept; a few vague ones (`# mounts if there are`, "optionally you can also set entry.upon here") were replaced by the reason the code does what it does.

   **Verified:** `grep -rn TBD` over the package → 0 hits. An AST scan of all 126 definitions in `luks_tray/` + `runner` finds a docstring everywhere except the eight `__init__`s (conventional, and each class's docstring covers its arguments) and the empty [`luks_tray/__init__.py`](../luks_tray/__init__.py) package marker. `python -m py_compile` passes, `ruff check --select F,E9 luks_tray runner` → *All checks passed*, and both harnesses (the 29-assertion dialog run and the auto-mount cleanup run) still report `FAILURES: none`.
7. ✅ **Dead code / unused imports.** ~~`rerun_module_as_root()`, `Utils.copy_to_folder()`, `History._load_initial_state()`, `LuksTray.show_partition_details()` and the `udisks_cmd` plumbing, plus the original unused-import list (`shlex`, `Path`, `QSizePolicy`, `QFontDatabase`).~~ **Fixed** — all five are gone, and the import list had already stopped being true (the split left ruff with nothing to report).
   - **`rerun_module_as_root()`** ([Main.py](../luks_tray/Main.py)) — nothing called it. Gone; `main()` is now the last definition in the module.
   - **`LuksTray.show_partition_details()`** — never wired into the menu *and* broken: `details += 'UUID={container.UUID}\n'` had no `f` prefix and containers carry `uuid`, not `UUID`, so the only thing it showed was the literal text. Removed rather than repaired, because a details dialog nobody opens is not worth keeping a broken API alive for.
   - **`Utils.copy_to_folder()`** — only ever mentioned in a comment. Removed, along with `import importlib.resources as pkg_resources`, which existed solely for it; `importlib.resources.as_file()` + `shutil.copy2()` in `LuksTray.__init__()` does the job. `shutil` stays imported in [`Utils.py`](../luks_tray/Utils.py) because `prt()` uses `shutil.move()` to rotate the debug log.
   - **`History._load_initial_state()`** — medium #6 deleted its commented-out call and `restore()` sets `last_mtime`/`file_existed` itself, so nothing referenced it; removed, with the `__init__` defaults left untouched.
   - **`udisks_cmd` plumbing** — `check_dependencies()` discovered the `${udisks…}` command that a commented-out candidate list once hoped for, returned it as a third tuple element, and `LuksTray.__init__()` stored it in `self.udisks_cmd` — which no code ever read. The chain is gone: the function returns `(found, missing)`, both call sites unpack two values, and the `isinstance(entry, list)` "one of these candidates" support went with the dead entry. Dropping the duplicated `bindfs` from the same list closes the first half of #9 (ten commands, each listed once — #9 itself later took the list to fifteen).
   - **Imports:** `QMessageBox` dropped from `Main.py` (only `show_partition_details()` used it). `ruff check --select F401,F811,F841 luks_tray runner` reports nothing, so the deletions left no dangling import.

   **Verified:** `grep -rn` for the five names over `luks_tray/`, `runner` and the `/tmp` harnesses → 0 hits (the only `udisks` left in the tree is README's "No udisks2 integration" feature note, which is still accurate). `python -m py_compile` passes and `ruff check --select F,E9 luks_tray runner` → *All checks passed*. A direct assertion confirms `check_dependencies()` returns a 2-tuple with `missing == []` and `bindfs` exactly once, and `luks-tray --check-deps` prints all fifteen `✓` lines and exits 0 — that smoke test is what caught the first pass missing the second unpacking (`ValueError: not enough values to unpack`, [Main.py:954](../luks_tray/Main.py#L954)). Both harnesses still report `FAILURES: none` (29 dialog assertions and the auto-mount cleanup run). Sizes after the cleanup: `Main.py` 997 lines, `Utils.py` 149, `History.py` 474.
8. ✅ **Global mutable state:** ~~`LuksTray.singleton`, module-level `IS_SWAY_LIKE_ENV`, `Utils.prt_path`, and `IniTool.__init__` calling `os.chdir()`~~. **Fixed — all four are gone.** They were not equally serious; the `chdir` was the only one that could damage something, and it is the one the other three were propping up.

   - **`LuksTray.singleton` → instance methods.** The class attribute, its assignment in `__init__` and the helpers that read it are gone: [`get_auto_mount_root()`](../luks_tray/Main.py), [`generate_auto_mount_folder()`](../luks_tray/Main.py) and [`remove_if_auto()`](../luks_tray/Main.py) are plain methods on the tray (`self.ini_tool`, `self.upons`, `self.history`), and [`validate_auto_mount_root()`](../luks_tray/Main.py) / [`ensure_auto_mount_root()`](../luks_tray/Main.py) take their path as a required argument instead of falling back to the singleton. A "static" helper that reads the configuration can only work *after* a tray exists, so violating construction order raised `AttributeError: 'NoneType' object has no attribute 'ini_tool'`, and a second tray silently re-routed every later lookup. The genuinely pure helpers (`get_real_user_ids`, `get_real_user_home`, `expand_real_user`, `is_strictly_within`, `remove_empty_dir`, `is_empty_dir`) stay static.
   - **`IS_SWAY_LIKE_ENV` → `requires_manual_title()` at the point of use.** It is two environment lookups, so the "run the check only once" cache bought nothing and cost correctness: the answer was frozen at import time, so an environment set up afterwards — or a test that wants the Sway layout — was ignored. [`set_title()`](../luks_tray/dialogs/Common.py) asks the function; the constant is gone from both `__all__`s and from `Main`'s imports.
   - **`Utils.prt_path` / `prt_to_init` → `Utils.configure_log()`.** One private `_log` object (`path`, `opened_path`, `max_kb`) replaces the two globals. `configure_log()` redirects *immediately*, so messages logged while the program is still starting up reach the file: `main()` resolves the log path from a `paths_only` `IniTool` and calls `ensure_folder()` **before** building the real one, so a failing constructor is logged instead of written to a stdout a desktop launch has pointed nowhere. It can also be called again — `opened_path` records the file *this process* opened, so a re-target takes the stream over, where the old flag was set once and never cleared (making a second assignment a silent no-op). A terminal is still left alone, and so is a stdout the caller redirected to a file of their own.
   - **`os.chdir(self.folder)` → deleted.** A constructor that changes the process working directory redefines every relative path, including inside every child command (no call passes `cwd=`). Worse, it made a safety check unreachable: `get_auto_mount_root()` ran `os.path.abspath()` on the configured value, so a relative `auto_mount_folder = Vaults` became `~/.config/luks-tray/Vaults` — which then *passed* [`validate_auto_mount_root()`](../luks_tray/Main.py), being absolute by then, two levels deep, neither home nor a system tree — and was `mkdir -p`'d as the vault root. A relative value is now refused with a warning and the configured default (`~/Vaults`) is used. The same hole existed for dialog input: [`MountFileDialog.mount_file()`](../luks_tray/dialogs/MountFile.py) now refuses a relative or empty crypt file and a relative mount point *before* normalizing, because `check_upon()`'s `isabs()` test could never fire on an argument that had already been abspath'd.

   **Drive-by:** the launcher's name, settled here. It has been through three states in this audit's own history: #10 recorded `runner.py` → `Runner.py`, that rename landed as `runner` (extension dropped, name not CamelCased), and item #8 renamed it back to `Runner.py` on the assumption that the missing extension was the accident. The bare name is the intended one: an executable dev script is not an importable module, so #10's CamelCase rule for `luks_tray/*.py` never applied to it. It is [`runner`](../runner), and both the script's docstring and #10's rename list now say so.

   **Harness landscape:** `fix16` and `cleanup` needed one mechanical change each (they now hand a stand-in tray to helpers that are instance methods), and `fix8`/`fix10`/`fix11`/`fix14`/`fix15` were repointed where they had kept the pre-split paths or expectations — `fix15`'s AST allowlist names the three new silent handlers (`_is_tty`, `_is_reg`, `_reopen_log`) and its "missing `/sys` model" assertion now matches the later "expected sysfs gap" decision. Running that policy scan also caught a genuine leftover: `is_empty_dir()`'s `except OSError: return False`, which predates this change, now reports once through `prt_once()` — a permission problem no longer looks like a normal in-use vault directory. Four older harnesses (`fix10`, `fix11`, `fix12`, `fix13`) still fail, but on the medium #5 split rather than on this change: they extract functions out of the monolithic `Main.py` and call them with pre-split signatures. They are due that migration, not a regression here.

   **Verified** with `/tmp/luks_fix18_check.py` (33 assertions, `QT_QPA_PLATFORM=offscreen`): `LuksTray` has no `singleton`, the three helpers are instance methods while the eight pure helpers stay static; constructing `IniTool(paths_only=False)` leaves the cwd untouched (a relative `open()` still resolves to the launch directory, and the folder plus `config.ini` are still created); a relative `auto_mount_folder` yields the default *and* exactly one warning, an absolute one is used silently; `configure_log()` redirects on the first call, moves the log on the second and stops writing to the old file, while a user's `> file` redirect is respected; `requires_manual_title()` flips to `True` when `XDG_CURRENT_DESKTOP` is set *after* import; and `mount_file()` reports the relative crypt file, the empty crypt file and the relative mount point without reaching `check_upon()`. Subprocess runs cover the redirection itself. Plus `python -m py_compile` for every module and `runner`; both ruff selections → *All checks passed*; `lt-venv/bin/python runner --check-deps`, `python -m luks_tray.Main --check-deps` and `lt-venv/bin/luks-tray --check-deps` all exit 0; a `HOME=$(mktemp -d)` startup writes `debug.log` before `LuksTray` exists; and the four harnesses (`fix17` 25, `fix16` 28, `cleanup` 7, `split` 29) still report `FAILURES: none` after `fix16` and `cleanup` were migrated to hand a stand-in tray to the now-instance helpers.
9. ✅ **Dependency check list** ([`check_dependencies()`](../luks_tray/Main.py#L223)): ~~`bindfs` is listed twice~~ (removed with #7); ~~`blkid` and `mkdir` are used but not checked~~; ~~`udisks` plumbing is dead~~ (gone with #7). **Fixed — an AST sweep of every `run_cmd`/`sudo_cmd`/`run_cmd_capture`/`sudo_run`/direct `subprocess.run` call site found five used-but-unchecked commands, not two: `blkid`, `mkdir`, `touch`, `chown` and `sudo`.** All five are now in the list (ten entries → fifteen), which stays one flat hard-fail list rather than a required/optional split: [`__init__`](../luks_tray/Main.py#L146) already refuses to start on `missing`, and for these five that is the better failure mode. An absent `blkid` makes [`_probe_back_file_uuid()`](../luks_tray/History.py#L329) return `''`, which makes [`_json_data_to_namespaces()`](../luks_tray/History.py#L360) *purge* every file-container entry it cannot re-derive — silently discarding the stored password and mount point — while [`get_luks_uuid_for_file()`](../luks_tray/LuksOps.py#L52) catches nothing and [`run_cmd_capture()`](../luks_tray/LuksOps.py#L97) only catches `TimeoutExpired`, so the same absence raises `FileNotFoundError` into the mount dialog. `sudo` is listed for the related reason that it is never looked up by name — [`sudo_cmd()`](../luks_tray/LuksOps.py#L56) and [`sudo_run()`](../luks_tray/LuksOps.py#L119) prepend it — so its absence would surface as a raw `FileNotFoundError` rather than as the "please install" line; `touch` and `chown` cover the newly-created file container and the repair of a root-owned vault root left by an older `sudo mkdir -p`. **Verified:** `--check-deps` prints the fifteen `✓` lines and exits 0; `check_dependencies()` still returns a 2-tuple with `bindfs` exactly once; `python -m py_compile` and both `ruff` selections pass.
10. ✅ **Module naming:** ~~uppercase module files (`History.py`, `IniTool.py`, `Utils.py`) violate PEP 8 (`N999`).~~ **Fixed the other way round — CamelCase is the project style, so the odd ones out were the lowercase modules, and those are the ones that moved.** Renamed: `device_info.py` → [`DeviceInfo.py`](../luks_tray/DeviceInfo.py), `luks_ops.py` → [`LuksOps.py`](../luks_tray/LuksOps.py), `main.py` → [`Main.py`](../luks_tray/Main.py), `dialogs/common.py` → [`Common.py`](../luks_tray/dialogs/Common.py), `dialogs/master_password.py` → [`MasterPassword.py`](../luks_tray/dialogs/MasterPassword.py), `dialogs/mount_device.py` → [`MountDevice.py`](../luks_tray/dialogs/MountDevice.py), `dialogs/mount_file.py` → [`MountFile.py`](../luks_tray/dialogs/MountFile.py), plus the dev launcher `runner.py` → [`runner`](../runner) — a script, not a module, so it keeps the bare name (drive-by under #8). Package *directories* (`luks_tray/`, `dialogs/`) and the `__init__.py` markers stay lowercase — Python requires the latter, and a CamelCase directory would have bought nothing but a second naming rule. Every importer moved with them: the `luks_tray.X` imports inside the package, the `pyproject.toml` entry point ([`luks_tray.Main:main`](../pyproject.toml#L39)), the clickable path links in each module docstring, 241 path references in this audit, and the `/tmp` harnesses (their `import luks_tray.X as x` aliases were kept, so only the dotted paths changed).

    The convention is recorded in [`pyproject.toml`](../pyproject.toml#L41) so tooling agrees with it instead of nagging: `[tool.ruff.lint] ignore = ["N999"]`, with the reason in a comment. That `ignore` applies to any config-driven selection of pep8-naming; an explicit `ruff check --select N999` still names all ten modules, because a rule named on the command line overrides `ignore` — which is the flag doing its job, not a regression.

    **Verified:** `python -m py_compile` over `luks_tray/*.py`, `luks_tray/dialogs/*.py` and `runner` passes; `ruff check --select F,E9` and `--select F401,F811,F841` → *All checks passed*; an import test resolves `luks_tray.Main`, `luks_tray.DeviceInfo`, `luks_tray.LuksOps` and all four dialogs from their new modules (`main.__module__ == 'luks_tray.Main'`, dialogs reporting `luks_tray.dialogs.Common` … `MountFile`); `python -m luks_tray.Main --check-deps` **and** `lt-venv/bin/python runner --check-deps` both print the fifteen `✓` lines and exit 0, which exercises the renamed entry point end to end; and all four harnesses report `FAILURES: none` (fix17 25 assertions, fix16 28, cleanup 7, split 29) after their imports were repointed.
11. **No tests, CI, or dev dependencies** in [`pyproject.toml`](../pyproject.toml#L24). No `[tool.ruff]` / `[tool.pytest]` config despite ruff being the obvious linter — partially addressed by #10, whose `[tool.ruff.lint]` block exists only to record the `N999` decision and selects nothing on its own.

---

## 🔵 Documentation & packaging

- ✅ **Wrong folder names in README — FIXED.** It said `~/.Crypts` and `~/Crypts/{basename}`, but the code uses `~/.Vaults` for new containers ([`dot_vault_dir`](../luks_tray/dialogs/Common.py#L298)) and `~/Vaults/<basename minus .luks/.luks2/.crypt>` for a first mount ([`MountFileDialog.mount_file()`](../luks_tray/dialogs/MountFile.py#L240)). [README.md:54](../README.md#L54) and [README.md:55](../README.md#L55) now match the code. (`~/Vaults` as the *auto-generated* mount area at [README.md:27](../README.md#L27) was already right — it is the `auto_mount_folder` default in [`IniTool`](../luks_tray/IniTool.py#L53).)
- ✅ **Over-broad privilege guidance — FIXED.** [README.md:93](../README.md#L93) no longer recommends `{user} ALL=(ALL) NOPASSWD: ALL`. It now ships a `Cmnd_Alias LUKS_TRAY` covering exactly the eleven commands the app runs under `sudo -n`: `cryptsetup`, `mount`, `umount`, `losetup`, `fuser`, `mkdir`, `rmdir`, `chown`, `truncate`, `mkfs.ext4`, `bindfs` — derived from every `sudo_cmd()`/`sudo_run()` call site rather than from the full `--check-deps` list. The trailing note warns that sudoers matches *resolved* paths (so a `/usr/sbin/mkfs.ext4` must be corrected) and that `lsblk`, `blkid` and `touch` run unprivileged and need no rule.
- ✅ **Typos — FIXED.** `luk-tray` → `luks-tray` ([README.md:20](../README.md#L20)), `Waylan` → `Wayland` ([README.md:111](../README.md#L111)), `Workaounds` → `Workarounds` ([README.md:113](../README.md#L113)).
- ✅ **Legacy licence metadata — FIXED.** [`pyproject.toml`](../pyproject.toml#L13) now uses the PEP 639 form — `license = "MIT"` plus `license-files = ["LICENSE"]` — instead of the deprecated `license = { file = "LICENSE" }` table. The build requirement moved to `flit_core >=3.12,<4`, the first release that understands both keys, and the `License :: OSI Approved :: MIT License` classifier was dropped because a licence *expression* and `License ::` classifiers must not coexist. Classifiers now describe what the app really targets: `Environment :: X11 Applications :: Qt`, `Programming Language :: Python :: 3 :: Only`, `Topic :: System :: Filesystems`, `Intended Audience :: End Users/Desktop`, alongside the existing 3.11–3.13 and POSIX/Linux entries. Verified by building the wheel with flit_core 3.12.0: `Metadata-Version: 2.4`, `License-Expression: MIT`, `License-File: LICENSE`, and `luks_tray-2.0.5.dist-info/licenses/LICENSE` present in the archive.
- **`deploy` still mis-installs.** [`deploy`](../deploy) runs `pip install -e .` into whatever environment is active, while its own `echo` tells you to test with `lt-venv/bin/luks-tray` — so in the common case it installs into the wrong interpreter and that binary is stale or missing. Fix is `"$script_dir/lt-venv/bin/pip" install -e "$script_dir"` (plus `set -eu`), but this is a developer convenience script only, so it was left alone in the documentation pass.

---

## ⚪ Sway/Wayland support — open, prior attempt abandoned

Verified on **niri** (`XDG_CURRENT_DESKTOP=niri`, `WAYLAND_DISPLAY=wayland-1`, `QT_QPA_PLATFORM` unset → the `wayland` platform plugin): dialogs appear on the output the tray icon was clicked on and take focus correctly. That is the *compositor's* doing, not ours — under an `xdg_toplevel` surface the protocol has no client-side positioning, so `self.move(x, y)` in [`showEvent()`](../luks_tray/dialogs/Common.py#L152) is a no-op. The failures below are therefore not generic Wayland problems; they are **sway + waybar** problems, and none of them reproduce on niri.

**History — do not resurrect `origin/sway`.** `origin/sway` (`6e02692`, "sway incr dev: trying to get it work ... still shit", 2025-10-08) was four commits forked from `574d56b` (2025-09-28) and was **42 commits behind `dev`**: it predates the entire remediation and still edits the pre-split `luks_tray/main.py`. It was never merged (it is not an ancestor of `main` or `dev`), and its whole delta is that module plus a standalone repro script, `test-app`. Two of its ideas were re-implemented independently on `main` on 2025-11-04 — `349df08` "Fixed just broken i3 by trying to fix sway" and `c658d3e` "re-fix the issue of sway menus not updating, good grief" — which is where the failure table in the [`Common.py`](../luks_tray/dialogs/Common.py#L3) header, [`requires_manual_title()`](../luks_tray/dialogs/Common.py#L38), the in-dialog title in [`set_title()`](../luks_tray/dialogs/Common.py#L96) and the persistent-menu scheme behind [`replace_menu_if_different()`](../luks_tray/Main.py#L479) came from. A fresh branch off `dev` is cheaper than reconciling a 42-commit-old `main.py`, so the branch was deleted with nothing lost — but the commits were tagged first, so the option stays open cheaply.

**Removal and retrieval.** `refs/heads/sway` is gone from `origin`; the four commits are preserved by the lightweight tag `archive/sway` → `6e02692`, pushed to `origin` as `refs/tags/archive/sway`. Inspect it without touching the working tree:

```sh
git fetch --tags origin                       # if the tag is not local yet
git log --oneline 574d56b..archive/sway       # the four commits
git diff --stat 574d56b archive/sway          # luks_tray/main.py + test-app, nothing else
git show archive/sway:test-app > /tmp/test-app.py   # the standalone repro script
```

Restore it as a branch only if it is ever actually needed:

```sh
git branch sway archive/sway && git switch sway
```

Do not `git switch archive/sway` directly: that lands on a detached HEAD, and the tree it checks out is 42 commits behind `dev` with the pre-split `luks_tray/main.py`, so nothing there matches the current module layout. To drop the archive for good, `git push origin --delete archive/sway && git tag -d archive/sway`.

**Still unresolved (real, not cosmetic):**

1. **Taskbar entry / `app_id: python3`.** [`CommonDialog.__init__()`](../luks_tray/dialogs/Common.py#L79) *replaces* the window flags with `Dialog | WindowSystemMenuHint | WindowTitleHint | WindowCloseButtonHint`, dropping `WindowType.Tool`, because the hint was ignored anyway (diagnosis recorded at [`Common.py:14`](../luks_tray/dialogs/Common.py#L14)). The compositor cannot honour "no taskbar entry" when the dialog's only parent is [`dialog_parent`](../luks_tray/Main.py#L155) — a `QWidget(None)` created and immediately hidden, so it never acquires a Wayland surface to be the transient parent *of*. The abandoned branch's lever (a `QHiddenParentWidget`, 1×1, `Window | FramelessWindowHint`, hidden but *real*) was never tested on `dev`. **Caveat:** a real 1×1 window is exactly what shows up as a stray window under i3/polybar, and `349df08` exists because a sway fix broke i3 — any retry must be verified on **both** i3 and sway.
2. **No title bar.** Mitigated rather than fixed: `requires_manual_title()` draws a `QLabel` styled as a title bar *inside* the dialog, and only when the literal string `sway` appears in `XDG_CURRENT_DESKTOP` or `DESKTOP_SESSION` ([`Common.py:48`](../luks_tray/dialogs/Common.py#L48)). Niri, Hyprland and river get the missing decorations without the mitigation. Do **not** widen it to "any `WAYLAND_DISPLAY`" — Plasma on Wayland draws server-side decorations and would then be double-decorated; a capability test, if one is wanted, has to be explicit.
3. **Dialog placement is correct on X11 only.** [`showEvent()`](../luks_tray/dialogs/Common.py#L129) picks the screen with `self.screen()`; before the first show there is no window handle, so that resolves to the *primary* screen. The clamp `x = screen_geometry.right() - dialog_width` then drags the dialog back onto the primary whenever the cursor sits on an output extending past the primary's right edge. `QApplication.screenAt(QCursor.pos())` is the two-token fix. Invisible on Wayland, where the compositor places the window regardless.
4. **The documented sway rule is probably inert.** [README.md:126](../README.md#L126) says `for_window title "luks-tray.%"`. Sway criteria must be bracketed (`for_window [title="…"] floating enable`), and the pattern `luks-tray.%` cannot match the titles the code actually sets — `Master Password Dialog [luks-tray]`, `Mount Crypt File [luks-tray]`, … ([`MasterPassword.py:19`](../luks_tray/dialogs/MasterPassword.py#L19), [`MountFile.py:41`](../luks_tray/dialogs/MountFile.py#L41)). So that float rule has most likely never fired.

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

1. **Startup + crash fixes:** ✅ env-var guard (#1), ✅ stdout-returning command helper + `_setup_loop_device` (#2), ✅ `filesystem.name` guard (#3).
2. **Correct history identity:** use `generate_uuid_for_file_path`/back-file key for new files and persist `back_file` (#4).
3. **Crypto hardening:** PBKDF2/Argon2 with salt, `0600`, atomic write (#5, #7), and saves that no longer wipe the in-memory password (#9).
4. ✅ **Packaging:** `requires-python` is now `>=3.11` and the two PEP 701 f-strings were rewritten (#6).
5. ✅ **Safety:** `auto_mount_folder` is validated before any cleanup, and cleanup no longer uses sudo (#8); ✅ the documented sudoers rule is scoped to the eleven commands the app runs under `sudo -n` (see 🔵 Documentation & packaging).
6. **High-list correctness items** (✅ `browse_folder`, ✅ `check_upon` mount set, `update_history` ordering, ✅ `add_input_field`, ✅ `luksFormat` sudo, ✅ `mount_device` mapper name, ✅ `MountDeviceDialog` close prompt, ✅ error-sentinel idiom, ✅ CLI `paths_only` config/log, ✅ runtime `assert`s, ✅ dead `_unlock_luks` guard, ✅ `subprocess` deadlines).
7. **Refactor/perf:** ✅ `update_menu` re-entrancy guard + `processEvents` removal (medium #1), ✅ `blkid` result cache keyed on `(mtime, size)` so unchanged backing files are probed once (medium #2), ✅ logging for every swallowed exception via `prt`/`prt_once` (medium #4), ✅ `Main.py` split into `DeviceInfo.py` / `LuksOps.py` / `dialogs/` (medium #5), ✅ every `TBD` docstring written and the stale commented-out code removed (medium #6), ✅ dead code and unused imports removed (medium #7), ✅ `LuksTray.singleton`, `IS_SWAY_LIKE_ENV`, `Utils.prt_path` and `IniTool`'s `os.chdir()` removed (medium #8), ✅ the dependency check now covers every command the code actually shells out to, `sudo` and `blkid` included (medium #9); still open: worker-thread polling, add tests + a scoped ruff config.
8. **Sway/Wayland** (see ⚪ above): decide whether [`dialog_parent`](../luks_tray/Main.py#L155) becomes a real hidden surface — testing i3 *and* sway, because that trade is what broke i3 the last time — make the decoration decision capability-based instead of a `sway` string match, use `QApplication.screenAt()` for dialog placement, and correct the sway rule in the README. What is **not** worth doing is resurrecting `origin/sway` (deleted; its four commits survive as the `archive/sway` tag): the resolution has to be reinvented against the current tree.

`ruff check .` (ruff 0.17.0) now reports **50 issues**: 10 blind `except` (each of which now logs), 6 unused imports, 5 unsorted-imports, 5 `UP009`, 4 `RUF010`, 4 `EXE001`, plus style — and **0** `try/except/pass` (was 4) and no `invalid-syntax` since the PEP 701 rewrite. Most are auto-fixable with `ruff check --fix`; the critical items above required manual work.

After the medium #5 split, `ruff check --select F,E9 luks_tray runner` reports **0 findings** (it was 8 in `Main.py` alone: 6 unused imports + the 2 PEP 701 f-strings), and `python -m py_compile` passes for every module. Medium #7 keeps it at 0 findings with `Main.py` down to 997 lines, and `luks-tray --check-deps` still lists all fifteen commands and exits 0.
