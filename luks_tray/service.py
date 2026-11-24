#!/usr/bin/env python3
"""Install, inspect, refresh and remove the luks-tray `systemd --user` unit."""
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

LAUNCHER = Path('.local') / 'bin' / EXECUTABLE

SOURCE_DIR = Path(__file__).resolve().parent / 'resources' / 'systemd'

STATUS_ACTIVE = 0
STATUS_INACTIVE = 1
STATUS_ABSENT = 2

OK = 0
FAILED = 1

SYSTEMCTL_TIMEOUT = 10

_EXEC_START_RE = re.compile(r'^ExecStart=(.*)$', re.MULTILINE)
_CGROUP_UNIT_RE = re.compile(r'([A-Za-z0-9@_.\-]+\.service)')
_USER_MANAGER_RE = re.compile(r'^user@[0-9]*\.service$')


class ServiceError(Exception):
    """A `--svc-*` request that cannot be carried out as stated."""


@dataclass(frozen=True)
class Step:
    """One effect, in the order the verbs perform them.

    Attributes:
        describe: Text shown to the user before the step runs.
        argv: Command to run, when this step runs a command.
        write: `(path, content)` to write, when this step writes a file.
        remove: File to delete, when this step removes one.
        mkdir: Directory to create, when this step creates one.
        hint: Text appended to the error when the step fails.
    """
    describe: str
    argv: tuple = ()
    write: tuple = None
    remove: Path = None
    mkdir: Path = None
    hint: str = ''


@dataclass
class Plan:
    """What a verb intends to do, plus anything the user should know first.

    Attributes:
        steps: Steps to perform in order.
        notes: Messages to print before the steps.
        details: Diff lines describing the intended change.
        prompt: Question asked before a destructive step.
    """
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    details: list = field(default_factory=list)
    prompt: str = ''


@dataclass
class Status:
    """Current on-disk and systemd state of the installed unit.

    Attributes:
        path: Installed unit path.
        installed: Whether the unit file exists.
        enabled: Whether the unit is enabled.
        active: Whether the unit is running.
        up_to_date: Whether the file matches the shipped unit.
        failed: Whether the last start failed.
    """
    path: Path
    installed: bool
    enabled: bool
    active: bool
    up_to_date: bool
    failed: bool


def _env(env=None):
    """Return a plain environment mapping."""
    return dict(os.environ if env is None else env)


def run_command(argv, timeout=60):
    """Run one command and return `(exit status, combined output)`.

    Args:
        argv: Command and arguments.
        timeout: Deadline in seconds.

    Returns:
        tuple: Exit status and combined output.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or '') + (completed.stderr or '')


def systemctl(*args):
    """Build a `systemctl --user` argv tuple.

    Args:
        *args: Arguments for the command.

    Returns:
        tuple: The argv tuple.
    """
    return (COMMAND, '--user', *args)


def systemd_available(env=None):
    """Return True when a user manager can be reached.

    Args:
        env: Environment mapping to read.

    Returns:
        bool: Whether systemd and a runtime directory are present.
    """
    return Path('/run/systemd/system').is_dir() and bool(_env(env).get('XDG_RUNTIME_DIR'))


def unit_dir(env=None):
    """Return `$XDG_CONFIG_HOME/systemd/user`, defaulting to `~/.config`.

    Args:
        env: Environment mapping to read.

    Returns:
        Path: The unit directory.
    """
    base = _env(env).get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'systemd' / 'user'


def installed_unit(env=None):
    """Return the path the unit is installed to.

    Args:
        env: Environment mapping to read.

    Returns:
        Path: The installed unit path.
    """
    return unit_dir(env) / UNIT_NAME


def read_unit(path):
    """Read a unit file as text.

    Args:
        path: Unit file path.

    Returns:
        str: The file contents.
    """
    return Path(path).read_text(encoding='utf-8')


def log_path(env=None):
    """Return the applet's log path.

    Args:
        env: Environment mapping to read.

    Returns:
        Path: The debug log path.
    """
    home = _env(env).get('HOME') or str(Path.home())
    return Path(home) / '.config' / 'luks-tray' / 'debug.log'


def _expand(path, env):
    """Expand the `%h` and `%U` specifiers systemd would expand."""
    home = _env(env).get('HOME') or str(Path.home())
    return path.replace('%h', home).replace('%U', str(os.getuid()))


def installed_executable(text, env=None):
    """Return the executable named by the first `ExecStart=`, expanded.

    Args:
        text: Unit file text.
        env: Environment mapping to read.

    Returns:
        str | None: The executable path, or None when no `ExecStart=` exists.
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
    """Return `~/.local/bin/luks-tray`.

    Args:
        env: Environment mapping to read.

    Returns:
        Path: The launcher path.
    """
    home = _env(env).get('HOME') or str(Path.home())
    return Path(home) / LAUNCHER


