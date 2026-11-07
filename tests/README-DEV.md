# Developing `luks-tray`

How to set up a tree, what the gates are, what the tests pin, and how a release
leaves this repository.

This file lives under `tests/` on purpose: the root belongs to
[`README.md`](../README.md), which is for people who *use* the tray.  The
development material — this file, plus the tests it describes — sits next to
them, and open items are listed in the `TODOs` section at the end of
[`README.md`](../README.md).

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
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The `dev` extra is exactly what the gate needs — `pytest`, `pytest-cov`, `ruff`,
`build`, `twine>=7`, `packaging>=26` — on top of the runtime dependencies.  `-e`
also installs the console script, so `.venv/bin/luks-tray` works from the tree.

To run the tray from the source tree without installing anything at all:

```sh
./runner                     # or: .venv/bin/python runner
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
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q luks_tray conftest.py tests
.venv/bin/python -m pytest -q --cov=luks_tray --cov-report=term-missing
```

* `ruff check .` takes **no flags** in the gate.  The rule selection is pinned in
  [`pyproject.toml`](../pyproject.toml) under `[tool.ruff.lint] select` instead of
  being inherited from whatever the installed ruff calls its default — ruff 0.17
  widened that default set and reports about 50 pre-existing findings in this
  package.  The reasoning is in the comment there; widening the selection is a
  TODO in [`README.md`](../README.md), not something to smuggle into a release.
* `compileall` parses every file *including* the tests, so a syntax error cannot
  hide behind pytest's collection order.
* `pytest` reads `[tool.pytest.ini_options]` and `[tool.coverage.run]` /
  `[tool.coverage.report]`; coverage is collected with `branch = true`, because
  the misses that matter here are the arms of an `if` nobody takes — the
  container state machine and the mount/unmount error paths.

