#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Install, inspect, refresh and remove the luks-tray `systemd --user` unit.

    One unit ships in `luks_tray/resources/systemd/luks-tray.service`, and it is
    the single source of truth.  It ships inside the wheel (flit includes
    everything under the module directory), and `--svc-install` copies it to
    `~/.config/systemd/user/luks-tray.service`.  That verb is the only one that
    writes or enables the file; `--svc-start` and `--svc-restart` operate on a
    unit that is already installed.

    There is one unit rather than one per desktop or per display server:
    luks-tray has no per-desktop behaviour to encode, and its own startup code
    finds the session it was started into.  What it needs from that session is
    the *display* (`WAYLAND_DISPLAY` or `DISPLAY`, which Qt turns into a
    platform plugin) and the *desktop name* (`XDG_CURRENT_DESKTOP`/
    `DESKTOP_SESSION`, which the compositor helper uses to report the right
    window rule and keeps the log banner identifiable).  A user unit cannot
    inherit either from the compositor, so `render()` appends what the
    installing shell exported, after `ExecStart=`, where a later `Environment=`
    line wins.  *Which* display variable is appended is decided by
    `Session.display_server()` from the socket that is listening, so a Wayland
    install pins `WAYLAND_DISPLAY`, an X11 install pins `DISPLAY`, and neither
    pins the other one -- a Wayland login's Xwayland display included.

    That appended copy is a snapshot of one login, and a unit's own
    `Environment=` wins over what the *next* session exports, so it goes stale
    the moment the user logs in as the other display server -- which is a thing
    people do often.  The snapshot is therefore a fallback only:
    [`luks_tray.Session`](luks_tray/Session.py:1) re-derives `XDG_SESSION_TYPE`,
    `WAYLAND_DISPLAY` and `DISPLAY` at startup from the socket that is
    listening (and from `loginctl` when the environment names none), so an
    installed unit keeps working across a switch and there is nothing to
    remember.  Re-running `--svc-install` after a switch is optional: it only
    refreshes the snapshot.

    The unit deliberately does not redirect stdout: `main()` already points
    `prt()` at `~/.config/luks-tray/debug.log` whenever stdout is not a
    terminal, so the applet's own log stays the one place to look.  A
    `StandardOutput=append:` line would also hand systemd a path it opens but
    never creates, failing every start with `EXIT_STDOUT` (status 209).

    Nothing here shells out to systemctl on its own: the verbs are built as
    `Step` lists by the `plan_*()` functions and performed by `execute()`, with
    the subprocess call injected.  That keeps the whole policy testable on a
    machine without a systemd session.
