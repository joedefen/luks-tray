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

The suite is **279 tests**.  It needs no network, no `sudo`, and writes nothing
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
| [`test_initool.py`](test_initool.py) | The config folder, the file it creates, and the tolerant parsing: a hand-edited `config.ini` must never be able to break start-up. |
| [`test_history.py`](test_history.py) | The clear-text and encrypted history files, the atomic write, the migrations and every corrupt-file fallback — each one is a path that loses a password silently if it is wrong. |
| [`test_luksops.py`](test_luksops.py) | The `run_cmd`/`sudo_cmd` contract, `run_unmount`'s busy-device reporting, the mapper/loop-device primitives. |
| [`test_main_helpers.py`](test_main_helpers.py) | Which directories may be deleted: `auto_mount_folder` validation, ownership, symlinks, `is_strictly_within`, `remove_empty_dir`'s escalation, `remove_unused_automounts`. |
| [`test_dialogs.py`](test_dialogs.py) | `CommonDialog`: window flags, the manual title, input fields, `check_upon`, the progress UI.  A real `QApplication` under the offscreen plugin, not a stub. |
| [`test_menu.py`](test_menu.py) | `replace_menu_if_different()` — rebuild the tray menu only when it changed.  This is the fix for the sway symptom that produced the `archive/sway` branch: rebuilding the `QMenu` on every 3 s tick closed a popup under the user's cursor. |
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

The audit itself — what was broken, what was fixed, and what remains — is
[`AUDIT.md`](AUDIT.md).  Why the release works this way, and what `bless` assumes
about a tree, is [`BLESS-ADOPTION-PLAN.md`](BLESS-ADOPTION-PLAN.md).