The suite is **669 tests**.  It needs no network, no `sudo`, and writes nothing
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
| [`test_utils.py`](test_utils.py) | Where `prt()` writes: terminal, redirected file, configured log, and `-o/--stdout` keeping a pipe.  The redirection runs in a subprocess, because `configure_log()` calls `os.dup2()`, which would replace pytest's own output.  Also the log's owner-only mode, the one-generation rotation to `debug.log.old`, and the viewer argv: which terminal is picked, that a GLib terminal gets one quoted command line, and that `less --follow-name` is probed once and only when the installed `less` reports it.  A terminal is not taken over but *mirrored into*: the line reaches both, `-o` suppresses the file copy, the mirror rotates at the cap, and a stdout that already *is* the log is not written twice (`_same_file`).  Plus the session bookkeeping the menu reads: which lines count as failures (`ERROR`/`FAIL:`/`exception:`, never `WARN`), the `=== … started/stopped ===` banner with name, version and pid, and the signal handler that writes the stop line and then re-raises the signal. |
| [`test_initool.py`](test_initool.py) | The config folder, the file it creates, and the tolerant parsing: a hand-edited `config.ini` must never be able to break start-up.  Also the write side (`set_values`): all-or-nothing validation, atomic replace, unknown settings preserved. |
| [`test_history.py`](test_history.py) | The clear-text and encrypted history files, the atomic write, the migrations and every corrupt-file fallback — each one is a path that loses a password silently if it is wrong.  Also `lock()`, which has to run *after* the file was read: it forgets the master password and the decrypted entries together, leaves the file untouched, and leaves the `save()` guard refusing a later clear-text write. |
| [`test_container_type.py`](test_container_type.py) | The salt, which is the whole identity of a container whose header is encrypted: the 64-byte length check, unreadable, missing and short files, that nothing after the salt matters, and that a re-created container gets a different one. |
| [`test_luksops.py`](test_luksops.py) | The `run_cmd`/`sudo_cmd` contract, `run_unmount`'s busy-device reporting, the mapper/loop-device primitives. |
| [`test_main_helpers.py`](test_main_helpers.py) | Which directories may be deleted: `auto_mount_folder` validation, ownership, symlinks, `is_strictly_within`, `remove_empty_dir`'s escalation, `remove_unused_automounts`. |
| [`test_dialogs.py`](test_dialogs.py) | `CommonDialog`: window flags, the manual title, input fields, `check_upon`, the progress UI.  A real `QApplication` under the offscreen plugin, not a stub. |
| [`test_menu.py`](test_menu.py) | `replace_menu_if_different()` — rebuild the tray menu only when it changed.  This is the fix for the sway symptom that produced the `archive/sway` branch: rebuilding the `QMenu` on every 3 s tick closed a popup under the user's cursor.  Also the fixed entries at the bottom: every one starts with a symbol, `⚙ Options…` opens the dialog, the `✎` master-password entry sits directly below it and is worded by the state, `≡ Open Log` is offered only when a terminal can be found (and spawns the viewer detached, since it outlives the applet), it turns into `≡ Open Log [n errors]` (`[1 error]` in the singular) once the session reported a failure and goes back to the plain label after a new session starts, and the versioned `↺ Restart luks-tray …` entry is last — carrying `(locks history)` only while the history is unlocked.  `lock_history()` is here too: one screen lock produces two or three events and must cost one lock, one menu rebuild and one log line, and a locked history must skip the container merge so the poller cannot write what it could not read. |
| [`test_qt_messages.py`](test_qt_messages.py) | What Qt's own console output is allowed to put in the log: debug and information levels dropped, the palette dump of the platform theme dropped by text — both the qt6ct and the qt5ct spelling, since it arrives at warning level on some builds — Qt's shutdown `QThreadStorage … destroyed` notice dropped by its prefix, a warning kept once per distinct message with the C++ `[file:line]` Qt reports as its footer (`[qt]` when the build has no log context), and `install_qt_messages()` registering exactly that handler. |
| [`test_signal_wakeup.py`](test_signal_wakeup.py) | That a stop signal reaches its Python handler without waiting for a poll tick: `signal.set_wakeup_fd()` puts the signal number into the pipe (so the byte is there and the handler has run), the `QSocketNotifier`'s slot empties it — a slot that left bytes behind would fire on every loop iteration — and a second `install_signal_wakeup()` reuses the pipe the loop already watches instead of leaving the handlers deaf.  The handler's own behaviour (log, then die of the signal) is pinned in [`test_utils.py`](test_utils.py). |
| [`test_options_dialog.py`](test_options_dialog.py) | `OptionsDialog`: what it shows (the values the tray is using) and what it writes (only values `update_config()` accepts back), the refused `auto_mount_folder` paths and the unparseable `log_viewer` command that keep the dialog open, and that the four settings are the whole form — a history that raises on any access proves the password half has left this dialog. |
| [`test_tray_watch.py`](test_tray_watch.py) | Tray (re-)registration: `tray_watcher_present()` answers True/False/None instead of blocking, `TrayWatch` re-registers the item when the StatusNotifierWatcher appears or restarts, and the item is re-created (not duplicated) against a fake bus. |
| [`test_lock_watch.py`](test_lock_watch.py) | The session events that lock the history, against a fake bus: `Lock`, `LockedHint` true, `Active` false and `PrepareForSleep(true)` each call the one callback, the harmless half of each pair does not, another interface's properties are ignored, one published event is enough to be worth watching, and a session with no logind — or no QtDBus — gets no watch instead of a start-up failure. |
| [`test_single_instance.py`](test_single_instance.py) | The single-instance lock, `--replace` as the only takeover path (and only for a verified pid), `main()` exiting 3 without signalling, and `restart_self()` releasing (and, on a failed spawn, retaking) the lock. |
| [`test_service.py`](test_service.py) | The `systemd --user` unit: the one shipped template (launcher path, no pinned display server or session type, `RestartPreventExitStatus=` naming every final status — including the SIGKILL that `SendSIGKILL=yes` uses and the SIGABRT Qt raises when the display connection is already gone — a bounded `TimeoutStopSec=` with `SendSIGKILL=yes`, no `UnsetEnvironment=SESSION_MANAGER`, no `StandardOutput=`), `display_keys()` choosing the display variable from the socket that is listening, `render()` appending only the variables the installing shell exported, and every `--svc-*` verb as a `Step` list -- install is the only verb that writes, `start`/`restart` refuse a unit that runs another executable, `stop`/`uninstall` keep working on a broken install, and `status` returns 0/1/2.  The unit itself carries almost no prose; why each directive is there is §13. |
| [`test_service_restart.py`](test_service_restart.py) | The supervisor-aware paths: `restart_self()` asks systemd (`systemctl --user --no-block restart`, run synchronously so the job is queued before the applet exits) instead of spawning a child the cgroup teardown would kill, stays up when the request cannot be made, and `--replace` refuses to signal a holder that a unit supervises.  Also `Utils.in_systemd_service()` and `spawn_detached()` (viewers only -- a detached control request is what made the menu row behave like Quit). |
| [`test_metadata.py`](test_metadata.py) | One version location, one console script, and a `--version` that agrees with both. |
| [`test_repo_hygiene.py`](test_repo_hygiene.py) | Repository invariants the gate relies on: the sdist manifest, the inspector, the workflow's gates matrix, `QT_QPA_PLATFORM`, branch triggers, `permissions`, and that CI holds no credential and cannot publish. |
| [`test_bless.py`](test_bless.py) | The half only this project can answer: the identity the vendored `bless` derives from [`pyproject.toml`](../pyproject.toml) (distribution name, module, console script, branch model), `read_version()` agreeing with the package, and the inspector the release uploads behind — [`inspect_dist.py`](inspect_dist.py) run against a synthetic wheel and sdist. What the gate itself decides (branch plan, refusals, prompt, index check, emitter) is tested once, in [`../blessd/tests/test_bless.py`](../blessd/tests/test_bless.py). |

