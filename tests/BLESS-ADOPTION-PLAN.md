# Adopting `bless` in `luks-tray`

**Status: plan, not yet implemented.** This document is the record of what
adoption requires, verified against the tree and against PyPI on 2026-11-01.
Nothing in it has been applied yet.

`bless` is the release gate used by `../pwr-tray`, `../my-snaps` and
`../vappman` (all three hold the identical file, SHA-256
`4f756337840b0619fbf6f077b00d4bb3fb9aee95ff18bbeb6a0521d39d52131e`,
1015 lines). It is copied **verbatim**; every project-specific fact is read out
of `pyproject.toml` and the package. `../dwipe/bless` is a different file
(`dead17db…`) and is not the source here.

Adopting it **supersedes [`deploy`](../deploy)**. That script exists only to run
`pip install -e .` into whichever interpreter happens to be active, while its own
`echo` tells you to test with `lt-venv/bin/luks-tray` — so in the common case it
installs into the wrong interpreter. Nothing else in the tree references it:
`README.md`, [`runner`](../runner) and
[`pyproject.toml`](../pyproject.toml) do not mention it, and the only written
reference is the audit note in [`AUDIT.md`](../tests/AUDIT.md) that this plan
closes out.

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
then provide"). Current state was read from the tree, not assumed.

| `bless` requires | `luks-tray` now | Action |
| --- | --- | --- |
| Flat layout: the package directory at the root | [`luks_tray/`](../luks_tray) is at the root | none |
| `__version__ = "X.Y.Z"` in `<package>/__init__.py` | [`luks_tray/__init__.py`](../luks_tray/__init__.py) is **empty** | add the line |
| The build backend reads that line | [`pyproject.toml`](../pyproject.toml:8) has a static `version = "2.0.5"` | replace with `dynamic = ["version"]` |
| The version is not already published | PyPI has `1.5, 1.6, 1.7, 2.0.3, 2.0.4, 2.0.5`; **2.0.5 is the latest** | bump to `2.0.6` |
| A console script that prints the version for `--version` | `luks-tray = luks_tray.Main:main`, and [`main()`](../luks_tray/Main.py:1002) has no `--version` | add the argparse action |
| `python -m build` works, from the dev extra | `build` is **not installed** in `lt-venv` (3.13.5) | add `build>=1.2`, install `.[dev]` |
| Dev extra: ruff, pytest, pytest-cov, build, twine>=7, packaging>=26 | no `[project.optional-dependencies]` at all; `lt-venv` has ruff 0.17.3 only | add the extra |
| Inspector at `tests/inspect_dist.py`, overridable via `[tool.bless] inspect` | missing — `tests/` holds only `AUDIT.md` | add an adapted copy |
| Clean working tree | clean; `.ruff_cache/`, `*.whl`, `*.tar.gz`, `.coverage*` are not ignored | extend [`.gitignore`](../.gitignore) |
| `vX.Y.Z` tag absent | no version tags exist (only `archive/sway`) | none |
| `main` and `dev` exist locally **and** as `origin/*` | both do; `dev` is 3 commits ahead of `origin/dev` | push `dev` first |
| Credentials per index, kept out of the tree | none in the tree | use `~/.pypirc` or `TWINE_PASSWORD` / `TWINE_TESTPYPI_PASSWORD` |
| A `pytest` suite the gate can run | **none** — `pytest` exits 5 (no tests collected), which `bless` reads as a failed gate | the test suite below |

Two consequences worth stating plainly:

* `bless` also **refuses** a dirty tree, so the working tree must be committed
  before it can run at all. That is a property to rely on, not a nuisance: it is
  what makes "the tag describes what was tested" true.
* The gates run `sys.executable`, so the interpreter that runs `bless` is the
  interpreter that must have the dev extra and PyQt6. The shebang is
  `#!/usr/bin/env python3` (system Python here), so the documented invocation is
  **`lt-venv/bin/python ./bless`**, not `./bless`.

## Decisions this plan takes

1. **`2.0.6` is the version in `__init__.py`.** Not a preference: `2.0.5` is on
   PyPI and the duplicate check refuses it, listing every release. `dev` is three
   documentation-only commits ahead of `origin/dev`, so nothing published is
   changed by moving the number forward.