"""
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from luks_tray import Session
from luks_tray.Utils import SingleInstance

UNIT_NAME = 'luks-tray.service'
COMMAND = 'systemctl'
EXECUTABLE = 'luks-tray'

#: The one path the shipped unit runs.  Every install method (pipx, pip
#: --user, a distribution package) writes it, so the unit never has to know
#: which one built it -- and `--svc-install` from a virtualenv produces the
#: same file as `--svc-install` from any other shell.
LAUNCHER = Path('.local') / 'bin' / EXECUTABLE

#: Directory holding the units shipped in the wheel.  Tests repoint this.
SOURCE_DIR = Path(__file__).resolve().parent / 'resources' / 'systemd'

# `--svc-status` exit codes: the one verb meant to be used from `if`.
STATUS_ACTIVE = 0
STATUS_INACTIVE = 1
STATUS_ABSENT = 2

OK = 0
FAILED = 1

#: How long the applet waits for `systemctl --no-block` to accept a restart.
#: Called from a Qt action slot, so a wedged user manager must not freeze the
#: menu for longer than this.
SYSTEMCTL_TIMEOUT = 10

#: Variables that name the desktop rather than the display server.  They are
#: pinned from the installing shell on every install; which *display* variable
#: is pinned as well is decided per install by `display_keys()`.
IDENTITY_KEYS = ('XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION')

_EXEC_START_RE = re.compile(r'^ExecStart=(.*)$', re.MULTILINE)
_CGROUP_UNIT_RE = re.compile(r'([A-Za-z0-9@_.\-]+\.service)')
#: A user session's own manager, which is in every process's cgroup and is not
#: what supervises the applet -- a terminal under it must not look supervised.
_USER_MANAGER_RE = re.compile(r'^user@[0-9]*\.service$')


class ServiceError(Exception):
    """ A `--svc-*` request that cannot be carried out as stated. """


@dataclass(frozen=True)
class Step:
    """ One effect, in the order the verbs have to perform them.

        Exactly one of `argv` (run a command), `write` (create or replace a
        file), `mkdir` (create a directory) or `remove` (delete a file) is set;
        `describe` is what the user is shown before anything happens, and
        `hint` is appended to the error if the step turns out to fail.
    """
    describe: str
    argv: tuple = ()
    write: tuple = None
    remove: Path = None
    mkdir: Path = None
    hint: str = ''


@dataclass
class Plan:
    """ What a verb intends to do, plus anything the user should know first. """
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    details: list = field(default_factory=list)
    prompt: str = ''


@dataclass
class Status:
    """ Current on-disk and systemd state of the installed unit. """
    path: Path
    installed: bool
    enabled: bool
    active: bool
    up_to_date: bool
    failed: bool


def _env(env=None):
    return dict(os.environ if env is None else env)


def run_command(argv, timeout=60):
    """ Run one command and return `(exit status, combined output)`. """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or '') + (completed.stderr or '')


def systemctl(*args):
    """ Build a `systemctl --user` argv tuple. """
    return (COMMAND, '--user', *args)


def systemd_available(env=None):
    """ True if a user manager can be talked to on this machine. """
    return Path('/run/systemd/system').is_dir() and bool(_env(env).get('XDG_RUNTIME_DIR'))


def unit_dir(env=None):
    """ Return `$XDG_CONFIG_HOME/systemd/user` (defaulting to ~/.config). """
    base = _env(env).get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'systemd' / 'user'


def installed_unit(env=None):
    """ Return the path the unit is installed to. """
    return unit_dir(env) / UNIT_NAME


def read_unit(path):
    """ Read a unit file as text. """
    return Path(path).read_text(encoding='utf-8')


def log_path(env=None):
    """ Return the applet's own log, spelled the way IniTool spells it.

        Display-only: this file is written by `Utils.configure_log()`, and the
        function exists so a refused or failed unit can point at it.
    """
    home = _env(env).get('HOME') or str(Path.home())
    return Path(home) / '.config' / 'luks-tray' / 'debug.log'


def _expand(path, env):
    """ Expand the `%h`/`%U` specifiers systemd would expand in a path. """
    home = _env(env).get('HOME') or str(Path.home())
    return path.replace('%h', home).replace('%U', str(os.getuid()))


def installed_executable(text, env=None):
    """ Return the executable named by the first `ExecStart=`, expanded.

        systemd expands `%h` when it starts the unit, so the comparison that
        matters -- does this unit run `~/.local/bin/luks-tray`? -- has to
        expand it the same way.
    """
    match = _EXEC_START_RE.search(text)
    if not match:
        return None
    command = match.group(1).strip()
    if not command:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        return _expand(command[1:end], _env(env)) if end > 0 else None
    return _expand(command.split()[0], _env(env))


def launcher_path(env=None):
    """ Return `~/.local/bin/luks-tray`: the path the shipped unit runs. """
    home = _env(env).get('HOME') or str(Path.home())
    return Path(home) / LAUNCHER


def launcher_problem(path):
    """ Return why `path` cannot launch the applet, or None when it can. """
    path = Path(path)
    if path.is_symlink() and not path.exists():
        return f'{path} is a dangling symlink'
    if not path.exists():
        return f'{path} does not exist'
    if not path.is_file():
        return f'{path} is not a regular file'
    if not os.access(path, os.X_OK):
        return f'{path} is not executable'
    return None


def _launcher_hint():
    return ('install it with: pipx install luks-tray (or pipx install --editable .'
            ' for a checkout, or python -m pip install --user luks-tray)')


def require_launcher(env=None):
    """ Return the launcher path, or refuse because no unit could start. """
    path = launcher_path(env)
    problem = launcher_problem(path)
    if problem:
        raise ServiceError(f'{problem}, so the unit could not start; {_launcher_hint()}')
    return path


def template(source=None):
    """ Return the shipped unit file, or refuse when the package lacks it. """
    folder = SOURCE_DIR if source is None else Path(source)
    path = folder / UNIT_NAME
    if not path.is_file():
        raise ServiceError(f'the shipped unit {path} is missing from this package')
    return path


def display_keys(env=None):
    """ Return the display variables a unit rendered here should pin.

        Decided from the display server that is actually listening, not from
        `XDG_SESSION_TYPE`: one unit serves both kinds of session, and a Wayland
        login has an Xwayland `DISPLAY` as well, so pinning by marker alone
        would write the wrong one into the file.  Nothing listening (a bare
        `ssh` login) pins nothing -- the applet asks the machine at startup
        either way, so an empty answer is not a failure.
    """
    kind = Session.display_server(_env(env))
    if kind == Session.WAYLAND:
        return ('WAYLAND_DISPLAY',)
    if kind == Session.X11:
        return ('DISPLAY',)
    return ()


def render(text, env=None):
    """ Return `text` with this login's variables appended after `ExecStart=`.

        Only variables the calling shell actually exports are appended: a unit
        rendered from a bare `ssh` login must not invent a display.  Appending
        after `ExecStart=` is safe because systemd collects every `Environment=`
        line and the last assignment to a name wins.
    """
    env = _env(env)
    overrides = [f'Environment={key}={env[key]}'
                 for key in (*display_keys(env), *IDENTITY_KEYS) if env.get(key)]
    result, placed = [], False
    for line in text.splitlines():
        result.append(line)
        if line.startswith('ExecStart=') and not placed:
            result.extend(overrides)
            placed = True
    if not placed:
        raise ServiceError('the unit has no ExecStart= line')
    return '\n'.join(result) + ('\n' if text.endswith('\n') else '')


def rendered_unit(env=None):
    """ Return the shipped unit, rendered for this environment. """
    return render(read_unit(template()), _env(env))


def probe(rendered=None, env=None, run=run_command):
    """ Read the installed unit and ask systemd about it. """
    path = installed_unit(env)
    installed = path.is_file()
    enabled = active = failed = False
    if installed:
        enabled = run(systemctl('is-enabled', '--quiet', UNIT_NAME))[0] == 0
        active = run(systemctl('is-active', '--quiet', UNIT_NAME))[0] == 0
        failed = run(systemctl('is-failed', '--quiet', UNIT_NAME))[0] == 0
    up_to_date = False
    if installed and rendered is not None:
        try:
            up_to_date = path.read_text(encoding='utf-8') == rendered
        except OSError:
            up_to_date = False
    return Status(path=path, installed=installed, enabled=enabled, active=active,
                  up_to_date=up_to_date, failed=failed)


def changed_lines(current, wanted):
    """ Return `-`/`+` lines describing how `current` differs from `wanted`. """
    old, new = current.splitlines(), wanted.splitlines()
    spare = sum(1 for line in old if line not in new) + sum(1 for line in new if line not in old)
    if spare > 12:
        return [f'  ({spare} lines differ)']
    lines = [f'- {line}' for line in old if line not in new]
    lines += [f'+ {line}' for line in new if line not in old]
    return lines


def plan_install(rendered, status, force=False, holder=None, env=None):
    """ Write the unit if needed, reload, and enable it (without starting).

        Installing never starts the applet: the user may already be running one
        from a desktop autostart, and starting it here would either exit 3
        against the lock or fight that instance.  The unit takes over at the
        next login, which is what "starting at login" means.
    """
    plan = Plan()
    target = status.path
    wrote = False
    if status.installed and status.up_to_date:
        plan.notes.append(f'{target} is already up to date')
    elif status.installed:
        try:
            plan.details = changed_lines(read_unit(target), rendered)
        except OSError:
            plan.details = []
        plan.steps.append(Step(f'write {target}', write=(target, rendered)))
        wrote = True
        plan.prompt = f'{target} exists and differs from the shipped unit; replace it?'
    else:
        plan.steps.append(Step(f'write {target}', write=(target, rendered)))
        wrote = True
    if wrote:
        plan.notes.append(f'the unit will run {launcher_path(env)}')
        plan.steps.append(Step('reload the unit files', argv=systemctl('daemon-reload')))
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if not status.enabled:
        plan.steps.append(Step(f'enable {UNIT_NAME}', argv=systemctl('enable', UNIT_NAME)))
    if holder and not status.active:
        plan.notes.append(
            f'luks-tray is already running outside systemd (pid {holder}); it keeps'
            ' running, and the unit takes over at your next login')
    return plan


def _systemd_hint():
    return (f'inspect it with: systemctl --user status {UNIT_NAME};'
            f' the applet\'s own log is {log_path()}')


def _running_outside_systemd(holder):
    return ServiceError(
        f'luks-tray is already running outside systemd (pid {holder}); the unit'
        ' would exit 3 against the single-instance lock.  Remove the desktop'
        ' autostart that started it (or quit that instance) and run this again')


def _require_installed(status):
    """ Refuse a start/restart that has no unit to operate on. """
    if not status.installed:
        raise ServiceError(
            f'{UNIT_NAME} is not installed; run luks-tray --svc-install first')


def _refuse_moved_unit(status, env):
    """ Refuse to start a unit that does not run the launcher.

        The shipped unit is a constant: it names `~/.local/bin/luks-tray` and
        never an installation.  A unit naming anything else was either hand
        edited or installed by a script that baked in an absolute path -- usually
        a project virtualenv's, which is how a service keeps running a copy
        nobody edits.  Rewriting the unit is `--svc-install`'s decision, so it is
        refused here rather than fixed here.
    """
    try:
        installed = installed_executable(read_unit(status.path), env)
    except OSError:
        return
    wanted = str(launcher_path(env))
    if installed and installed != wanted:
        raise ServiceError(
            f'{UNIT_NAME} runs {installed}, but the shipped unit runs {wanted};'
            ' run luks-tray --svc-install to update the unit, then try again')


def _note_stale_unit(plan, status, rendered):
    """ Point at --svc-install when the file differs, without changing it. """
    if rendered is not None and status.installed and not status.up_to_date:
        plan.notes.append(
            f'{status.path} differs from the shipped unit;'
            ' run luks-tray --svc-install to update it')


def plan_start(rendered, status, force=False, holder=None, env=None):
    """ Enable if needed and start an installed unit; never writes the unit.

        Installing is a separate, explicit act (`--svc-install`).  `start` only
        operates on the unit that is already installed, so it cannot silently
        commit the machine to systemd.  It does enable a disabled unit:
        "start" means "run now and come back at your next login".
    """
    _require_installed(status)
    _refuse_moved_unit(status, env)
    plan = Plan()
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if not status.enabled:
        plan.steps.append(Step(f'enable {UNIT_NAME}', argv=systemctl('enable', UNIT_NAME)))
        plan.notes.append(
            f'{UNIT_NAME} was disabled; it is enabled so it returns at your next login')
    if status.active:
        if status.up_to_date or rendered is None:
            plan.notes.append(f'{UNIT_NAME} is already running')
        else:
            # `start` on an active unit is a no-op; restarting is a separate
            # verb so that `start` never changes what is running.
            plan.notes.append(
                f'{UNIT_NAME} is already running; the installed unit differs from the'
                ' shipped one, so run luks-tray --svc-install to update it or'
                ' luks-tray --svc-restart to apply code changes')
        return plan
    if holder:
        raise _running_outside_systemd(holder)
    _note_stale_unit(plan, status, rendered)
    plan.steps.append(Step(f'start {UNIT_NAME}', argv=systemctl('start', UNIT_NAME),
                           hint=_systemd_hint()))
    return plan


def plan_restart(rendered, status, force=False, holder=None, env=None):
    """ Restart an installed unit so it runs the code its launcher names.

        An editable install (or a `git pull`) changes the *code* without
        changing the unit file, so neither `--svc-install` nor `--svc-start`
        can notice it: only a restart makes systemd load the modules the
        launcher resolves to.  A restart never installs, enables or rewrites
        the unit.
    """
    _require_installed(status)
    _refuse_moved_unit(status, env)
    plan = Plan()
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if holder and not status.active:
        raise _running_outside_systemd(holder)
    _note_stale_unit(plan, status, rendered)
    if not status.enabled:
        plan.notes.append(
            f'{UNIT_NAME} is not enabled; it runs now but will not return at your'
            ' next login (run luks-tray --svc-install to enable it)')
    plan.steps.append(Step(f'restart {UNIT_NAME}', argv=systemctl('restart', UNIT_NAME),
                           hint=_systemd_hint()))
    plan.notes.append(
        f'{UNIT_NAME} restarts now, so it runs the code {launcher_path(env)} resolves to')
    return plan


def plan_stop(status):
    """ Stop the unit; it stays enabled, so it returns at the next login. """
    plan = Plan()
    if not status.installed:
        raise ServiceError(f'{UNIT_NAME} is not installed, so there is nothing to stop')
    if not status.active:
        plan.notes.append(f'{UNIT_NAME} is not running')
        return plan
    plan.steps.append(Step(f'stop {UNIT_NAME}', argv=systemctl('stop', UNIT_NAME)))
    plan.notes.append(f'{UNIT_NAME} stays enabled; it starts again at your next login')
    return plan


def plan_uninstall(status, force=False):
    """ Stop, disable and delete the unit, leaving config and logs alone. """
    plan = Plan()
    if not status.installed:
        plan.notes.append(f'{UNIT_NAME} is not installed; nothing to do')
        return plan
    if not (status.up_to_date or force):
        raise ServiceError(
            f'{status.path} was edited after it was installed, so it is not'
            " luks-tray's to delete; remove it yourself, or pass --force")
    if status.active:
        plan.steps.append(Step(f'stop {UNIT_NAME}', argv=systemctl('stop', UNIT_NAME)))
    if status.enabled:
        plan.steps.append(Step(f'disable {UNIT_NAME}', argv=systemctl('disable', UNIT_NAME)))
    plan.steps.append(Step(f'remove {status.path}', remove=status.path))
    plan.steps.append(Step('reload the unit files', argv=systemctl('daemon-reload')))
    plan.notes.append('config.ini, the mount history and the logs are left in place')
    return plan


def execute(plan, run=run_command):
    """ Perform every step of `plan` in order; the plan was already shown. """
    for step in plan.steps:
        if step.write:
            path, content = step.write[0], step.write[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        elif step.mkdir:
            step.mkdir.mkdir(parents=True, exist_ok=True)
        elif step.remove:
            step.remove.unlink(missing_ok=True)
        elif step.argv:
            code, output = run(step.argv)
            if code != 0:
                message = f'{step.describe} failed (exit {code}): {output.strip()}'
                raise ServiceError(f'{message}\n  {step.hint}' if step.hint else message)


def applet_holder(lock_dir):
    """ Return the pid of a live luks-tray holding the single-instance lock.

        The lock is authoritative here, not a `ps` scan: a command line that
        merely *mentions* luks-tray -- the shell running
        `luks-tray --svc-start`, an editor, `tail` on the log -- must not look
        like a running applet, and a stale pid file must not either.  Holding
        the lock is the only thing that makes the next start exit 3, which is
        the case worth warning about.
    """
    path = Path(lock_dir) / f'{EXECUTABLE}.lock'
    if not path.exists():
        return None
    lock = SingleInstance(str(path))
    if lock.acquire():
        lock.release()
        return None
    return SingleInstance.read_pid(lock.path)


def unit_in_cgroup(text):
    """ Return the deepest `.service` a `/proc/<pid>/cgroup` file names, or None.

        A supervised applet sits in
        `.../user@1000.service/app.slice/luks-tray.service` on cgroup v2 (and in
        `.../user@1000.service/luks-tray.service` on v1), so the last match is
        the unit that owns it.  `user@1000.service` is skipped: it is the user's
        own manager, present in every process's cgroup, and a terminal under it
        -- whose deepest entry is a `.scope` -- must not look supervised.
    """
    found = [name for name in _CGROUP_UNIT_RE.findall(text or '')
             if not _USER_MANAGER_RE.match(name)]
    return found[-1] if found else None


def supervised_by(pid):
    """ Return the systemd unit that supervises `pid`, or None when none does. """
    try:
        text = Path(f'/proc/{int(pid)}/cgroup').read_text(encoding='utf-8', errors='replace')
    except (OSError, TypeError, ValueError):
        return None
    return unit_in_cgroup(text)


def supervised_here():
    """ True when *this* process is running inside the applet's own unit.

        `INVOCATION_ID` alone is not enough: systemd sets it for every process it
        launches, and children inherit it -- a terminal opened from a GUI
        session that systemd started carries it too.  The cgroup says which unit
        this pid actually belongs to, which is the question a restart has to
        answer.
    """
    return supervised_by(os.getpid()) == UNIT_NAME


def request_restart(env=None, run=None):
    """ Ask systemd to restart this unit, and wait until the job is queued.

        Called from inside the unit, so the request has to reach the manager
        *before* the applet exits: a restart begins by stopping the unit, and
        that stop kills the unit's whole cgroup (`KillMode=control-group`).

        `--no-block` is what makes a synchronous call safe.  The job is queued
        and `systemctl` returns without waiting for the stop it just asked for,
        so the caller can leave cleanly and the manager carries the job out --
        stop, then the unit's own `ExecStart=` starts the fresh instance.

        Detaching the request instead (`systemd-run --user --scope`, which is
        what `Utils.spawn_detached()` does for *viewers*) does not work here,
        and field-testing showed exactly why: the transient scope is created
        out of our own cgroup, so the stop job kills the helper before it has
        asked systemd anything.  The tray went down, the unit stayed inactive,
        and the menu row behaved like "Quit".

        Returns the exit status of the `systemctl` call (0 = request accepted).
        Raises ServiceError when this process is not the unit's main process --
        there is no unit to restart then.
    """
    env = _env(env)
    if not supervised_here():
        raise ServiceError(
            'this process is not running inside the applet\'s unit,'
            ' so there is no unit to restart')
    argv = systemctl('--no-block', 'restart', UNIT_NAME)
    if run is None:
        return run_command(argv, timeout=SYSTEMCTL_TIMEOUT)[0]
    return run(argv)[0]


def _no_prompt(_question):
    return False


def _auto_yes(_question):
    return True


def dispatch(verb, env=None, force=False, yes=False, echo=print,
             ask=None, run=run_command, holder=None, systemd=None):
    """ Carry out one `--svc-*` verb and return a process exit status. """
    env = _env(env)
    ask = (ask or _no_prompt)
    if yes:
        ask = _auto_yes

    if verb == 'status':
        return _report(env, echo, run)

    # Dumping the unit needs neither a systemd user manager nor a launcher: it
    # is the starter for a hand-installed (or hand-edited) unit, so it has to
    # work exactly where an automatic install might not.
    if verb == 'show-file':
        for line in rendered_unit(env).splitlines():
            echo(line)
        return OK

    if not (systemd_available(env) if systemd is None else systemd):
        raise ServiceError(
            'no systemd user manager is reachable (is this a systemd system?);'
            ' use your desktop autostart instead')

    if verb in ('install', 'start', 'restart'):
        # The unit runs ~/.local/bin/luks-tray and nothing else, so a missing
        # launcher is a unit that fails every restart.  Refuse before writing
        # or starting anything; `stop`, `uninstall` and `status` must still
        # work exactly when things are broken.
        require_launcher(env)
        rendered = rendered_unit(env)
    else:
        # stop/uninstall must work on an installed unit even when the shipped
        # unit cannot be rendered, so rendering is advisory here.
        try:
            rendered = rendered_unit(env)
        except ServiceError:
            rendered = None

    status = probe(rendered=rendered, env=env, run=run)
    if verb == 'install':
        plan = plan_install(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'start':
        plan = plan_start(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'restart':
        plan = plan_restart(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'stop':
        plan = plan_stop(status)
    elif verb == 'uninstall':
        plan = plan_uninstall(status, force=force)
    else:
        raise ServiceError(f'unknown service verb {verb!r}')

    _show(plan, echo)
    accepted = True
    if plan.prompt and plan.steps and not force:
        accepted = ask(plan.prompt)
    if not accepted:
        echo('  left unchanged')
        return FAILED
    if not plan.steps:
        return OK
    execute(plan, run=run)
    return OK


def _show(plan, echo):
    for note in plan.notes:
        echo(note)
    for line in plan.details:
        echo(line)
    if plan.steps:
        echo('about to:')
        for step in plan.steps:
            echo(f'  {step.describe}')


def _report(env, echo, run):
    """ Print the state of the unit and return the documented status code. """
    path = installed_unit(env)
    if not path.is_file():
        echo(f'{UNIT_NAME}: not installed')
        echo('  install it with: luks-tray --svc-install')
        return STATUS_ABSENT

    try:
        rendered = rendered_unit(env)
    except ServiceError:
        rendered = None
    status = probe(rendered=rendered, env=env, run=run)
    if status.active:
        state = 'active'
    elif status.failed:
        state = 'failed'
    else:
        state = 'installed, not running'
    flags = 'enabled' if status.enabled else 'disabled'
    echo(f'{UNIT_NAME}: {state} ({flags})')
    echo(f'  unit: {path}')
    launcher = launcher_path(env)
    try:
        installed_exe = installed_executable(read_unit(path), env)
    except OSError:
        installed_exe = None
    if installed_exe:
        echo(f'  runs: {installed_exe}')
        if Path(installed_exe) != launcher:
            echo(f'  note: the shipped unit runs {launcher};'
                 ' luks-tray --svc-install would replace this unit')
    problem = launcher_problem(launcher)
    if problem:
        echo(f'  note: {problem}, so the unit cannot start ({_launcher_hint()})')
    else:
        target = Path(launcher).resolve()
        if target != launcher:
            echo(f'        -> {target}')
    echo(f'  log: {log_path(env)}')
    if status.failed:
        echo(f'  note: the last start failed; see: {_systemd_hint()}')
        echo('  note: luks-tray --svc-start clears the failed state and tries again')
    if rendered is not None and not status.up_to_date:
        echo('  note: the file differs from the shipped unit'
             ' (luks-tray --svc-install would replace it)')
    return STATUS_ACTIVE if status.active else STATUS_INACTIVE
