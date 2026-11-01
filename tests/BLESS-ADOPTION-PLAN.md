# Adopting `bless` in `luks-tray`

**Status: Phases 1–4 done, coverage floor outstanding.** [`bless`](../bless)
is present, executable and copied verbatim and the full pipeline runs end to end
(transcript in Phase 2); the tree has **279 tests** across ten modules; CI runs
the same three gates on 3.11, 3.12 and 3.13; the developer guide is
[`tests/README-DEV.md`](README-DEV.md), with the user-facing
[`README.md`](../README.md) pointing at it. What is left is the `fail_under`
floor, which waits for the three remaining coverage targets. Each phase below
says what was verified, not what was intended.

`bless` is the release gate used by `../pwr-tray`, `../my-snaps` and
`../vappman` (all three hold the identical file, SHA-256
`4f756337840b0619fbf6f077b00d4bb3fb9aee95ff18bbeb6a0521d39d52131e`,
1015 lines). It is copied **verbatim** — the same hash is in [`bless`](../bless)
here — because reuse is "copy verbatim": every project-specific fact is read out
of [`pyproject.toml`](../pyproject.toml) and the package, so a later upstream
change is adopted as a new copy rather than as a local edit. `../dwipe/bless` is
a different file (`dead17db…`) and is not the source here.

Adopting it **replaced [`deploy`](../deploy)**, which has been deleted. That
script ran `pip install -e .` into whichever interpreter happened to be active,
while its own `echo` told you to test with `lt-venv/bin/luks-tray` — so in the
common case it installed into the wrong interpreter. Nothing referenced it:
`README.md`, [`runner`](../runner) and [`pyproject.toml`](../pyproject.toml)
never mentioned it, and the audit note in [`AUDIT.md`](AUDIT.md) that recorded
the defect now records the replacement instead. Installing for development is
one documented command, `lt-venv/bin/python -m pip install -e '.[dev]'`.

## Why adopt it at all

`bless` makes one path to an upload and runs every check on the way, every time:
clean tree, no already-published version, ruff, compileall, pytest with
coverage, a build into a fresh directory, an inspection of the two built files
by the same inspector CI uses, `twine check --strict`, and an install-and-run
smoke test of the wheel in a throwaway virtualenv. Only then does it ask
`====> Enter test or publish or quit:` and only the typed answer decides the
irreversible part. The rehearsal (`test`) uploads a throwaway `X.Y.Z.99.N` to
TestPyPI from a *copy* of the tree, so a rehearsal never spends the working
version.

For this project the ordering matters more than the ceremony: `luks-tray 2.0.5`
is on PyPI, and an upload cannot be replaced, so a duplicate check that runs
*before* anything is built is the only place that mistake can be caught.

## What `bless` assumes, and where this tree stands

Requirements are taken from the `bless` module docstring ("What a project must
then provide"). Status was read from the tree, not assumed.

| `bless` requires | `luks-tray` | Status |
| --- | --- | --- |
| Flat layout: the package directory at the root | [`luks_tray/`](../luks_tray) is at the root | ok |
| `__version__ = "X.Y.Z"` in `<package>/__init__.py` | [`luks_tray/__init__.py`](../luks_tray/__init__.py) is **empty** | **done**: `__version__ = "3.0.0"` |
| The build backend reads that line | static `version = "2.0.5"` in `[project]` | **done**: `dynamic = ["version"]` |
| The version is not already published | PyPI has `1.5, 1.6, 1.7, 2.0.3, 2.0.4, 2.0.5` | **done**: the working version is `3.0.0` |
| A console script that prints the version for `--version` | `luks-tray = luks_tray.Main:main` had no `--version` | **done**: `action='version'` in [`main()`](../luks_tray/Main.py) |
| `python -m build` works, from the dev extra | `build` was not installed in `lt-venv` (3.13.5) | **done**: `build 1.6.1` |
| Dev extra: ruff, pytest, pytest-cov, build, twine>=7, packaging>=26 | no `[project.optional-dependencies]` at all | **done**: `pytest 9.2.0`, `pytest-cov 7.1.0`, `build 1.6.1`, `twine 7.0.0`, `packaging 26.3`, `ruff 0.17.3` |
| Inspector at `tests/inspect_dist.py` | missing — `tests/` held only `AUDIT.md` | **done**: [`tests/inspect_dist.py`](inspect_dist.py) |
| A `pytest` suite the gates can run | `pytest` exited 5, which `bless` reads as a failed gate | **done**: 279 tests, 54.1% of 1893 statements |
| Clean working tree | clean; `.ruff_cache/`, `*.whl`, `*.tar.gz`, `.coverage*` were not ignored | **done**: [`.gitignore`](../.gitignore) covers the gate's output |
| `vX.Y.Z` tag absent | no version tags exist (only `archive/sway`) | ok |
| `main` and `dev` exist locally **and** as `origin/*` | both do; `dev` is 4 commits ahead of `origin/dev` | push `dev` before the first `bless` run from `dev` |
| Credentials per index, kept out of the tree | none in the tree | `~/.pypirc` or `TWINE_PASSWORD` / `TWINE_TESTPYPI_PASSWORD` |
| A GitHub Actions pipeline | none | **done**: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) |