The suite imports the **real PyQt6**; it does not stub Qt.  Everything it needs
is on PyPI, and the behaviour worth pinning — window flags, the manual title
label, dialog placement, menu rebuilds — *is* Qt behaviour, which a stub would
only pretend to exercise.  That is why the gate installs the Qt libraries and
runs under `QT_QPA_PLATFORM=offscreen` instead of replacing the toolkit.

Two product bugs were found by this suite and fixed: `LuksOps.run_unmount()`
called `QMessageBox.set_title()` (the method is `setWindowTitle`), so the
busy-device dialog raised instead of appearing; and
`CommonDialog.hide_progress()` tested `hasattr(self, 'progress_label')`, which is
always true, so the dialogs' `finally:` paths hit `AttributeError` whenever the
progress widgets had never been shown.  Both are pinned by
[`test_luksops.py`](test_luksops.py) and [`test_dialogs.py`](test_dialogs.py).

[`inspect_dist.py`](inspect_dist.py) is not a test module: it is the shared
inspector that both the CI packaging job and `bless` run against the built wheel
and sdist.  One implementation, so the two cannot disagree about what a good
distribution looks like.

---

## 5. The release gate

```sh
.venv/bin/python ./bless
```

[`bless`](../bless) is the only path to an upload.  It takes **no argument and no
flag** — any stage you could name on the command line is a stage you could skip.
Use the `.venv` interpreter: the gates run `sys.executable`, and the system
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

[`bless`](../bless) is **emitted**, byte for byte, from
[`../blessd/bless`](../blessd/bless) by that repository's
[`build.py`](../blessd/build.py): 1229 lines, SHA-256
`3ef60e9e806ca58a3d900dd6622bb8be0052210da1ef26f9918bf0a1deb1e3b2`.
It is never edited here — a change is made in `blessd` and re-emitted with

```sh
python3 ../blessd/build.py --into . --force     # replaces ./bless, prints the hash
sha256sum bless                                  # must equal build.py --sha
```

[`test_repo_hygiene.py`](test_repo_hygiene.py) pins that hash.  Every
project-specific fact is read out of [`pyproject.toml`](../pyproject.toml) and
the package, so nothing here is edited per project.  The gate's policy and its
tests live in `blessd` once ([`../blessd/docs/adopting.md`](../blessd/docs/adopting.md));
what stays here is the identity this tree derives and the inspector it uploads
behind.  `bless` replaced `deploy`, which has been deleted — a second,
unchecked install path that guessed at the interpreter.

