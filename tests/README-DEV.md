# Developing `luks-tray`

How to set up a tree, what the gates are, what the tests pin, and how a release
leaves this repository.

This file lives under `tests/` on purpose: the root belongs to
[`README.md`](../README.md), which is for people who *use* the tray.  The rest of
the development material — this file, [`AUDIT.md`](AUDIT.md) and
[`BLESS-ADOPTION-PLAN.md`](BLESS-ADOPTION-PLAN.md) — sits next to the tests it
describes.

Every command below is run from the repository root.

---

## 1. One version, in one place

[`luks_tray/__init__.py`](../luks_tray/__init__.py) holds the release number:

```python
__version__ = "3.0.0"
```

That line is read by three different things and written by none of them:

* the build backend — [`pyproject.toml`](../pyproject.toml) declares
  `dynamic = ["version"]`, so flit_core reads the line at build time;
* `luks-tray --version`, which prints the number from the *installed* package;
* [`bless`](../bless), which tags that number and then reads it back out of the
  wheel it just built (the smoke stage).

A second copy of the version is how the published metadata drifted from the code
in the 2.x releases, so [`tests/test_metadata.py`](test_metadata.py) fails if one
appears.  Bumping a release means editing that one line, committing, and running
`./bless`.

---

## 2. Set up a tree

```sh
python3 -m venv lt-venv
lt-venv/bin/python -m pip install -e '.[dev]'
```

The `dev` extra is exactly what the gate needs — `pytest`, `pytest-cov`, `ruff`,
`build`, `twine>=7`, `packaging>=26` — on top of the runtime dependencies.  `-e`
also installs the console script, so `lt-venv/bin/luks-tray` works from the tree.

To run the tray from the source tree without installing anything at all:

```sh
./runner                     # or: lt-venv/bin/python runner
```

Name the interpreter explicitly if the shebang picks a `python3` that has no
PyQt6.

**Headless machines** (CI, ssh, a container): Qt needs a platform plugin.
The test suite sets `QT_QPA_PLATFORM=offscreen` for itself in
[`conftest.py`](../conftest.py) before Qt is imported; for a manual run, set it
yourself.  PyQt6 wheels also need `libgl1 libegl1 libxkbcommon0 libdbus-1-3` —
that is the `apt-get` line the CI jobs install.

---

## 3. The three gates

[`bless`](../bless) and CI run these three commands on the same interpreter, and
nothing else counts as "green":

```sh
lt-venv/bin/python -m ruff check .
lt-venv/bin/python -m compileall -q luks_tray conftest.py tests
lt-venv/bin/python -m pytest -q --cov=luks_tray --cov-report=term-missing
```

* `ruff check .` takes **no flags** in the gate.  The rule selection is pinned in
  [`pyproject.toml`](../pyproject.toml) under `[tool.ruff.lint] select` instead of
  being inherited from whatever the installed ruff calls its default — ruff 0.17
  widened that default set and reports 56 pre-existing findings in this package.
  The reasoning is in the comment there; widening the selection is
  [`AUDIT.md`](AUDIT.md) medium #11, not something to smuggle into a release.
* `compileall` parses every file *including* the tests, so a syntax error cannot
  hide behind pytest's collection order.
* `pytest` reads `[tool.pytest.ini_options]` and `[tool.coverage.run]` /
  `[tool.coverage.report]`; coverage is collected with `branch = true`, because
  the misses that matter here are the arms of an `if` nobody takes — the
  container state machine and the mount/unmount error paths.

The suite is **491 tests**.  It needs no network, no `sudo`, and writes nothing
outside `tmp_path` (the `isolated_home` fixture in
[`conftest.py`](../conftest.py) points `HOME` and `XDG_CONFIG_HOME` at a
temporary directory for every test).  Any behaviour that would need root is
asserted as a *refusal* — that is the contract the tray actually has.

