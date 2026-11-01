# Audit: `luks-tray` (v2.0.5)

**Scope:** [`pyproject.toml`](../pyproject.toml), [`runner.py`](../runner.py), [`deploy`](../deploy), [`luks_tray/main.py`](../luks_tray/main.py), [`luks_tray/History.py`](../luks_tray/History.py), [`luks_tray/IniTool.py`](../luks_tray/IniTool.py), [`luks_tray/Utils.py`](../luks_tray/Utils.py), [`README.md`](../README.md).

**Method:** manual review, `python -m py_compile` (passes on 3.13), `ruff check` (64 findings), and contract tracing of `run_cmd`/`sudo_cmd`.

**Progress:** **9 of 9** critical items fixed and re-verified — **#1** (env-var guard), **#2** (`run_cmd`/`sudo_cmd` contract), **#3** (anomaly-case `cryptsetup close` crash), **#4** (file containers not remembered), **#5** (master-password KDF), **#6** (declared Python floor, now 3.11), **#7** (atomic, owner-only history writes), **#8** (`auto_mount_folder` validated before cleanup) and **#9** (history serialization no longer mutates the live namespace). Every critical item is closed; the High-severity table below is what remains. Line links for #1–#7 were re-verified against the current tree; the later sections predate those edits and may drift (the #3/#4/#6 fixes added ~55 lines to `main.py`, the #5/#7 fixes ~50 lines to `History.py`, and the #8 fix ~150 lines to `main.py`, so links into other code are stale).

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