def launcher_problem(path):
    """Return why `path` cannot launch the applet, or None when it can.

    Args:
        path: Launcher path.

    Returns:
        str | None: The problem text.
    """
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
    """Return the install hint for a missing launcher."""
    return ('install it with: pipx install luks-tray (or pipx install --editable .'
            ' for a checkout, or python -m pip install --user luks-tray)')


def require_launcher(env=None):
    """Return the launcher path, or refuse because no unit could start.

    Args:
        env: Environment mapping to read.

    Returns:
        Path: The launcher path.

    Raises:
        ServiceError: When the launcher cannot start the applet.
    """
    path = launcher_path(env)
    problem = launcher_problem(path)
    if problem:
        raise ServiceError(f'{problem}, so the unit could not start; {_launcher_hint()}')
    return path


def template(source=None):
    """Return the shipped unit file, or refuse when the package lacks it.

    Args:
        source: Directory holding the unit; defaults to the packaged copy.

    Returns:
        Path: The unit file path.

    Raises:
        ServiceError: When the unit file is missing.
    """
    folder = SOURCE_DIR if source is None else Path(source)
    path = folder / UNIT_NAME
    if not path.is_file():
        raise ServiceError(f'the shipped unit {path} is missing from this package')
    return path


def display_keys(env=None):
    """Return the display variables a unit rendered here should pin.

    Args:
        env: Environment mapping to read.

    Returns:
        tuple: Variable names to pin.
    """
    kind = Session.display_server(_env(env))
    if kind == Session.WAYLAND:
        return ('WAYLAND_DISPLAY',)
    if kind == Session.X11:
        return ('DISPLAY',)
    return ()


def render(text, env=None):
    """Return `text` with this login's display appended after `ExecStart=`.

    Args:
        text: Unit file text.
        env: Environment mapping to read.

    Returns:
        str: The rendered unit.

    Raises:
        ServiceError: When the unit has no `ExecStart=` line.
    """
    env = _env(env)
    overrides = [f'Environment={key}={env[key]}'
                 for key in display_keys(env) if env.get(key)]
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
    """Return the shipped unit, rendered for this environment.

    Args:
        env: Environment mapping to read.

    Returns:
        str: The rendered unit.
    """
    return render(read_unit(template()), _env(env))


def probe(rendered=None, env=None, run=run_command):
    """Read the installed unit and ask systemd about it.

    Args:
        rendered: Rendered unit text to compare against, when available.
        env: Environment mapping to read.
        run: Command runner; defaults to `run_command`.

    Returns:
        Status: The current unit state.
    """
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
    """Return `-`/`+` lines describing how `current` differs from `wanted`.

    Args:
        current: Current text.
        wanted: Desired text.

    Returns:
        list: Diff lines, or one line when the difference is large.
    """
    old, new = current.splitlines(), wanted.splitlines()
    spare = sum(1 for line in old if line not in new) + sum(1 for line in new if line not in old)
    if spare > 12:
        return [f'  ({spare} lines differ)']
    lines = [f'- {line}' for line in old if line not in new]
    lines += [f'+ {line}' for line in new if line not in old]
    return lines