**Coverage floor.**  `[tool.coverage.report] fail_under` is deliberately not set
yet; see [Known gaps](#11-known-gaps).  When it is, pytest-cov reads the number
from that table, so a developer and CI cannot enforce different floors.

---

## 4. What the tests pin

| File | What it holds down |
| --- | --- |
| [`test_utils.py`](test_utils.py) | Where `prt()` writes: terminal, redirected file, configured log.  The redirection runs in a subprocess, because `configure_log()` calls `os.dup2()`, which would replace pytest's own output. |
| [`test_initool.py`](test_initool.py) | The config folder, the file it creates, and the tolerant parsing: a hand-edited `config.ini` must never be able to break start-up.  Also the write side (`set_values`): all-or-nothing validation, atomic replace, unknown settings preserved. |
| [`test_history.py`](test_history.py) | The clear-text and encrypted history files, the atomic write, the migrations and every corrupt-file fallback — each one is a path that loses a password silently if it is wrong. |
| [`test_container_type.py`](test_container_type.py) | The salt, which is the whole identity of a container whose header is encrypted: the 64-byte length check, unreadable, missing and short files, that nothing after the salt matters, and that a re-created container gets a different one. |
| [`test_luksops.py`](test_luksops.py) | The `run_cmd`/`sudo_cmd` contract, `run_unmount`'s busy-device reporting, the mapper/loop-device primitives. |
| [`test_main_helpers.py`](test_main_helpers.py) | Which directories may be deleted: `auto_mount_folder` validation, ownership, symlinks, `is_strictly_within`, `remove_empty_dir`'s escalation, `remove_unused_automounts`. |
| [`test_dialogs.py`](test_dialogs.py) | `CommonDialog`: window flags, the manual title, input fields, `check_upon`, the progress UI.  A real `QApplication` under the offscreen plugin, not a stub. |
| [`test_menu.py`](test_menu.py) | `replace_menu_if_different()` — rebuild the tray menu only when it changed.  This is the fix for the sway symptom that produced the `archive/sway` branch: rebuilding the `QMenu` on every 3 s tick closed a popup under the user's cursor.  Also the fixed entries at the bottom: every one starts with a symbol, `⚙ Options…` opens the dialog, and the versioned `↺ Restart luks-tray …` entry is last. |
| [`test_options_dialog.py`](test_options_dialog.py) | `OptionsDialog`: what it shows (the values the tray is using) and what it writes (only values `update_config()` accepts back), the refused `auto_mount_folder` paths that keep the dialog open, and the hand-off to `MasterPasswordDialog` with the state line that follows it. |
| [`test_tray_watch.py`](test_tray_watch.py) | Tray (re-)registration: `tray_watcher_present()` answers True/False/None instead of blocking, `TrayWatch` re-registers the item when the StatusNotifierWatcher appears or restarts, and the item is re-created (not duplicated) against a fake bus. |
| [`test_single_instance.py`](test_single_instance.py) | The single-instance lock, `--replace` as the only takeover path (and only for a verified pid), `main()` exiting 3 without signalling, and `restart_self()` releasing (and, on a failed spawn, retaking) the lock. |
| [`test_service.py`](test_service.py) | The `systemd --user` unit: the two shipped templates (launcher path, pinned session type, `RestartPreventExitStatus=3 15`, no `StandardOutput=`), `render()` appending only the variables the installing shell exported, and every `--svc-*` verb as a `Step` list -- install is the only verb that writes, `start`/`restart` refuse a unit that runs another executable, `stop`/`uninstall` keep working on a broken install, and `status` returns 0/1/2. |
| [`test_service_restart.py`](test_service_restart.py) | The two supervisor-aware paths: `restart_self()` asks systemd (`systemd-run --user --scope`, then a clean exit) instead of spawning a child under `Restart=always`, and `--replace` refuses to signal a holder that a unit supervises.  Also `Utils.in_systemd_service()` and `spawn_detached()`. |
| [`test_metadata.py`](test_metadata.py) | One version location, one console script, and a `--version` that agrees with both. |
| [`test_repo_hygiene.py`](test_repo_hygiene.py) | Repository invariants the gate relies on: the sdist manifest, the inspector, the workflow's gates matrix, `QT_QPA_PLATFORM`, branch triggers, `permissions`, and that CI holds no credential and cannot publish. |
| [`test_bless.py`](test_bless.py) | The gate's own decisions: identity read from `pyproject.toml`, version-line rewrite inside the rehearsal copy, the duplicate check, `branch_plan`, the prompt, `_artifacts()`, `~/.pypirc` lookup, index-lag messages. |

Two product bugs were found by this suite and fixed: `LuksOps.run_unmount()`
called `QMessageBox.set_title()` (the method is `setWindowTitle`), so the
busy-device dialog raised instead of appearing; and
`CommonDialog.hide_progress()` tested `hasattr(self, 'progress_label')`, which is
always true, so the dialogs' `finally:` paths hit `AttributeError` whenever the
progress widgets had never been shown.  Both are recorded in
[`AUDIT.md`](AUDIT.md) medium #12.

[`inspect_dist.py`](inspect_dist.py) is not a test module: it is the shared
inspector that both the CI packaging job and `bless` run against the built wheel
and sdist.  One implementation, so the two cannot disagree about what a good
distribution looks like.

---

## 5. The release gate

```sh
lt-venv/bin/python ./bless
```

[`bless`](../bless) is the only path to an upload.  It takes **no argument and no
flag** — any stage you could name on the command line is a stage you could skip.
Use the `lt-venv` interpreter: the gates run `sys.executable`, and the system
`python3` has no PyQt6 for the tests to import.

The stages run in this order, and each one refuses rather than warns:

| Stage | Refuses |
| --- | --- |
| preflight | a dirty tree (what gets built has to describe a commit), a detached `HEAD`, a `vX.Y.Z` tag that already exists, `twine < 7` or `packaging < 26`, a missing `python -m build` |
| index check | a version PyPI already has — and prints every published version in the refusal, because an upload cannot be replaced |
| gates | the three commands in §3 |
| build | builds into a wiped `build/bless-<version>/`, then requires exactly one wheel and one sdist |
| inspect | [`tests/inspect_dist.py`](inspect_dist.py) against the tree's own metadata — with the rehearsal's own version passed in |
| check | `twine check --strict` on both files |
| smoke | installs the wheel into `build/bless-<version>/smoke-venv` and requires `luks-tray --version` to print the number that was built |
| branch plan | a `main`/`dev` pair that has diverged, a missing `origin/<branch>`, or a push that would not be a fast-forward |
| checklist | prints everything in hand, then asks |

---

## 6. The prompt

```text
====> Enter test or publish or quit:
```

The word has to be typed in full.  `test` and `publish` are one keystroke away
from inactivity and this is the only irreversible question in the gate, so a
prefix is not accepted and nothing is inferred:

* `test` — the rehearsal.  A throwaway `X.Y.Z.99.N` is built from a **copy** of
  the tree (your working version is never spent; `99` keeps it clear of every
  real release and `N` is one past whatever TestPyPI already holds for that
  base).  It uploads to `test.pypi.org` only, creates no tag and moves no branch.
* `publish` — fast-forwards whichever of `main`/`dev` is behind so the two are
  one commit, tags `vX.Y.Z`, uploads both files to PyPI, verifies the index, and
  pushes the branch, the other branch and the tag.
* anything else — including Enter, `quit` and Ctrl-C — **stops**.  Nothing is
  tagged, uploaded or pushed.  An unrecognised answer asks again, so a stray
  character can neither publish nor cancel by accident.

---

## 7. Credentials

The token is looked up per target, so a PyPI token is never spent on TestPyPI
just because both are configured on the same machine:

| Target | Environment variable | or a `~/.pypirc` section whose `repository` is |
| --- | --- | --- |
| PyPI | `TWINE_PASSWORD` (username `__token__`) | `https://upload.pypi.org/legacy/` |
| TestPyPI | `TWINE_TESTPYPI_PASSWORD` | `https://test.pypi.org/legacy/` |

When the environment answers, it wins; otherwise the matching section is used and
handed to twine as `TWINE_USERNAME`/`TWINE_PASSWORD`.  Whichever source answers,
twine receives exactly one credential.  `chmod 600 ~/.pypirc`.

```ini
[pypi]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-...

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-...
```

---

## 8. Trying the rehearsal

After a `test` run, `bless` prints this verbatim:

```sh
python -m pip cache purge
python -m pip install --no-cache-dir \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    luks-tray==X.Y.Z.99.N
```

Both lines are needed:

* `pip cache purge` — pip remembers an index it has already read, and will not
  see the upload that just landed;
* `--extra-index-url https://pypi.org/simple/` — TestPyPI carries none of the
  dependencies (`PyQt6`, `cryptography`, `psutil`, `petname`), so PyPI has to be
  the fallback.

Undo it with `python -m pip uninstall luks-tray`.  Nothing was tagged and nothing
reached PyPI at this point.

---

## 9. The index is slow, and that is not a failure

Both indexes answer through a CDN that has taken minutes — sometimes more than
ten — to list a release twine has already accepted.  So the read-back is
cache-busted, it waits up to **853 s (≈14 minutes)** printing how long it has
waited, and it never calls an accepted upload a failure.

Ctrl-C during that wait is safe: the files are already on the index.  If the
budget runs out, the message says the release is fine and the index is behind,
names the project page and the JSON document to compare it with, and prints
exactly what is left to do for the answer you gave — which for a timed-out
`publish` is only:

```sh
git push origin <branch> <other-branch> vX.Y.Z
```

Do not run `./bless` again for that version: an accepted upload cannot be
withdrawn, and a second attempt would only trip the duplicate check.

---

## 10. Release steps

1. `git switch dev` (or `main`) and merge whatever you intend to ship.
2. Bump `__version__` in [`luks_tray/__init__.py`](../luks_tray/__init__.py) and
   commit it.
3. Push, so `origin/dev` and `origin/main` both exist and neither push is a
   fast-forward violation — the branch plan refuses otherwise.
4. `lt-venv/bin/python ./bless` and read the checklist it prints.
5. Answer `test` first if this is a new machine or a new token; install the
   rehearsal build (§8) if the change is worth trying.
6. Answer `publish`, then confirm the index wait completes.
7. After: `git status` clean, `main` and `dev` both at `vX.Y.Z`, and
   `pip install --upgrade luks-tray` in a scratch venv reports the new version.

### The CI pipeline

[`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on pushes to
`main` and `dev`, on pull requests, and on demand:

* **lint** — `ruff check .` on its own, so a style failure is not buried in a
  matrix;
* **test** — the suite on Python 3.11, 3.12 and 3.13 with the apt Qt libraries
  and `QT_QPA_PLATFORM: offscreen`;
* **package** — builds, runs [`inspect_dist.py`](inspect_dist.py) on both
  artifacts, installs the wheel into a scratch venv, checks that the five shield
  SVGs are readable as `importlib.resources`, and that `luks-tray --version`
  prints the version that was built.

`permissions: contents: read`, no `secrets`, and **no publish job** — the upload
is irreversible and belongs to a deliberate, inspectable action on the owner's
machine.  [`test_repo_hygiene.py`](test_repo_hygiene.py) fails if the workflow
grows a credential, drifts from the gates above, or loses the offscreen setting.

---

## 11. Known gaps

* **The coverage floor is not set yet.**  `[tool.coverage.report]` deliberately
  has no `fail_under`: the number should be the one the suite actually achieves,
  set once and then only raised.  Still uncovered: `DeviceInfo`'s
  `lsblk`/`/sys` parsing, the menu/refresh half of
  [`Main.py`](../luks_tray/Main.py) (`update_menu`, `update_menu_items`,
  `update_history`, `busy_operation`), and the three dialog subclasses
  (`MasterPasswordDialog`, `MountDeviceDialog`, `MountFileDialog`).  Tracked in
  [`BLESS-ADOPTION-PLAN.md`](BLESS-ADOPTION-PLAN.md) Phase 5a.
* **The ruff selection is narrow on purpose** — `E4`, `E7`, `E9`, `F`, the set the
  package has always been clean under.  Widening it (56 pre-existing findings) is
  [`AUDIT.md`](AUDIT.md) medium #11.
* **Sway/Wayland support is open**, with four items in
  [`AUDIT.md`](AUDIT.md); the `archive/sway` tag is where the abandoned attempt
  lives.
* **TrueCrypt/VeraCrypt support is deliberately one-sided.**  Adding and
  unmounting a file container is implemented and exercised against a container
  made with `tcplay`; discovery, creation, non-default PIMs and hidden/system
  volumes are not.  [`ContainerType.py`](../luks_tray/ContainerType.py) says
  why the header type has to be *asked for* rather than read, and §12 is how to
  rebuild that container and re-run the check by hand.

---

## 12. A real TrueCrypt/VeraCrypt container

§11 says the TrueCrypt/VeraCrypt support is one-sided.  The missing half cannot
be reached by a test at all: what needs checking is cryptsetup's `rc=1`
fallback, the salt identity, and a mount/close cycle over a file that `blkid`
cannot see.  All three can be checked by hand in a couple of minutes against one
container made with `tcplay`.  This is that procedure.

Nothing under `tests/` reads the container.  The gate is hermetic by
construction (§3), and the same decisions are pinned there with fakes —
[`test_container_type.py`](test_container_type.py), the `unlock_container()`
cases in [`test_luksops.py`](test_luksops.py), and
`test_the_probe_identifies_an_encrypted_header_by_its_salt` in
[`test_history.py`](test_history.py).  The container's job is to prove that
cryptsetup and the kernel agree with those fakes.

### Where it lives

```text
~/Vaults/test-only-tcrypt.img
```

* `~/Vaults` is the default `auto_mount_folder`, so the mount point the tray
  makes during the run lands beside it.  It is safe there: the startup pass
  ([`remove_unused_automounts()`](../luks_tray/Main.py)) removes empty
  *directories* and skips everything that is not one, so a `.img` file is never
  a candidate.
* `test-only-` is load-bearing.  The passphrase below is published; this file
  must never be a vault anybody cares about.
* **`tcrypt`, not `veracrypt`.**  `tcplay` writes a TrueCrypt header.  That is
  measured rather than assumed: `cryptsetup --debug --veracrypt-pim 0
  tcryptDump` reports `TCRYPT: Magic: TRUE, Header version: 5` and settles on
  `pbkdf2-whirlpool-1000`, a TrueCrypt iteration count — VeraCrypt's `VERA`
  magic comes with counts in the hundreds of thousands.  A file named
  `…-veracrypt` would misdescribe itself in exactly the way
  [`ContainerType.py`](../luks_tray/ContainerType.py) exists to prevent.

So one container is enough.  `veracrypt` is not in Debian — `apt-cache search
veracrypt` returns `tcplay`, `libtcplay` and `libtcplay-dev`.  If you ever want
the `VERA` variant too, build it with the upstream tool from veracrypt.fr, save
it as `~/Vaults/test-only-veracrypt.img`, create it at PIM 0 and run the same
checklist; it adds the higher iteration count and nothing else this code does
differently.

### Install and create it (Debian)

```sh
sudo apt install tcplay                 # trixie/main, 3.3
```

`tcplay` needs a **block device**: pointed at the image file it stops with
`could not get disk info`.  Its passphrase comes from `/dev/tty`, and it flushes
the queue before each prompt, so `printf 'vaultpass\n' | sudo tcplay …` is
discarded and it reports `could not read passphrase`.  Type the three answers:

```sh
truncate -s 64M ~/Vaults/test-only-tcrypt.img
LOOP=$(sudo losetup --find --show ~/Vaults/test-only-tcrypt.img)
sudo tcplay -c -d "$LOOP" -b AES-256-XTS -a whirlpool
#   Passphrase: vaultpass
#   Repeat passphrase: vaultpass
#   Are you sure you want to proceed? (y/n) y
sudo losetup -d "$LOOP"
```

Add `-z` to skip the secure erase — seconds at 64 MB, and luks-tray only ever
reads the header.

Then a filesystem.  A fresh `tcplay` volume holds a header and nothing else, so
without this the dialog's mount fails with the misleading `wrong fs type, bad
option, bad superblock`:

```sh
printf 'vaultpass' | sudo cryptsetup open --type tcrypt --veracrypt-pim 0 \
    --key-file - ~/Vaults/test-only-tcrypt.img test-only-tcrypt.img-luks
sudo mkfs.ext4 -L luks-tray-test /dev/mapper/test-only-tcrypt.img-luks
sudo cryptsetup close test-only-tcrypt.img-luks
```

`test-only-tcrypt.img-luks` is not a name of your choosing: it is
`<basename>-luks`, which is what
[`mount_luks_container()`](../luks_tray/LuksOps.py) derives and what the menu
looks for in `/dev/mapper` after a restart.  Note that `cryptsetup` reads the
file directly, with no loop device — the asymmetry with `tcplay` above.

### The checklist

**1. There is nothing to detect.**

```sh
blkid -o value -s UUID ~/Vaults/test-only-tcrypt.img; echo "rc=$?"
# <nothing>
# rc=2
```

No signature and no UUID, which is why the identity falls back to the 64-byte
salt in [`ContainerType.py`](../luks_tray/ContainerType.py):

```sh
lt-venv/bin/python -c "from luks_tray.LuksOps import get_container_identity as g; print(g('$HOME/Vaults/test-only-tcrypt.img'))"
# salt-17a9cbd25b840999bf5859ce99b47880
```

Stable across mounts, different once the container is re-made.

**2. The type is wrong once, and that is the signal.**

```sh
printf 'vaultpass' | sudo cryptsetup open --type luks --key-file - \
    ~/Vaults/test-only-tcrypt.img probe
# Device ... is not a valid LUKS device.                             rc=1
```

`rc=1` is the only answer `unlock_container()` reads as "ask for the other
type".  A wrong passphrase is a different verdict, and is never retried:

```sh
printf 'wrongpass' | sudo cryptsetup --veracrypt-pim 0 tcryptDump \
    ~/Vaults/test-only-tcrypt.img
# No device header detected with this passphrase.                    rc=2
```

Retrying `rc=2` would double the cost of every typo and could not succeed: the
type was not what was wrong.

**3. The tray, with a throwaway history.**

```sh
HOME=$(mktemp -d) ./runner
```

Add file container → this path → `vaultpass`.  Expect: an entry appears in the
menu although `lsblk` knows nothing about the file; the dialog shows
`Type: TrueCrypt/VeraCrypt` where a LUKS container shows a UUID; unmount then
close leaves no `/dev/mapper/test-only-tcrypt.img-luks`; and restarting the tray
keeps the entry.

`HOME` is the variable that moves the history, not `XDG_CONFIG_HOME`: the config
folder is `$HOME/.config/luks-tray`, built from `get_user_home()`.  Without it a
published passphrase ends up in your real `~/.config/luks-tray/history.json`.

**4. The purge, which is why the identity is the salt.**

Run the create block again, at the same path.  The entry from step 3 has to be
gone after the next start: same name, new salt, and the old password no longer
opens that file.  Restarting *without* touching the file has to keep it — that
pair is what `_probe_back_file_identity()` and its purge loop are balanced
against.

Mounting is two layers, which shows in step 3: the container is mounted and then
bindfs-mounted over it to hand the files back to your uid, so one unmount
removes one layer.  [`MountFileDialog.unmount_file()`](../luks_tray/dialogs/MountFile.py)
unmounts twice on purpose; by hand, do the same.

### What this still does not cover

`VERA` headers and VeraCrypt's iteration count (they need the upstream build),
non-default PIMs (the tray always passes `--veracrypt-pim 0`, and from outside a
wrong PIM is indistinguishable from a wrong passphrase — `rc=2`), and hidden or
system volumes.  Those are the rest of the §11 bullet, untested for the same
reason they are unsupported.

### Cleanup

```sh
sudo cryptsetup close test-only-tcrypt.img-luks 2>/dev/null
rm ~/Vaults/test-only-tcrypt.img
```

Keeping it is also fine: 64 MB in `~/Vaults` is cheaper than rediscovering any
of the above.

---

The audit itself — what was broken, what was fixed, and what remains — is
[`AUDIT.md`](AUDIT.md).  Why the release works this way, and what `bless` assumes
about a tree, is [`BLESS-ADOPTION-PLAN.md`](BLESS-ADOPTION-PLAN.md).
