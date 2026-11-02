#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Install, inspect, refresh and remove the luks-tray `systemd --user` unit.

    The unit files in `luks_tray/resources/systemd/` are the single source of
    truth.  They ship inside the wheel (flit includes everything under the
    module directory), and `--svc-install` copies the one matching this
    session's display server to `~/.config/systemd/user/luks-tray.service`.
    That verb is the only one that writes or enables the file; `--svc-start`
    and `--svc-restart` operate on a unit that is already installed.

    Why two templates and not pwr-tray's ten: luks-tray has no per-desktop
    behaviour to encode.  What it does need from the session is the *display*
    (`WAYLAND_DISPLAY` or `DISPLAY`, which Qt turns into a platform plugin) and
    the *desktop name* (`XDG_CURRENT_DESKTOP`/`DESKTOP_SESSION`, which the
    compositor helper uses to report the right window rule and keeps the log
    banner identifiable).  The dialogs themselves always show a body heading,
    so they no longer change layout based on the desktop name.  A user unit
    cannot inherit either from the compositor, so `render()`
    appends what the installing shell exported, after `ExecStart=`, where a
    later `Environment=` line wins.  `XDG_SESSION_TYPE` is *pinned* by each
    template instead: it selects the template, so it is not re-derived here.

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

    Two units, one per session, each guarded with
    `ConditionEnvironment=XDG_SESSION_TYPE=...`, would also work: systemd
    evaluates conditions against the user manager's environment and *not*
    against the unit's own `Environment=` -- measured on systemd 257, where a
    unit that sets `XDG_SESSION_TYPE=x11` itself is still skipped in a Wayland
    session ("skipped, unmet condition check") -- so the mismatched unit is
    skipped rather than failed.  It is not done here because it buys nothing
    the startup repair does not: the guard is only as reliable as the manager's
    environment having been imported, and each unit would still carry a
    snapshot of the login that installed it.

    The units deliberately do not redirect stdout: `main()` already points
    `prt()` at `~/.config/luks-tray/debug.log` whenever stdout is not a
    terminal, so the applet's own log stays the one place to look.  That also
    keeps `--svc-install` away from pwr-tray's `StandardOutput=append:` trap
    (systemd opens that file but never creates its directory, and fails every
    start with `EXIT_STDOUT`, status 209).

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

from luks_tray.Utils import SingleInstance, spawn_detached

UNIT_NAME = 'luks-tray.service'
COMMAND = 'systemctl'
EXECUTABLE = 'luks-tray'

#: The one path the shipped units run.  Every install method (pipx, pip
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

#: Session variables a unit pins to one value; install-time overrides them.
#: `XDG_SESSION_TYPE` is absent on purpose: it selects the template.
SESSION_KEYS = ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION')

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


def run_command(argv):
    """ Run one command and return `(exit status, combined output)`. """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=60)
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
    """ Return `~/.local/bin/luks-tray`: the path the shipped units run. """
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


def session_of(env=None):
    """ Return the session a unit is rendered for: 'wayland' or 'x11'. """
    value = (_env(env).get('XDG_SESSION_TYPE') or '').strip().lower()
    if value in ('wayland', 'x11'):
        return value
    raise ServiceError(
        'cannot tell whether this is a Wayland or an X11 session'
        f' (XDG_SESSION_TYPE is {value or "unset"}); set it to wayland or x11'
        ' and run the command again')


def templates(source=None):
    """ Map every shipped session name to its unit file. """
    folder = SOURCE_DIR if source is None else Path(source)
    found = {}
    prefix, suffix = f'{EXECUTABLE}-', '.service'
    for path in sorted(folder.glob(f'{prefix}*{suffix}')):
        found[path.name[len(prefix):-len(suffix)]] = path
    return found


def template_for(session, source=None):
    """ Return the shipped unit file for `session`, or explain what exists. """
    found = templates(source)
    try:
        return found[session]
    except KeyError:
        known = ', '.join(sorted(found)) or 'none'
        raise ServiceError(f'no shipped unit for session {session!r} (shipped: {known})') from None


def session_keys_for(text):
    """ Return the session variables a unit of this kind should carry.

        A Wayland unit must not pin a leftover `DISPLAY=:1` and an X11 unit must
        not pin a stale `WAYLAND_DISPLAY`: only the display the session type
        names is meaningful, alongside the desktop identity the dialogs read.
    """
    identity = ('XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION')
    if 'Environment=XDG_SESSION_TYPE=x11' in text:
        return ('DISPLAY', *identity)
    if 'Environment=XDG_SESSION_TYPE=wayland' in text:
        return ('WAYLAND_DISPLAY', *identity)
    return SESSION_KEYS


def render(text, env=None):
    """ Return `text` with this session's variables appended after `ExecStart=`.

        Only variables the calling shell actually exports are appended: a unit
        rendered from a bare `ssh` login must not invent a display.  Appending
        after `ExecStart=` is safe because systemd collects every `Environment=`
        line and the last assignment to a name wins.
    """
    env = _env(env)
    overrides = [f'Environment={key}={env[key]}'
                 for key in session_keys_for(text) if env.get(key)]
    result, placed = [], False
    for line in text.splitlines():
        result.append(line)
        if line.startswith('ExecStart=') and not placed:
            result.extend(overrides)
            placed = True
    if not placed:
        raise ServiceError('the unit has no ExecStart= line')
    return '\n'.join(result) + ('\n' if text.endswith('\n') else '')


def rendered_unit(session=None, env=None):
    """ Return the shipped unit for `session`, rendered for this environment. """
    env = _env(env)
    return render(read_unit(template_for(session or session_of(env))), env)


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

        The shipped units are constants: they name `~/.local/bin/luks-tray` and
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


def detached_restart(env=None, spawn=None):
    """ Ask systemd to restart this unit from outside its own cgroup.

        Called from inside the unit, so a plain `systemctl --user restart` would
        be killed mid-flight together with the process it is restarting; see
        `Utils.spawn_detached()`.  Returns whatever the spawn call returns.
    """
    env = _env(env)
    if not supervised_here():
        raise ServiceError(
            'this process is not running inside the applet\'s unit,'
            ' so there is no unit to restart')
    return spawn_detached(systemctl('restart', UNIT_NAME), spawn=spawn, env=env)


def _no_prompt(_question):
    return False


def _auto_yes(_question):
    return True


def dispatch(verb, session=None, env=None, force=False, yes=False, echo=print,
             ask=None, run=run_command, holder=None, systemd=None):
    """ Carry out one `--svc-*` verb and return a process exit status. """
    env = _env(env)
    ask = (ask or _no_prompt)
    if yes:
        ask = _auto_yes

    if verb == 'status':
        return _report(session, env, echo, run)

    # Dumping the unit needs neither a systemd user manager nor a launcher: it
    # is the starter for a hand-installed (or hand-edited) unit, so it has to
    # work exactly where an automatic install might not.
    if verb == 'show-file':
        for line in rendered_unit(session, env).splitlines():
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
        rendered = rendered_unit(session, env)
    else:
        # stop/uninstall must work on an installed unit even when the session
        # is unknown, so rendering is advisory here.
        try:
            rendered = rendered_unit(session, env)
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


def _report(session, env, echo, run):
    """ Print the state of the unit and return the documented status code. """
    path = installed_unit(env)
    if not path.is_file():
        try:
            available = template_for(session or session_of(env)).name
        except ServiceError:
            available = 'none'
        echo(f'{UNIT_NAME}: not installed (unit available: {available})')
        echo('  install it with: luks-tray --svc-install')
        return STATUS_ABSENT

    try:
        rendered = rendered_unit(session, env)
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