def plan_install(rendered, status, force=False, holder=None, env=None):
    """Write the unit if needed, reload, and enable it without starting.

    Args:
        rendered: Rendered unit text.
        status: Current unit state.
        force: Accepted for API symmetry; unused.
        holder: Pid of a running instance outside systemd.
        env: Environment mapping to read.

    Returns:
        Plan: The install plan.
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
    """Return the systemd inspection hint."""
    return (f'inspect it with: systemctl --user status {UNIT_NAME};'
            f' the applet\'s own log is {log_path()}')


def _running_outside_systemd(holder):
    """Return the error for a running instance outside systemd."""
    return ServiceError(
        f'luks-tray is already running outside systemd (pid {holder}); the unit'
        ' would exit 3 against the single-instance lock.  Remove the desktop'
        ' autostart that started it (or quit that instance) and run this again')


def _require_installed(status):
    """Refuse a start or restart that has no unit to operate on."""
    if not status.installed:
        raise ServiceError(
            f'{UNIT_NAME} is not installed; run luks-tray --svc-install first')


def _refuse_moved_unit(status, env):
    """Refuse to start a unit that does not run the launcher."""
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
    """Point at `--svc-install` when the file differs, without changing it."""
    if rendered is not None and status.installed and not status.up_to_date:
        plan.notes.append(
            f'{status.path} differs from the shipped unit;'
            ' run luks-tray --svc-install to update it')


def plan_start(rendered, status, force=False, holder=None, env=None):
    """Enable if needed and start an installed unit; never writes the unit.

    Args:
        rendered: Rendered unit text.
        status: Current unit state.
        force: Accepted for API symmetry; unused.
        holder: Pid of a running instance outside systemd.
        env: Environment mapping to read.

    Returns:
        Plan: The start plan.
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
    """Restart an installed unit so it runs its current code.

    Args:
        rendered: Rendered unit text.
        status: Current unit state.
        force: Accepted for API symmetry; unused.
        holder: Pid of a running instance outside systemd.
        env: Environment mapping to read.

    Returns:
        Plan: The restart plan.
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
    """Stop the unit; it stays enabled, so it returns at the next login.

    Args:
        status: Current unit state.

    Returns:
        Plan: The stop plan.
    """
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
    """Stop, disable and delete the unit, leaving config and logs alone.

    Args:
        status: Current unit state.
        force: Delete a unit that was edited after installation.

    Returns:
        Plan: The uninstall plan.
    """
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
    """Perform every step of `plan` in order.

    Args:
        plan: Plan to carry out.
        run: Command runner; defaults to `run_command`.

    Raises:
        ServiceError: When a command step fails.
    """
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
    """Return the pid of a live luks-tray holding the single-instance lock.

    Args:
        lock_dir: Directory holding the lock file.

    Returns:
        int | None: The holder pid, or None when the lock is free.
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
    """Return the deepest `.service` named in a `/proc/<pid>/cgroup` file.

    Args:
        text: Cgroup file contents.

    Returns:
        str | None: The owning unit, or None.
    """
    found = [name for name in _CGROUP_UNIT_RE.findall(text or '')
             if not _USER_MANAGER_RE.match(name)]
    return found[-1] if found else None


def supervised_by(pid):
    """Return the systemd unit that supervises `pid`, or None.

    Args:
        pid: Process id.

    Returns:
        str | None: The supervising unit.
    """
    try:
        text = Path(f'/proc/{int(pid)}/cgroup').read_text(encoding='utf-8', errors='replace')
    except (OSError, TypeError, ValueError):
        return None
    return unit_in_cgroup(text)


def supervised_here():
    """Return True when this process runs inside the applet's own unit.

    Returns:
        bool: Whether the current process is supervised by the unit.
    """
    return supervised_by(os.getpid()) == UNIT_NAME


def request_restart(env=None, run=None):
    """Ask systemd to restart this unit and wait until the job is queued.

    Args:
        env: Environment mapping to read.
        run: Command runner; defaults to `run_command`.

    Returns:
        int: Exit status of the `systemctl` call.

    Raises:
        ServiceError: When this process is not the unit's main process.
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
    """Answer no to every question."""
    return False


def _auto_yes(_question):
    """Answer yes to every question."""
    return True


def dispatch(verb, env=None, force=False, yes=False, echo=print,
             ask=None, run=run_command, holder=None, systemd=None):
    """Carry out one `--svc-*` verb.

    Args:
        verb: Verb name.
        env: Environment mapping to read.
        force: Replace or delete a unit luks-tray did not write.
        yes: Answer yes to prompts.
        echo: Callable used to print output.
        ask: Callable used to ask questions.
        run: Command runner.
        holder: Pid of a running instance outside systemd.
        systemd: Override for the systemd availability check.

    Returns:
        int: Process exit status.
    """
    env = _env(env)
    ask = (ask or _no_prompt)
    if yes:
        ask = _auto_yes

    if verb == 'status':
        return _report(env, echo, run)

    if verb == 'show-file':
        for line in rendered_unit(env).splitlines():
            echo(line)
        return OK

    if not (systemd_available(env) if systemd is None else systemd):
        raise ServiceError(
            'no systemd user manager is reachable (is this a systemd system?);'
            ' use your desktop autostart instead')

    if verb in ('install', 'start', 'restart'):
        require_launcher(env)
        rendered = rendered_unit(env)
    else:
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
    """Print a plan before it runs."""
    for note in plan.notes:
        echo(note)
    for line in plan.details:
        echo(line)
    if plan.steps:
        echo('about to:')
        for step in plan.steps:
            echo(f'  {step.describe}')


def _report(env, echo, run):
    """Print the state of the unit and return the documented status code.

    Args:
        env: Environment mapping to read.
        echo: Callable used to print output.
        run: Command runner.

    Returns:
        int: `STATUS_ACTIVE`, `STATUS_INACTIVE` or `STATUS_ABSENT`.
    """
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