**Verified:** `unmount_device()` was driven directly (Qt offscreen, `sudo_cmd` stubbed) with four fake containers: (a) partition + `filesystems=[luks-1111-2222]` → `cryptsetup close luks-1111-2222`; (b) partition + `opened=True` + `filesystems=[]` (the anomaly) → **no exception**, no `cryptsetup` call, `ERR: cannot close sdb1: ...` alert, dialog not accepted; (c) `type='crypt'` (file container) + `filesystems=[]` → `cryptsetup close mycrypt`; (d) `filesystems=[<name=''>]` → **no exception**, same actionable alert. `py_compile` passes. Note: this fix added 12 lines at `main.py:1535`, so pre-existing links into code below that point (`unmount_file`, `mount_file`, …) are stale (the #4 fix shifted them further).

### ✅ 4. New/added file containers are never remembered — FIXED

[`mount_file()`](../luks_tray/main.py#L1724) created a container with `uuid=None`, mounted it, then called `update_history(uuid, ...)` with `uuid=None` → [`HistoryClass.get_vital(None)`](../luks_tray/History.py#L98) stored an entry under the `None` key and never recorded `back_file`. After restart, file containers vanished from the menu and the JSON gained a `"null"` key.

The helper the audit originally pointed at — [`generate_uuid_for_file_path()`](../luks_tray/main.py#L74) — is **the wrong key** for this job: [`_json_data_to_namespaces()`](../luks_tray/History.py#L271) re-derives every `back_file`'s LUKS UUID with `blkid` on restore and **purges** the entry unless it equals the dict key, so a path-hash key would be culled on the very next load. The entry must be keyed by the LUKS UUID *inside* the file.

**Fix applied** — three parts:

1. New helper [`get_luks_uuid_for_file()`](../luks_tray/main.py#L84) (next to `generate_uuid_for_file_path`, which is no longer dead and is now only the last-resort fallback):

```python
rc, stdout, _stderr = run_cmd_capture(
        ['blkid', '-o', 'value', '-s', 'UUID', file_path])
return stdout.strip() if rc == 0 else ''
```

2. [`mount_file()`](../luks_tray/main.py#L1836) resolves the key after a successful mount and hands the backing file to the history:

```python
if not uuid:
    uuid = (container.uuid or get_luks_uuid_for_file(back_file)
            or generate_uuid_for_file_path(back_file))
tray.update_history(uuid, values, back_file=back_file)
```

3. [`update_history()`](../luks_tray/main.py#L780) gained `back_file=None` (device containers, which omit it, keep an empty `back_file`), sets `vital.uuid`, and now writes `upon` **before** `put_vital()` instead of after, so the mount point is actually persisted on the first save. It also only saves when something changed (`dirty`), covering a `back_file`-only change.

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

- [main.py:116](../luks_tray/main.py#L118) and [main.py:1318](../luks_tray/main.py#L1351) used **PEP 701** quote reuse in f-strings (`f'FAIL: {' '.join(args)} ...'`) — a parser error **before Python 3.12** (ruff `invalid-syntax`).
- [main.py:120](../luks_tray/main.py#L137) uses PEP 604 `str | None` without `from __future__ import annotations` → import-time `TypeError` on <3.10.

**Fix applied:**

- Both f-strings now use an outer double quote — `f"FAIL: {' '.join(args)}: {stdout} {stderr} [rc={rc}]"` ([main.py:118](../luks_tray/main.py#L118), [main.py:1351](../luks_tray/main.py#L1351)) — which every supported interpreter parses; a short comment at the first site records why the quote characters must differ.
- PEP 604 annotations need Python ≥3.10, so with the floor at 3.11 nothing has to change: [`run_unmount(..., busy_warns: set) -> str | None`](../luks_tray/main.py#L137) stays as-is and no `__future__` import is needed.
- [`pyproject.toml`](../pyproject.toml#L14) now declares `requires-python = ">=3.11"` and lists 3.11/3.12/3.13 classifiers.

**Verified:** `ruff --target-version py311 --select F,E9 luks_tray runner.py` → **0** `invalid-syntax` findings (was 2); the 6 remaining findings are pre-existing `F401` unused imports (`shlex`, `tempfile`, `pathlib.Path`, `io.StringIO`, `QSizePolicy`, `QFontDatabase`). A sweep for other 3.12-only APIs (`itertools.batched`, `typing.override`, `sys.monitoring`, `Path.walk`, PEP 695 `type` aliases/generics) found none. `py_compile` passes for every module on 3.13.

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

[`remove_unused_automounts()`](../luks_tray/main.py#L1033) iterated **whatever** `auto_mount_folder` resolved to and ran `sudo rmdir` on every empty subdirectory. If a user set `auto_mount_folder = /home` (or a typo such as `/`), it removed empty directories under that path **with root privileges**.

**Fix applied** — a new gate, [`validate_auto_mount_root()`](../luks_tray/main.py#L872), is consulted by *both* deletion paths ([`remove_unused_automounts()`](../luks_tray/main.py#L1039) and [`remove_if_auto()`](../luks_tray/main.py#L1026)); any returned string means "do nothing, log why". The folder must look like a **dedicated** vault area:

- absolute, at least two levels deep, and not one of the exact system paths in [`FORBIDDEN_AUTO_MOUNT_ROOTS`](../luks_tray/main.py#L384) (`/`, `/home`, `/tmp`, `/mnt`, `/usr`, …) nor anywhere inside a system tree listed in [`FORBIDDEN_AUTO_MOUNT_PREFIXES`](../luks_tray/main.py#L395) (`/etc`, `/usr`, `/var`, `/boot`, `/opt`, `/srv`, …). Paths inside the real user's home are exempt from the prefix list, so non-standard layouts (`/var/home/<user>/Vaults`) still work;
- not a symlink (which could point anywhere), not the real user's home, not an ancestor of it, and not inside *another* user's home — `/home` is thus checked as a multi-user tree explicitly (`/home/other/Vaults` is rejected while `~/Vaults` is not), which also holds when the tray's home is `/root`;
- if it exists: a plain directory, not a mount point, owned by the real user ([`get_real_user_ids()`](../luks_tray/main.py#L843), sudo-aware) **or root** — the tray runs under `sudo` and creates the folder itself, so `~/Vaults` is root-owned in practice — and not writable by group/other (which also rejects `/tmp`).

Cleanup itself no longer escalates at all: [`remove_empty_dir()`](../luks_tray/main.py#L1006) uses plain `os.rmdir` (which refuses non-empty directories), skipping symlinks and mount points, so a validated folder needs no root to be tidied. The subdirectory loop also skips dot-entries, symlinks and directories owned by a third uid, and reports each removal via `prt`. Containment is now checked by [`is_strictly_within()`](../luks_tray/main.py#L994) instead of `os.path.commonpath(...) == parent`, which treated "target == parent" as "inside the parent".

**Verified** (`QT_QPA_PLATFORM=offscreen`, `LuksTray.singleton` stubbed with a fake `ini_tool`, real temp directories):

- Rejected: `/`, `/home`, `/tmp`, `/etc`, `/usr`, `/var`, `/mnt`, `/boot`, `/dev`, `/proc`, `/sys`, `/root`, the home directory itself, `/etc/luks`, `/usr/local/Vaults`, `/var/lib/Vaults`, `/opt/Vaults`, `/srv/Vaults`, `/root/Vaults`, `/home/notjoe/Vaults`, a symlinked root, a `0777` root, and a regular file used as the root.
- Accepted: the shipped default `~/Vaults` (= `/home/joe/Vaults`, root-owned `0755`), a not-yet-existing `~/Vaults`-style path, `~/Vaults` written without expansion, `/mnt/Vaults`, `/tmp/<random>/Vaults`, and — with `HOME=/root` — `/root/Vaults` (while `/home/<any-user>/Vaults` stays rejected).
- Cleanup on a valid root: exactly the one empty subdirectory removed; a non-empty directory, a regular file, a dot-directory and a symlink were all left untouched, and the symlink's **target** outside the root was not deleted.
- Hostile configs: `auto_mount_folder=/home` and `/` both log `WARN: skipping auto-mount cleanup: …` and return `0` (nothing inspected, nothing deleted); an invalid (group-writable) root disables both `remove_unused_automounts()` and `remove_if_auto()` while the child directories survive.
- `remove_if_auto()`: `True` for an empty dir inside the root (and the dir is gone), `False` for the root itself, for a path outside the root, and for any path when the root fails validation.

`python -m py_compile` passes; `ruff check --select F,E9 luks_tray/main.py` still reports the same 6 pre-existing `F401` unused imports and nothing new. **Trade-off:** on a folder that fails validation, stale empty mount directories are no longer cleaned (a log line explains why) — a safe failure, and the shipped default passes all checks.

### ✅ 9. In-memory password wiped as a side effect — FIXED

[`_namespaces_to_json_data()`](../luks_tray/History.py#L151):

```python
legit = vars(vital)          # alias to vital.__dict__
legit['password'] = ''       # mutates the live object
entries[uuid] = vars(vital)  # same dict again
```

`vars()` hands back the namespace's **own** `__dict__`, so blanking the password "for the clear-text file" also erased it from the live object in `self.vitals`, and `entries[uuid]` was that same dict again. Each clear-text save therefore destroyed the remembered password:

- the next "Mount …" dialog prefilled an empty password ([`main.py:1663`](../luks_tray/main.py#L1663), [`main.py:1829`](../luks_tray/main.py#L1829));
- [`update_history()`](../luks_tray/main.py#L823) compared `values['password'] != vital.password` against `''`, so it saw a change and re-saved on every call.

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
| 1 | ✅ [`browse_folder()`](../luks_tray/main.py#L1313) | ~~`self.home_dir()` calls a **string** (`home_dir` is a `str` set by [`get_real_user_home_directory()`](../luks_tray/main.py#L1301)) → `TypeError` when the mount field is cleared.~~ **Fixed** — the empty-field fallback now calls `self.get_real_user_home_directory()`, which returns the `str` (and lazily initializes it). |
| 2 | ✅ [`update_history()`](../luks_tray/main.py#L780) | ~~Sets `vital.upon` **after** `put_vital()` (which saves), so a new mount point is not persisted on that save.~~ **Fixed with critical item 4** — `upon` and `back_file` are now written before the single `put_vital()`. |
| 3 | ✅ [`check_upon()` callers](../luks_tray/main.py#L1714) | ~~`mount_points = tray.update_mounts()` returns the **device key set** ([main.py:563](../luks_tray/main.py#L563)), not mount points, yet [`check_upon`](../luks_tray/main.py#L1415) treats it as mount points (`text in mount_points`). Duplicate-mount detection is broken; pass `tray.upons`.~~ **Fixed** — both callers ([`mount_device()`](../luks_tray/main.py#L1714), [`mount_file()`](../luks_tray/main.py#L1985)) now call `tray.update_mounts()` for its side effect and pass `tray.upons`; `check_upon()` documents the required argument. |
| 4 | ✅ [`add_input_field()`](../luks_tray/main.py#L1222) | ~~With a scalar label and multiple `keys`, it does `placeholder_texts.append(text_str)` while `placeholder_texts` may still be a `str` → `AttributeError`. Normalize both to lists first.~~ **Fixed** — `label_texts` and `placeholder_texts` are normalized to lists up front, single values are broadcast to every key, and mismatched lengths now raise a descriptive `ValueError` instead of an `IndexError`. |
| 5 | ✅ [`mount_luks_container()`](../luks_tray/main.py#L1525) | ~~`luksFormat` runs **without `sudo`** (unlike everything else) and uses `check=True`, so the `if sub.returncode != 0` branch is unreachable and real stderr is swallowed by the outer `except` → generic "An error occurred". Convert to [`sudo_run()`](../luks_tray/main.py#L132) (see #2).~~ **Fixed** — `luksFormat` now runs through [`sudo_run()`](../luks_tray/main.py#L132) (`sudo -n`, `check=False`), and its real `rc`/stderr is returned immediately rather than overwritten by the later `_unlock_luks()` call. |
| 6 | ✅ [`mount_device()`](../luks_tray/main.py#L1714) | ~~`luks_device = ''` when `len(container.filesystems) != 1`; `cryptsetup open ... ''` then fails with a confusing error.~~ **Fixed** ([main.py:1747](../luks_tray/main.py#L1747)) — the mapper name is taken from the single named child filesystem, otherwise derived as `f'{container.name}-luks'` (same convention as [`mount_luks_container()`](../luks_tray/main.py#L1579)); an empty or missing `container.filesystems` can no longer reach `cryptsetup open`. |
| 7 | ✅ [`MountDeviceDialog`](../luks_tray/main.py#L1661) | ~~Open-state branch renders `f'Close {",".join(mounts)}?'` where `mounts` is `[]` ([main.py:1427](../luks_tray/main.py#L1427)) → "Close ?".~~ **Fixed** ([main.py:1680](../luks_tray/main.py#L1680)) — the close branch (which by definition has no mount points) now names the mapper devices, falling back to `container.name`, so it never renders a bare "Close ?". |
| 8 | ✅ Error sentinel idiom | ~~`errs.append(f'{container.name}')` then `if len(errs) <= 1` treats the first element as a placeholder ([main.py:1452](../luks_tray/main.py#L1452), 1509, 1678). Use a plain `errs = []` and `if not errs`.~~ **Fixed** — the placeholder append is gone from [`mount_device()`](../luks_tray/main.py#L1720), [`unmount_device()`](../luks_tray/main.py#L1793) and [`mount_file()`](../luks_tray/main.py#L1968); every `len(errs) <= 1` / `len(errs) > 1` test is now `if not errs` / `if errs` (e.g. [main.py:1769](../luks_tray/main.py#L1769), [1828](../luks_tray/main.py#L1828), [2048](../luks_tray/main.py#L2048)), and the container-name heading is prepended to [`alert_errors()`](../luks_tray/main.py#L1389) only at the point of reporting. Side effects: a single error is now reported (the old off-by-one swallowed exactly one error), and `unmount_device()` no longer dereferences a `None` container (it reports "container not found"). |
| 9 | ✅ [CLI paths](../luks_tray/main.py#L2119) | ~~`--edit-config` / `--follow-log` use `IniTool(paths_only=True)`, which skips [`ensure_ini_file()`](../luks_tray/IniTool.py#L76). On a fresh install the folder/file may not exist.~~ **Fixed** — [`ensure_folder()`](../luks_tray/IniTool.py#L70) was split out of `ensure_ini_file()` (which now calls it), `--edit-config` calls `ensure_ini_file()` ([main.py:2125](../luks_tray/main.py#L2125)) so `$EDITOR` always gets an existing `config.ini`, and `--follow-log` calls `ensure_folder()` and creates an empty log file ([main.py:2142](../luks_tray/main.py#L2142)) so `tail -F` does not report a missing file on first run. `paths_only=True` itself remains side-effect free, so `main()`'s normal startup path is unchanged. |
| 10 | ✅ Runtime `assert` | ~~Validation via `assert` at [main.py:398](../luks_tray/main.py#L398), [434](../luks_tray/main.py#L434), [802](../luks_tray/main.py#L802), [819](../luks_tray/main.py#L819), [1286](../luks_tray/main.py#L1286). Under `python -O` these vanish, so missing deps/icon-copy failures continue half-initialized.~~ **Fixed** — all seven `assert` statements in [`main.py`](../luks_tray/main.py) (and the `assert False` in [`IniTool.update_config()`](../luks_tray/IniTool.py#L179)) are now real validation. Missing system commands ([main.py:437](../luks_tray/main.py#L437)) and missing tray icons ([main.py:478](../luks_tray/main.py#L478)) raise `RuntimeError`, so the tray fails in `main()`'s handler instead of running half-initialized; the same runs for `generate_auto_mount_folder()` ([main.py:987](../luks_tray/main.py#L987), [1004](../luks_tray/main.py#L1004)); `add_input_field()` raises `ValueError` for an unknown `field_type` ([main.py:1297](../luks_tray/main.py#L1297), which also no longer leaves `input_field` unbound); `mount_luks_container()` returns its usual error string when `upon` is empty ([main.py:1557](../luks_tray/main.py#L1557)) instead of letting a bogus mount point reach `mount`; `mount_file()` reports "tray is not initialized" through `alert_errors()` ([main.py:1996](../luks_tray/main.py#L1996)); and an unsupported config golden type now keeps the default and logs the skip ([IniTool.py:179](../luks_tray/IniTool.py#L179)). Verified under `python3 -O`, where the old `assert`s were stripped. |
| 11 | ✅ [`_unlock_luks()`](../luks_tray/main.py#L1493) | ~~`if hasattr(self, 'opened') and self.opened` — `self` is a dialog, which never has `opened`; the "already unlocked" guard is dead code.~~ **Fixed** — the guard is gone from [`_unlock_luks()`](../luks_tray/main.py#L1493), and the identical unreachable test in [`_setup_loop_device()`](../luks_tray/main.py#L1519) went with it. `CommonDialog` never defines `opened` (only [`DeviceInfo`](../luks_tray/main.py#L209) container namespaces do), so both branches could never fire; the `cryptsetup open` / `losetup` code beneath them now always runs. It was deliberately *not* replaced with a live "mapper path exists → skip" test: that would skip the password check and mount a volume another user already has open. |
| 12 | ✅ [stdin redirect](../luks_tray/main.py#L2234) | ~~Replaces `stdin` with `/dev/null`; acceptable, but combined with no `subprocess` timeouts, a hung `cryptsetup` freezes the UI indefinitely.~~ **Fixed** — the redirect itself stays (it stops a child from stealing the shell's keystrokes; the reason is now spelled out at [main.py:2226](../luks_tray/main.py#L2226)), but every external command now has a deadline. [`run_cmd_capture()`](../luks_tray/main.py#L141) forwards a `timeout` defaulting to `CMD_TIMEOUT` = 60 s ([Utils.py:29](../luks_tray/Utils.py#L29)), kills the child on overrun and reports `TIMEOUT_RC` = 124 ([Utils.py:40](../luks_tray/Utils.py#L40)) with a `timed out after Ns` message, so [`run_cmd()`](../luks_tray/main.py#L107)/`sudo_cmd()`/`sudo_run()` ([main.py:163](../luks_tray/main.py#L163)) render it as their usual `FAIL: … [rc=124]` string and no caller needs a new branch. Read-only probes use `PROBE_TIMEOUT` = 15 s ([Utils.py:33](../luks_tray/Utils.py#L33)): `lsblk` keeps the previous snapshot and logs ([main.py:333](../luks_tray/main.py#L333), [339](../luks_tray/main.py#L339)), `fuser` falls back to "(Could not get process info…)" ([main.py:199](../luks_tray/main.py#L199)), and `blkid` treats the entry as unverifiable ([History.py:284](../luks_tray/History.py#L284)). `umount` ([main.py:177](../luks_tray/main.py#L177)) is bounded too, and the one-shot setup commands use `FORMAT_TIMEOUT` = 900 s ([Utils.py:37](../luks_tray/Utils.py#L37)): `luksFormat` ([main.py:1639](../luks_tray/main.py#L1639)) and `mkfs.ext4` ([main.py:1669](../luks_tray/main.py#L1669)). Verified with a harness that really runs a hanging child: at `timeout=0.4` the call returns `rc=124` with the timeout text (and partial stdout coerced to `str`), stdin piping for `--key-file -` still works, `run_unmount`/`parse_lsblk` survive a hung `umount`/`lsblk`, and an AST scan shows no `subprocess.run` left in `main.py`/`History.py` without `timeout=`. |

---

## 🟡 Medium — design, performance, quality

1. ✅ **UI-thread blocking & re-entrancy.** The 3 s `QTimer` ([main.py:545](../luks_tray/main.py#L550)) runs [`update_menu()`](../luks_tray/main.py#L647): opens `/proc/mounts`, spawns `lsblk`, reloads/decrypts history, and runs `blkid` per backing file. ~~[`show_progress()`](../luks_tray/main.py#L1505) calls `QApplication.processEvents()`, so the timer can **re-enter** `update_menu` mid-mount. Move polling to a worker thread and remove `processEvents`.~~ **Fixed (re-entrancy)** — `update_menu()` is now guarded: it returns immediately if a refresh is already running (`self.refreshing`, [main.py:659](../luks_tray/main.py#L659)) or if a privileged dialog action is in flight, which is what [`busy_operation()`](../luks_tray/main.py#L690) marks around the mount/unmount work in both dialogs ([main.py:1898](../luks_tray/main.py#L1898), [1928](../luks_tray/main.py#L1928), [2062](../luks_tray/main.py#L2062), [2198](../luks_tray/main.py#L2198)). Both flags are cleared in `finally`, so an exception cannot park the poller. `show_progress()` no longer spins the event loop — it activates the layout and calls `repaint()` ([main.py:1527](../luks_tray/main.py#L1528)), painting the progress widgets *without* dispatching the timer tick or a queued click; `processEvents` no longer appears in the code. Polling is still synchronous, but every command it runs is bounded by the deadlines from high item #12; moving it to a worker thread (and the `blkid` cache in #2) is now a latency refactor, not a correctness fix, and stays in the fix order.
2. **`blkid` per file per refresh.** [`_json_data_to_namespaces()`](../luks_tray/History.py#L271) shells out `blkid` for every `back_file` on each restore (every 3 s). Cache by `(path, mtime)`.
3. ✅ **Subprocess deadlines** (fixed with high item #12): `run_cmd_capture()`, `run_unmount()`, `parse_lsblk()` and `History`'s `blkid` all pass `timeout=`; one hung command can no longer stall the tray indefinitely.
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

1. **Startup + crash fixes:** ✅ env-var guard (#1), ✅ stdout-returning command helper + `_setup_loop_device` (#2), ✅ `filesystem.name` guard (#3).
2. **Correct history identity:** use `generate_uuid_for_file_path`/back-file key for new files and persist `back_file` (#4).
3. **Crypto hardening:** PBKDF2/Argon2 with salt, `0600`, atomic write (#5, #7), and saves that no longer wipe the in-memory password (#9).
4. ✅ **Packaging:** `requires-python` is now `>=3.11` and the two PEP 701 f-strings were rewritten (#6).
5. ✅ **Safety:** `auto_mount_folder` is validated before any cleanup, and cleanup no longer uses sudo (#8); ⬜ scope the documented sudoers rule.
6. **High-list correctness items** (✅ `browse_folder`, ✅ `check_upon` mount set, `update_history` ordering, ✅ `add_input_field`, ✅ `luksFormat` sudo, ✅ `mount_device` mapper name, ✅ `MountDeviceDialog` close prompt, ✅ error-sentinel idiom, ✅ CLI `paths_only` config/log, ✅ runtime `assert`s, ✅ dead `_unlock_luks` guard, ✅ `subprocess` deadlines).
7. **Refactor/perf:** ✅ `update_menu` re-entrancy guard + `processEvents` removal (medium #1); still open: worker-thread polling, cached `blkid` (#2), split `main.py`, add tests + a scoped ruff config.

`ruff check .` currently reports **64 issues** (16 blind `except`, 6 unused imports, 2 real `invalid-syntax` under the declared Python, 4 `try/except/pass`, plus style). Most are auto-fixable with `ruff check --fix`; the critical items above require manual work.

After the #1/#2 fixes the `ruff --select F,E9` subset of [`main.py`](../luks_tray/main.py) is **8 findings** (6 unused imports + the 2 PEP 701 f-strings), no new findings introduced, and `python -m py_compile` passes.