2. **`deploy` is deleted**, not repaired. `bless` covers the same intent (build
   it, install it, run it) with checks, and the missing piece `deploy` had —
   "install into *this* environment" — is `lt-venv/bin/python -m pip install -e
   '.[dev]'`, which is a one-line README-DEV instruction rather than a script
   that guesses at the interpreter.
3. **No `smoke_args`.** `[tool.bless] smoke_args` would add an argv to the smoke
   run; `--check-deps` is the only non-interactive candidate and it exits 1 when
   a system program is absent, so on a CI runner without `cryptsetup` it would
   fail the gate for a packaging reason. The version probe is the smoke test.
4. **The suite imports the real PyQt6**, it does not stub Qt. `pwr-tray` stubs
   PyQt5 because it is PyQt5-only metadata; here every dependency is on PyPI and
   the parts worth testing (dialogs, menu construction, window flags) are Qt
   behaviour. CI installs `libgl1 libegl1 libxkbcommon0` and runs with
   `QT_QPA_PLATFORM=offscreen`, which is how a tray app gets a real
   `QApplication` without a display.
5. **The coverage floor is set after the first measured run** and then only
   raised (`fail_under`, read by `pytest-cov` itself so CI and a developer
   enforce one number).

## Phase 1 — the release plumbing

Ordered, each step verified before the next.

1. [`luks_tray/__init__.py`](../luks_tray/__init__.py): `__version__ = "2.0.6"`,
   with a comment naming it the single source of truth.
2. [`pyproject.toml`](../pyproject.toml): delete `version = "2.0.5"`, add
   `dynamic = ["version"]`. flit_core reads the line from step 1 and enforces
   that no second copy exists.
3. [`main()`](../luks_tray/Main.py:1002): add
   `parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')`
   so the smoke run can read the built wheel's own number back out. It must
   exit before `IniTool` is built and before stdin is detached, which
   `action='version'` does by construction.
4. `[project.optional-dependencies] dev = ["pytest>=8", "pytest-cov>=5",
   "ruff>=0.6", "build>=1.2", "twine>=7", "packaging>=26"]`, then
   `lt-venv/bin/python -m pip install -e '.[dev]'`.
5. `tests/inspect_dist.py`, adapted from
   `../pwr-tray/tests/inspect_dist.py`: same shape (expectations read from
   `pyproject.toml` and the package, never from the artifact), with the resource
   list replaced by the five shipped SVGs in
   [`luks_tray/resources/`](../luks_tray/resources) and no systemd-unit check.
   Wheel: `Name`, `Version` (or the rehearsal override), `Summary`,
   `Requires-Python`, `Keywords`, runtime `Requires-Dist` names, `LICENSE`
   carried, every resource present, no `tests/`, no `.venv`, no bytecode.
   sdist: `pyproject.toml`, `README.md`, `LICENSE`, `luks_tray/__init__.py`,
   `luks_tray/Main.py`, `conftest.py` and the test files present, the embedded
   `__version__` equal to the artifact version, no venv or bytecode.
6. `[tool.flit.sdist] include = ["tests/", "conftest.py"]` so the sdist can run
   its own gate.
7. `[tool.pytest.ini_options] testpaths = ["tests"]`, `addopts = "-ra"`;
   `[tool.coverage.run] branch = true`, `source = ["luks_tray"]`;
   `[tool.coverage.report] show_missing = true`, `fail_under` after measuring.
8. `[tool.ruff] target-version = "py311"`, `line-length` and
   `extend-exclude = ["build", "dist", "lt-venv"]`; keep the existing
   `[tool.ruff.lint] ignore = ["N999"]` and add `per-file-ignores` for `tests/*`
   (`S101`, `S108`) once the suite exists.
9. [`.gitignore`](../.gitignore): `build/`, `dist/` (present), plus
   `.ruff_cache/`, `.pytest_cache/`, `.coverage`, `.coverage.*`, `coverage.xml`,
   `*.whl`, `*.tar.gz`.
10. Copy `bless` from `../pwr-tray/bless`, `chmod +x`, and record its SHA-256 in
    this document. Do not edit it: reuse is "copy verbatim", and a later upstream
    change is adopted as a new copy.
11. Delete [`deploy`](../deploy); point the `deploy` paragraph in
    [`AUDIT.md`](../tests/AUDIT.md) at this document instead of describing a fix
    that will not happen.
12. Commit on `dev`, push `dev` (so `origin/dev` exists and is not behind).

## Phase 2 — the test suite (the part that does not exist yet)