**Adoption note.**  The tree already satisfied the gate: one version location
(`dynamic = ["version"]`), a console script that answers `--version`, the
inspector at `tests/inspect_dist.py`, and a `dev` extra holding `build`,
`twine>=7` and `packaging>=26`.  Its branch model is the gate's default,
`main` and `dev`; the hash above is the build it carries, and the two commands
just above reproduce it.

Three things are deliberately *not* wired up:

* **no publish job in CI.**  The upload is irreversible and belongs to a
  deliberate, inspectable action on the owner's machine (§10);
* **no `[tool.bless] inspect = ""` shortcut.**  The inspector is shared with the
  CI packaging job, so the two cannot disagree about what a good distribution
  looks like;
* **no `smoke_args`.**  `[tool.bless] smoke_args` appends an argv to the smoke
  run, and the only non-interactive candidate, `--check-deps`, exits 1 when a
  system program is missing — on a runner without `cryptsetup` that would fail
  the gate for a packaging reason.  `luks-tray --version` in the throwaway venv
  is the smoke test.

`images/` stays at the repository root and is not shipped in the sdist; that is
the existing shape of the published package, and moving it would be a
README/asset decision rather than a release-gate one.

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
4. `.venv/bin/python ./bless` and read the checklist it prints.
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
  set once and then only raised.  A full run measures **76.5% of 3623
  statements** with `branch = true`, which is the figure the floor has to start
  from.  Still uncovered: `DeviceInfo`'s `lsblk`/`/sys` parsing, the
  menu/refresh half of [`Main.py`](../luks_tray/Main.py) (`update_menu`,
  `update_menu_items`, `update_history`, `busy_operation`), and the three dialog
  subclasses (`MasterPasswordDialog`, `MountDeviceDialog`,
  `MountFileDialog`).  Those three are the work left before the floor is worth
  setting.
* **The ruff selection is narrow on purpose** — `E4`, `E7`, `E9`, `F`, the set the
  package has always been clean under.  Widening it (about 50 pre-existing
  findings) is a TODO in [`README.md`](../README.md).
* **Sway/Wayland support is open** — the remaining items are in the `TODOs`
  section of [`README.md`](../README.md); the `archive/sway` tag is where the
  abandoned attempt lives.
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
~/.Vaults/test-only-tcrypt.img
```

Two folders, and they are not interchangeable:

* `~/.Vaults` (hidden) — **containers**.  It is already where the tray creates
  new ones (`dot_vault_dir`; README → "When creating LUKS files, the default
  folder is `~/.Vaults`"), so a test container sits beside the real ones.
* `~/Vaults` — **mount points** (`auto_mount_folder`).  Nothing but directories
  belongs there.
* `test-only-` is load-bearing: the passphrase is published, and this file must
  never be a vault anybody cares about.
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
it as `~/.Vaults/test-only-veracrypt.img`, create it at PIM 0 and run the same
checklist; it adds the higher iteration count and nothing else this code does
differently.

### Why not in `~/Vaults`

Only one of the two folders takes files.  "Add Existing Crypt File" prefills
*Mount At* with `~/Vaults`, and
[`mount_file()`](../luks_tray/dialogs/MountFile.py) turns that back into
`~/Vaults/<basename>` with one known suffix dropped
([`CONTAINER_SUFFIXES`](../luks_tray/dialogs/MountFile.py): `.luks`, `.luks2`,
`.crypt`, `.img`).  A `.img` container *in* the mount area therefore derives
`~/Vaults/test-only-tcrypt` and no longer collides with itself — that collision
(`check_upon()` answering `mount point (...) exists but is not a directory`) is
what a name without a known suffix still does: `myvault.dat` derives itself.
The list cannot cover every name somebody might choose, so the rule stays:
containers in `~/.Vaults`, mount points in `~/Vaults`.

Sitting outside the auto-mount root also keeps the container out of the startup
sweep: [`remove_unused_automounts()`](../luks_tray/Main.py) removes empty
*directories* below `~/Vaults` and skips everything else.

### Install and create it (Debian)

```sh
sudo apt install tcplay                 # trixie/main, 3.3
```

`tcplay` needs a **block device**: pointed at the image file it stops with
`could not get disk info`.  Its passphrase comes from `/dev/tty`, and it flushes
the queue before each prompt, so `printf 'vaultpass\n' | sudo tcplay …` is
discarded and it reports `could not read passphrase`.  Type the three answers:

```sh
truncate -s 64M ~/.Vaults/test-only-tcrypt.img
LOOP=$(sudo losetup --find --show ~/.Vaults/test-only-tcrypt.img)
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
    --key-file - ~/.Vaults/test-only-tcrypt.img test-only-tcrypt.img-luks
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
blkid -o value -s UUID ~/.Vaults/test-only-tcrypt.img; echo "rc=$?"
# <nothing>
# rc=2
```

No signature and no UUID, which is why the identity falls back to the 64-byte
salt in [`ContainerType.py`](../luks_tray/ContainerType.py):

```sh
.venv/bin/python -c "from luks_tray.LuksOps import get_container_identity as g; print(g('$HOME/.Vaults/test-only-tcrypt.img'))"
# salt-17a9cbd25b840999bf5859ce99b47880
```

Stable across mounts, different once the container is re-made.

**2. The type is wrong once, and that is the signal.**

```sh
printf 'vaultpass' | sudo cryptsetup open --type luks --key-file - \
    ~/.Vaults/test-only-tcrypt.img probe