Two consequences worth stating plainly:

* `bless` **refuses** a dirty tree, so the working tree must be committed before
  it can run at all. That is a property to rely on, not a nuisance: it is what
  makes "the tag describes what was tested" true.
* The gates run `sys.executable`, so the interpreter that runs `bless` is the
  interpreter that must have the dev extra and PyQt6. The shebang is
  `#!/usr/bin/env python3` (system Python here, without PyQt6), so the
  documented invocation is **`lt-venv/bin/python ./bless`**, not `./bless`.

## Decisions this plan takes

1. **`3.0.0` is the version.** Owner's call, and the right one twice over:
   `2.0.5` is on PyPI so the duplicate check refuses it, and the audit
   remediation plus the packaging work since then is not a patch-release story.
   Significant features are still to come in the 3.x line.
2. **`deploy` is deleted**, not repaired. `bless` covers the same intent (build
   it, install it, run it) with checks, and the missing piece `deploy` had —
   "install into *this* environment" — is a one-line instruction rather than a
   script that guesses at the interpreter.
3. **The ruff selection is pinned in `pyproject.toml`.**
   `select = ["E4", "E7", "E9", "F"]` — the rule set this code has always been
   kept clean under — because `bless` runs `python -m ruff check .` with no
   flags, and ruff 0.17's default set now adds pyupgrade, isort, simplify,
   blind-except, shebang and `ruff`-specific rules, which report **56 findings**
   across the package (all pre-existing). Pinning means `bless`, CI and a
   developer run one rule set, and a ruff upgrade cannot redden the release gate
   by preferring different style. Widening it stays recorded as medium #11 in
   [`AUDIT.md`](AUDIT.md) — and when that happens, `tests/*` will need `S101`
   and `S108` (assertions, `/tmp` paths).
4. **No `smoke_args`.** `[tool.bless] smoke_args` adds an argv to the smoke run;
   `--check-deps` is the only non-interactive candidate and it exits 1 when a
   system program is absent, so on a CI runner without `cryptsetup` it would
   fail the gate for a packaging reason. The version probe is the smoke test.
5. **The suite imports the real PyQt6**, it does not stub Qt. `pwr-tray` stubs
   PyQt5; here every dependency is on PyPI and the behaviour worth pinning
   (window flags, the manual title label, dialog placement, menu rebuilds) is Qt
   behaviour, which a stub would only pretend to exercise. CI installs
   `libgl1 libegl1 libxkbcommon0` and runs with `QT_QPA_PLATFORM=offscreen`.
6. **The coverage floor is set after the first measured run** and then only
   raised (`fail_under`, read by `pytest-cov` itself so CI and a developer
   enforce one number).
7. **Developer documentation lives under `tests/`**, not beside the user-facing
   `README.md`: `tests/README-DEV.md`, next to the audit and this plan.

## Phase 1 — the release plumbing (done)

1. [`luks_tray/__init__.py`](../luks_tray/__init__.py): `__version__ = "3.0.0"`,
   documented as the single source of truth.
2. [`pyproject.toml`](../pyproject.toml): `dynamic = ["version"]`; the `dev`
   extra; `[tool.pytest.ini_options]`, `[tool.coverage.*]` (branch coverage,
   `show_missing`, no floor yet); `[tool.ruff]` pinned selection; and
   `[tool.flit.sdist] include = ["tests/"]` so the sdist can run its own gate.