This is the "whopper": `bless` will not pass without it, and CI has nothing to
run. The raw material already exists as the throwaway harnesses in `/tmp`
(`luks_fix8_check.py`, `luks_fix10_check.py`, `luks_fix11_check.py`,
`luks_fix16_check.py`, `luks_cleanup_check.py`, `luks_split_check.py`), which
were written to prove one fix each and then abandoned; they become assertions
here instead of being rewritten from scratch.

* `conftest.py`: point `HOME`, `XDG_CONFIG_HOME` and the package's config path at
  a temporary directory so no test can write to `~/.config/luks-tray`, and
  provide the fixtures the suite shares (a fake `IniTool`, a `QtBot`-free
  `QApplication` fixture guarded by `QT_QPA_PLATFORM=offscreen`).
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
  [`requires_manual_title()`](../luks_tray/dialogs/Common.py:38) per
  compositor, [`set_title()`](../luks_tray/dialogs/Common.py:96) inserting the
  in-dialog label, [`showEvent()`](../luks_tray/dialogs/Common.py:129) placement
  arithmetic, and [`replace_menu_if_different()`](../luks_tray/Main.py:479)
  rebuilding only on change (the sway menu regression).
* Repository hygiene, ported from `../pwr-tray/tests/test_repo_hygiene.py`:
  no second copy of the version in `pyproject.toml`, `bless` is present and
  executable with the recorded hash, `.gitignore` keeps gate artifacts out of
  `git status`, and the sdist include list still names the tests.
* The gate's own pure parts, ported from `../pwr-tray/tests/test_bless.py`:
  identity derivation, the exact `test`/`publish`/`quit` prompt handling, the
  `X.Y.Z.99.N` arithmetic, and `branch_plan`'s refusals. `bless` is
  loaded by path (`importlib`) because it is a script, not a module.

## Phase 3 — CI

`.github/workflows/ci.yml`, modelled on `../pwr-tray/.github/workflows/tests.yml`,
three jobs:

* **lint** — `ruff check --output-format=github .` on the in-repo config only.
* **test** — matrix `3.11`, `3.12`, `3.13`; `apt-get install -y libgl1 libegl1
  libxkbcommon0`; `pip install -e '.[dev]'`; `compileall -q luks_tray tests`;
  `pytest -q --cov=luks_tray --cov-report=term-missing` with
  `QT_QPA_PLATFORM=offscreen`. The coverage floor is not repeated in the command:
  `pytest-cov` reads `fail_under` from `pyproject.toml`.
* **package** — `python -m build`, then
  `python tests/inspect_dist.py dist/*.whl dist/*.tar.gz`, then install the wheel
  into a scratch venv, import the package, load each resource through
  `importlib.resources`, and run `luks-tray --version` against the version the
  built package reports. Credential-free: CI is a gate, the upload stays local.

Triggers: `push` on `main` and `dev`, `pull_request`, `workflow_dispatch`;
`permissions: contents: read`; a `concurrency` group that cancels superseded
runs.

## Phase 4 — documentation

* `README-DEV.md`: the one version source, the three gates, the `bless`
  stages and the `test`/`publish`/`quit` prompt, the TestPyPI rehearsal and its
  install line (`pip cache purge`, `--extra-index-url`), the per-index
  credentials, the patient index verification, and the release steps
  (`dev` and `main` aligned, `vX.Y.Z` tagged, both pushed).
* [`README.md`](../README.md): replace any "run `deploy`" advice with
  `lt-venv/bin/python -m pip install -e '.[dev]'` and name `./bless` as the
  release path.
* [`AUDIT.md`](../tests/AUDIT.md): the `deploy` paragraph becomes a reference to
  this plan; the missing-tests item (medium #11) is closed by Phase 2/3 once it
  is real, not before.

## What is deliberately out of scope

* No CI publish job. The upload is irreversible and the owner runs it from the
  tree, which is why the safety lives in an inspectable local script.
* No `[tool.bless] inspect = ""` shortcut: the inspector is shared with CI so the
  two cannot disagree.
* No PyPI metadata changes beyond what `dynamic = ["version"]` requires. The
  classifiers, `license-files` and `Homepage`/`Bug Tracker` URLs stay as the
  PEP 639 pass left them.
* `images/` stays at the root and is not shipped in the sdist; that is the
  existing state of the published package, and changing it is a README/asset
  decision, not a release-gate one.