# Device ... is not a valid LUKS device.                             rc=1
```

`rc=1` is the only answer `unlock_container()` reads as "ask for the other
type".  A wrong passphrase is a different verdict, and is never retried:

```sh
printf 'wrongpass' | sudo cryptsetup --veracrypt-pim 0 tcryptDump \
    ~/.Vaults/test-only-tcrypt.img
# No device header detected with this passphrase.                    rc=2
```

Retrying `rc=2` would double the cost of every typo and could not succeed: the
type was not what was wrong.

**3. The tray, with a throwaway history.**

```sh
HOME=$(mktemp -d) ./runner
```

Add file container → `~/.Vaults/test-only-tcrypt.img` → `vaultpass`, leaving
*Mount At* at its prefilled `~/Vaults`: that is what `mount_file()` turns into
`~/Vaults/test-only-tcrypt`, the `.img` suffix dropped.  Expect: **one** menu
entry, reading `~/.Vaults/test-only-tcrypt.img ~/Vaults/test-only-tcrypt` — one,
although `lsblk` cannot identify the file: it is keyed by the header's salt,
exactly as the history keys it, so no second stand-in row appears; the dialog
shows `Type: TrueCrypt/VeraCrypt` where a LUKS container shows a UUID; unmount
then close leaves no `/dev/mapper/test-only-tcrypt.img-luks`; and restarting the
tray keeps the entry.

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
sudo umount ~/Vaults/test-only-tcrypt           # twice: bindfs, then ext4
sudo cryptsetup close test-only-tcrypt.img-luks 2>/dev/null
rm ~/.Vaults/test-only-tcrypt.img
```

The emptied mount point needs no `rmdir`: the startup pass removes empty
directories below `~/Vaults`.

Keeping it is also fine: 64 MB in `~/.Vaults` is cheaper than rediscovering any
of the above.

---

What remains is listed in the `TODOs` section at the end of
[`README.md`](../README.md).  Why the release works this way, and what `bless`
assumes about a tree, is §5 above.

---

## 13. Running as a systemd Service Notes

The shipped unit — [`luks-tray.service`](../luks_tray/resources/systemd/luks-tray.service) —
carries only the comments a systemd-aware end user needs, at most two lines per
directive and nothing about the applet's internals.  The reasoning that decided
each directive lives here instead, because a unit file is read by people who
want to adjust it, not by people debugging a logout.  What a *user* needs is in
[`README.md`](../README.md) → "Starting at Login" and "Logging out"; this
section is the Why behind the file and the traps for a future edit.