3. `--version` in [`main()`](../luks_tray/Main.py), handled by argparse so it
   exits before `IniTool` is built and before stdin is detached from the
   terminal.
4. [`tests/inspect_dist.py`](inspect_dist.py): the same shape as `pwr-tray`'s
   inspector (expectations read from `pyproject.toml` and the package, never
   from the artifact), asserting `Name`, `Version`, `Summary`,
   `Requires-Python`, `License-Expression`, `License-File`, keywords, runtime
   `Requires-Dist` names, the five shipped SVGs, a `LICENSE` inside `.dist-info`,
   and no `tests/`, virtualenv or bytecode; and for the sdist, the manifest,
   README, licence, package, inspector, plus the embedded `__version__` equal to
   the artifact's.
5. [`.gitignore`](../.gitignore): `build/`, `dist/`, `.ruff_cache/`,
   `.pytest_cache/`, `.coverage*`, `coverage.xml`, `*.whl`, `*.tar.gz`.
6. `bless` copied from `../pwr-tray/bless`, `chmod +x`, hash recorded above.
7. `deploy` deleted; the audit paragraph now describes the replacement.
8. `lt-venv/bin/python -m pip install -e '.[dev]'`.

**Verified:** `lt-venv/bin/python -m build` produces
`luks_tray-3.0.0.tar.gz` and `luks_tray-3.0.0-py3-none-any.whl` with the
version read from the module; `tests/inspect_dist.py` accepts that pair;
`lt-venv/bin/luks-tray --version` → `luks-tray 3.0.0` and
`python -m luks_tray.Main --version` → `Main.py 3.0.0`; `ruff check .` →
*All checks passed* under the pinned selection; `git ls-files deploy` empty;
link checker 282 links in 16 files, all resolve.

And `lt-venv/bin/python ./bless` at commit `79f16d6` ran as designed and stopped
exactly where this plan said it must — this is the *before* picture; Phase 2
closes with the after one:

```
[bless] luks-tray 3.0.0
[preflight] clean tree at 79f16d6 on dev
[index] luks-tray 3.0.0 is not on PyPI (6 versions exist; latest is 2.0.5)
[gate] lint          → All checks passed!
[gate] compile       → clean
[gate] tests         → no tests ran; 0.0% coverage; exit 5
bless: refused: the tests gate failed (exit 5)
```

That is the ordering doing its job: the duplicate check ran before anything was
built, the gates ran before the prompt, and the prompt was never reached. The
coverage report it printed is the measure of the job ahead — **1893
statements** across the package, of which `Main.py` (550) and
`dialogs/Common.py` (247) are the two largest targets.

## Phase 2 — the test suite (done: 274 tests, 54.1% of 1893 statements)

**Done.** Everything planned below is in the tree, and the whole `./bless`
pipeline runs end to end on it — the transcript is at the end of this section.

This was the whopper: `bless` would not pass without it, and CI had nothing to
run. The raw material already existed as the throwaway harnesses in `/tmp`
(`luks_fix8_check.py`, `luks_fix10_check.py`, `luks_fix11_check.py`,
`luks_fix16_check.py`, `luks_cleanup_check.py`, `luks_split_check.py`), written
to prove one fix each and then abandoned; they become assertions here instead of
being rewritten from scratch.

* `conftest.py`: point `HOME`, `XDG_CONFIG_HOME` and the package's config path at
  a temporary directory so no test can write to `~/.config/luks-tray`, and
  provide the fixtures the suite shares (a fake `IniTool`, a `QApplication`
  fixture guarded by `QT_QPA_PLATFORM=offscreen`). It joins the sdist include
  list when it exists.
* Pure logic, no Qt — the bulk of the coverage:
  [`History`](../luks_tray/History.py) (fernet round trip, salt/iterations,
  atomic write and file mode, `restore()` on missing/corrupt/legacy data,
  `_probe_back_file_uuid`), [`IniTool`](../luks_tray/IniTool.py) (defaults,
  `to_array` parsing, `get_current_val`, config-change detection),
  [`Utils`](../luks_tray/Utils.py) (`configure_log` rotation and reopening,
  `prt_once` deduplication), [`LuksOps`](../luks_tray/LuksOps.py) (`run_cmd`,
  `run_cmd_capture`, `run_unmount` busy/no-fs/anomaly cases,
  `generate_uuid_for_file_path`), and the `LuksTray` static helpers
  (`validate_auto_mount_root`, `is_strictly_within`, `remove_empty_dir`,
  `is_empty_dir`, `generate_auto_mount_folder`, `get_real_user_ids`,
  `get_real_user_home`, `expand_real_user`).