### One file, and it names no display server

The applet runs in any desktop or window manager, on Wayland or X11, and the
unit is the same file in all of them.  Nothing in it names a compositor, a
desktop environment or a display server: there is no `XDG_SESSION_TYPE=` and no
`ConditionEnvironment=`, and [`test_service.py`](test_service.py) fails if one
is added back.

`XDG_SESSION_TYPE` is a freedesktop variable with exactly two values — `x11`
and `wayland` — that every session sets: niri, sway, Hyprland, KDE and GNOME
all export `wayland`; i3, XFCE and Openbox all export `x11`.  It names the
*display server*, and it was pinned in the file while the package shipped two
templates for it.  That pair is gone: one template plus
[`Session.display_server()`](../luks_tray/Session.py) covers both sessions, and
a value pinned at install time was only ever a snapshot of the login that ran
`--svc-install`.

The alternative that was measured and rejected is one unit per session guarded
with `ConditionEnvironment=XDG_SESSION_TYPE=...`.  It does work — systemd
evaluates the condition against the *user manager's* environment, not the unit's
own `Environment=` — but it buys nothing: the guard is only as good as the
manager's environment having been imported, and each unit would still carry a
display snapshot.

What *is* compositor-specific lives in the lines `--svc-install` appends below
`ExecStart=`, for example `Environment=XDG_CURRENT_DESKTOP=niri` — a snapshot of
the shell that ran the installer.  Only [`Compositor.detect()`](../luks_tray/Compositor.py)
reads it, to print `--show-window-rules` and to label the log; no behaviour of
the unit depends on it, so a unit carried to another desktop is not wrong, just
less informative.

### `--svc-install` renders, it does not template

A user unit cannot inherit the display from the compositor, so
[`service.render()`](../luks_tray/service.py) appends, *after* `ExecStart=`, the
variables the installing shell exported: the display variable the live server
uses, plus `XDG_CURRENT_DESKTOP` and `DESKTOP_SESSION`.  `after ExecStart=` is
safe because systemd collects every `Environment=` line and the last assignment
to a name wins.

Which display variable that is comes from `display_keys()`, which asks
`Session.display_server()` — the sockets, not the marker — so:

* a Wayland login pins `WAYLAND_DISPLAY` and never `DISPLAY`, although XWayland
  leaves `DISPLAY` set in that session; an X11 login pins `DISPLAY` and never a
  stale `WAYLAND_DISPLAY`;
* with both sockets live, the marker decides which is the session, and with no
  marker at all Wayland wins — the same order `Session._named_kind()` applies at
  startup, so install time and every later start agree;
* an empty or missing variable is skipped, and nothing listening (a bare `ssh`
  login) pins no display at all.  The unit still starts: the applet asks
  `loginctl` and reads the socket itself.

The appended values are a snapshot of one login.  That is why the applet
re-derives `XDG_SESSION_TYPE`, `WAYLAND_DISPLAY` and `DISPLAY` from the socket
that is actually listening before Qt starts ([`Session.py`](../luks_tray/Session.py)):
switching display servers needs no re-install, and a stale line here is
corrected rather than obeyed.

### `Environment=PATH=` must keep sbin

`cryptsetup`, `losetup`, `mkfs.ext4` and `blkid` live in `/usr/sbin` on a
non-usrmerged or Debian-style host, and `check_dependencies()` finds them with
`shutil.which()`.  A unit that pinned `PATH` without the sbin directories made
the applet exit at startup with "missing system commands" while the same shell
found every one of them.  `test_the_shipped_unit_keeps_the_sbin_directories_on_path`
holds the three directories in place.

### `Restart=on-failure`, never `always`

A logout does not stop this unit with a clean exit: the session ends, and
everything in it ends with a signal.  `Restart=always` answered that signal by
starting the applet again five seconds later, while the session was still
waiting for its processes to disappear — so it never emptied, the display
manager never got the VT, and the logout stopped on a black screen.
`on-failure` still recovers from a crash, and lets a stop signal be final,
which is also what makes `--svc-stop` stay stopped.