* Qt behaviour, with `QT_QPA_PLATFORM=offscreen`:
  [`set_title()`](../luks_tray/dialogs/Common.py#L96) inserting the body heading
  on every compositor, [`Compositor.detect()`](../luks_tray/Compositor.py)
  reporting the right window rule,
  [`showEvent()`](../luks_tray/dialogs/Common.py#L115)
  placement arithmetic, and
  [`replace_menu_if_different()`](../luks_tray/Main.py#L479) rebuilding only on
  change (the sway menu regression).
* Metadata and repository hygiene: `test_metadata.py` (one version location,
  `dynamic = ["version"]`, the console script, `--version` matching
  `__version__`), `bless` present and executable with the recorded hash,
  `.gitignore` keeping gate output out of `git status`, and the sdist include
  list still naming the tests.
* The gate's own pure parts, ported from `../pwr-tray/tests/test_bless.py`:
  identity derivation, the exact `test`/`publish`/`quit` prompt handling, the
  `X.Y.Z.99.N` arithmetic, and `branch_plan`'s refusals. `bless` is loaded by
  path (`importlib`) because it is a script, not a module.

**What landed.** Nine modules plus `conftest.py` — `test_utils.py`,
`test_initool.py`, `test_history.py`, `test_luksops.py`,
`test_main_helpers.py`, `test_dialogs.py`, `test_menu.py`, `test_metadata.py`,
`test_repo_hygiene.py` and `test_bless.py` — 274 tests in 1.8 s. Per module:
`Utils` 57.9%, `IniTool` 62.8%, `History` 93.8%, `LuksOps` 96.9%,
`dialogs/Common.py` 85.5%, `DeviceInfo` 14.5%, `Main.py` 44.6%.

Two real defects fell out of writing them (medium #12 in
[`AUDIT.md`](../tests/AUDIT.md)), both on error paths: `run_unmount()` called
`QMessageBox.set_title`, which does not exist, so the "device busy" dialog
raised `AttributeError` instead of appearing; and `hide_progress()` tested
`hasattr()` on an attribute that `__init__` always sets, so the `finally:` calls
in the dialogs' unmount handlers hit `'NoneType' has no attribute 'hide'`.

**Still uncovered, and why the floor waits.** `DeviceInfo`'s `lsblk`/`/sys`
parsing, the menu-building and refresh half of `Main.py` (`update_menu()`,
`update_menu_items()`, `update_history()`, `busy_operation()`) and the three
dialog subclasses (`MasterPasswordDialog`, `MountDeviceDialog`,
`MountFileDialog`) are the largest remaining statements and the least pure: the
subclasses need a stand-in tray with a history, containers and a
`busy_operation()` context manager. The `fail_under` floor goes in once they are
covered, so it is a ratchet rather than a number chosen to make tonight's build
green.

**Verified — the whole gate, end to end** (`lt-venv/bin/python ./bless`, answer
`quit`):

```
[bless] luks-tray 3.0.0
[preflight] clean tree at dd0ea7e on dev
[index] luks-tray 3.0.0 is not on PyPI (6 versions exist; latest is 2.0.5)
[gate] lint / compile / tests  → 274 passed, 54.1% coverage
[build] build/bless-3.0.0 (wiped first) → luks_tray-3.0.0.tar.gz + .whl
[inspect] both files against tests/inspect_dist.py
[check] twine check --strict → PASSED, both
[smoke] installed the wheel into build/bless-3.0.0/smoke-venv
[smoke] installed wheel reports luks-tray 3.0.0
[branch] would fast-forward main to dd0ea7e
====> Enter test or publish or quit:  (quit)
[stop] nothing was tagged, uploaded or pushed
```

The smoke stage is worth reading twice: it pip-installed the built wheel into a
throwaway virtualenv — pulling real PyQt6 — and ran `luks-tray --version` there,
which is the only way to prove that the console script, the packaged SVGs and
the version line agree in an installation rather than in the source tree.

## Phase 3 — CI (done, commit `13e2b42`)

`.github/workflows/ci.yml`, modelled on `../pwr-tray/.github/workflows/tests.yml`,
three jobs — as planned below, with two additions that only became clear while
writing it: `libdbus-1-3` alongside the GL libraries (Qt's platform plugin links
it), and the same apt step in the **package** job, because the installed console
script imports Qt and would abort without it. There is no publish job and no
credential anywhere in the file; `tests/test_repo_hygiene.py` asserts both, so a
future edit cannot quietly add one.

* **lint** — `ruff check --output-format=github .` on the in-repo config only.
* **test** — matrix `3.11`, `3.12`, `3.13`; `apt-get install -y libgl1 libegl1
  libxkbcommon0`; `pip install -e '.[dev]'`; `compileall -q luks_tray tests`;
  `pytest -q --cov=luks_tray --cov-report=term-missing` with
  `QT_QPA_PLATFORM=offscreen`. The coverage floor is not repeated in the
  command: `pytest-cov` reads `fail_under` from `pyproject.toml`.
* **package** — `python -m build`, then
  `python tests/inspect_dist.py dist/*.whl dist/*.tar.gz`, then install the
  wheel into a scratch venv, import the package, load each resource through
  `importlib.resources`, and run `luks-tray --version` against the version the
  built package reports. Credential-free: CI is a gate, the upload stays local.

Triggers: `push` on `main` and `dev`, `pull_request`, `workflow_dispatch`;
`permissions: contents: read`; a `concurrency` group that cancels superseded
runs.

**Verified as far as a local machine can.** The YAML was parsed with PyYAML and
its structure asserted (three jobs, every step has a `run` or a `uses`, the job
names and the environment); the workflow's commands were run locally one by one,
which is what `./bless` does anyway. The matrix's lowest version was executed
for real: a throwaway `python3.11 -m venv` with `-e '.[dev]'` runs the same
**279 tests** at the same 54.1% coverage as 3.13. That also settles the caveat
in [`AUDIT.md`](../tests/AUDIT.md) #6, where 3.11 support rested on ruff's
`py311` parser because no 3.11 interpreter was installed.

## Phase 4 — documentation (done)

[`tests/README-DEV.md`](README-DEV.md) is the developer guide: the one version
source and why a second copy is a defect, the setup with the `[dev]` extra, the
three gates (including why `ruff check .` runs with no flags), what each test
module pins, the `bless` stages with what each one refuses, the
`test`/`publish`/`quit` prompt, the per-index credentials and their two
`~/.pypirc` repository URLs, the rehearsal install line with its cache purge and
`--extra-index-url`, the ~14-minute index wait and why a timed-out verification
is not a failed release, the release steps, the CI jobs, and the known gaps.

It lives under `tests/` on purpose: the root belongs to the user-facing
[`README.md`](../README.md), which carries only a short "Development" section --
how to run from the tree, the `.[dev]` install line, the three commands,
`./bless` as the only upload path, the one-version-source warning, and two links,
to [`README-DEV.md`](README-DEV.md) for the release procedure and to
[`AUDIT.md`](AUDIT.md) for what is still open.

**Verified:** every number in the guide is one the tree produces, not an
estimate -- 279 tests, the dev extra's package list, the 853 s verification
budget (`sum(VERIFY_SCHEDULE)`), the floors `twine>=7` / `packaging>=26`, and the
two token names and two `pypirc` URLs taken from `bless`'s own `PYPI` and
`TESTPYPI` tables. The link checker reports **329 links in 29 files, all
resolving**. Writing it also caught two of my own mistakes before the commit: a
duplicated section number (two "10"s) and the cross-reference that pointed at it.

## What is deliberately out of scope

* No CI publish job. The upload is irreversible and the owner runs it from the
  tree, which is why the safety lives in an inspectable local script.
* No `[tool.bless] inspect = ""` shortcut: the inspector is shared with CI so
  the two cannot disagree.
* No metadata changes beyond `dynamic = ["version"]`. The classifiers,
  `license-files` and the `Homepage`/`Bug Tracker` URLs stay as the PEP 639 pass
  left them.
* `images/` stays at the root and is not shipped in the sdist; that is the
  existing state of the published package, and changing it is a README/asset
  decision, not a release-gate one.