### `RestartPreventExitStatus=` is a list of decisions

| Value | Why it is final | Why it is *not* in the list |
| --- | --- | --- |
| `3` | another instance holds the single-instance lock | retrying fails identically |
| `15` | unhandled exception in [`Main.py`](../luks_tray/Main.py) | retrying fails identically |
| `1` | Xlib's own status for "the X server is gone" | see below |
| `4` | — | `Session.display_problem()` found no usable display *yet*: a login race, and a retry is exactly right |
| `SIGTERM` `SIGHUP` `SIGINT` `SIGPIPE` | systemd's clean-stop set, restated so the intent survives an edit to `Restart=` | — |
| `SIGKILL` `SIGABRT` | how a *dead session* ends this process | — |

`1` is the one that took a measurement to find.  When the session's final
sweep takes the X server first, Xlib ends the process with `XIO: fatal IO error
0 (Success) on X server ":0"` and exit status 1 — no signal, so a signal-only
list never saw it.  Five seconds later the unit started again, into the *next*
login, where the only cookie this process had was the previous session's: Qt
answered `could not connect to display :0`, aborted, and dumped core.  That is
the whole of "a login core-dumps the tray although `--svc-start` works" — by
hand the user manager's environment is already complete.

`SIGKILL` is what `SendSIGKILL=yes` falls back to when `TimeoutStopSec`
expires, and `SIGABRT` is what Qt raises when it finds the connection already
gone.  Either one left out is read as a crash, and the applet restarts *while
the session is still emptying* — the black screen above, one level down.

### `TimeoutStopSec=3` is the backstop under everything else

systemd's default is 90 s, and a logout cannot finish until this process is
gone: a wedged Qt event loop or an open modal dialog would hold the session
open that long.  The applet answers SIGTERM in milliseconds —
`install_signal_wakeup()` puts a `QSocketNotifier` on the pipe behind
`signal.set_wakeup_fd()`, so the handler does not wait for the next 3 s poll —
which makes 3 s a backstop for a process that cannot answer at all, not a
budget the normal path spends.

### Never add `UnsetEnvironment=SESSION_MANAGER`

On a distribution Qt (whose xcb plugin links `SmcOpenConnection`),
`SESSION_MANAGER` is the only way ksmserver can ask the applet to leave
*before* the session's stop reaches it; the applet has no logoff action of its
own, so it relies on that request rather than racing it.  On a PyQt6-wheel Qt
the variable is inert either way (that Qt opens no ICE connection), which is
why it cannot be the guarantee — `TimeoutStopSec=` is.
[`test_service.py`](test_service.py) refuses the directive, scanning
directives only so the reasoning above can still name it.

### No `StandardOutput=`

`prt()` points itself at `~/.config/luks-tray/debug.log`; a
`StandardOutput=` line gives systemd a destination it refuses to create and the
unit dies with `EXIT_STDOUT` (status 209).  `test_no_shipped_unit_redirects_stdout`
refuses the directive.

---

## 14. Removed documents

`tests/AUDIT.md` was the audit of v2.0.5 that produced the v3.0.0 remediation.
It was removed once every item had been either fixed or carried over to the
`TODOs` section of [`README.md`](../README.md); keeping a 466-line incident log
next to the tests made every later edit re-check prose that no longer matched
the code.

`tests/BLESS-ADOPTION-PLAN.md` was the record of moving the release to
[`bless`](../bless): the requirements table, the phases, and the decisions this
file now states in full (§5).  It went for the same reason, once its one open
item — the coverage floor in §11 — was the only part still describing the tree.

Both are still in the history.  Retrieve one from the commit that deleted it:

```sh
path=tests/AUDIT.md            # or tests/BLESS-ADOPTION-PLAN.md
commit=$(git log -1 --format=%H --diff-filter=D -- "$path")
git show "$commit^:$path" > "/tmp/$(basename "$path")"
```

`"$commit^"` is the tree just before the deletion, so the file comes back
exactly as it was last reviewed, stale line numbers and all.  Run
`git log --all -- <path>` to list the commits that edited it, if an earlier
version is wanted.
