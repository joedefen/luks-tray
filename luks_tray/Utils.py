#!/usr/bin/env python3
"""Logging, process and single-instance helpers."""

# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import contextlib
import inspect
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO
from types import SimpleNamespace

try:
    import fcntl
except ImportError:     # non-POSIX: SingleInstance degrades to a no-op
    fcntl = None

# Destination and size cap for prt(); `opened_path` records the file this
# process redirected fd 1 onto, and `keep_stdout` is the -o request.
_log = SimpleNamespace(path='', opened_path='', max_kb=512, keep_stdout=False)
# Keys already emitted through prt_once().
_prt_seen = set()
PRT_ONCE_MAX = 256

# Failures this session reported, for the menu's log marker.
_problems = SimpleNamespace(count=0, last='')
_ERROR_PREFIXES = ('ERROR', 'FAIL:', 'exception:')

LOG_OLD_SUFFIX = '.old'

# Deadlines for state-changing commands, read-only probes and one-shot
# formatting commands, in seconds.
CMD_TIMEOUT = 60
PROBE_TIMEOUT = 15
FORMAT_TIMEOUT = 900
TIMEOUT_RC = 124


def where(above=0):
    """Return the `[file:line]` of a caller in this module.

    Args:
        above: Extra frames to move up from the reference frame.

    Returns:
        str: A `[file:line]` footer.
    """
    stack, frameNo = inspect.stack(context=0), 2 + above
    if frameNo < 0 or len(stack) < frameNo + 2:
        return '[n/a]'
    filename, line_number = stack[frameNo][1:3]
    return f'[{filename.split("/")[-1]}:{line_number}]'


def _is_tty():
    """Return True when stdout is a terminal."""
    try:
        os.ttyname(sys.stdout.fileno())
        return True
    except Exception:                               # pylint: disable=broad-except
        return False


def _is_reg():
    """Return True when stdout is an open regular file."""
    try:
        return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
    except Exception:                               # pylint: disable=broad-except
        return False


def _same_file(fd, path):
    """Return True when `fd` and `path` name the same file."""
    try:
        open_file, named_file = os.fstat(fd), os.stat(path)
    except (OSError, ValueError):
        return False
    return (open_file.st_dev, open_file.st_ino) == (named_file.st_dev,
                                                    named_file.st_ino)


def _is_error_message(args):
    """Return True when a prt() call reports a failure.

    Args:
        args: Positional arguments passed to `prt`.

    Returns:
        bool: Whether the first argument marks a failure.
    """
    if not args or not isinstance(args[0], str):
        return False
    return args[0].lstrip().startswith(_ERROR_PREFIXES)


def _reopen_log():
    """Point stdout and stderr at the configured log, in append mode."""
    try:
        fd = os.open(_log.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o600)
    try:
        sys.stdout = os.fdopen(fd, 'a', encoding='utf-8')
    except OSError:
        os.close(fd)
        return
    sys.stderr = sys.stdout
    os.dup2(sys.stdout.fileno(), 1)
    os.dup2(sys.stderr.fileno(), 2)
    _log.opened_path = _log.path


def _append_log(text):
    """Append one line to the log when stdout is not the log itself."""
    if not _log.path:
        return
    if _log.max_kb > 0:
        rotate_log()
    try:
        fd = os.open(_log.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        os.write(fd, f'{text}\n'.encode('utf-8', 'replace'))
    except OSError:
        pass
    finally:
        os.close(fd)


def rotate_log(path=None, max_bytes=None):
    """Move the live log to `<path>.old` when it outgrows its cap.

    Args:
        path: Log path; defaults to the configured one.
        max_bytes: Size limit; defaults to `_log.max_kb`.

    Returns:
        bool: True when a rotation happened.
    """
    path = _log.path if path is None else path
    if not path:
        return False
    limit = (_log.max_kb * 1024) if max_bytes is None else max_bytes
    try:
        if os.path.getsize(path) <= limit:
            return False
        os.replace(path, f'{path}{LOG_OLD_SUFFIX}')
        return True
    except OSError:
        return False


def configure_log(path, max_kb=512, keep_stdout=False):
    """Point prt() at `path` and redirect output onto it.

    Args:
        path: Log file path.
        max_kb: Size cap in KiB; a non-positive value disables logging.
        keep_stdout: Keep the caller's stdout instead of taking it over.
    """
    _log.path = path or ''
    _log.max_kb = max_kb
    _log.keep_stdout = bool(keep_stdout)
    if not _log.path or _log.max_kb <= 0:
        return
    if _log.keep_stdout:
        return
    if _is_tty():
        return
    if _is_reg() and not _log.opened_path:
        return
    _reopen_log()


def prt(*args, **kwargs):
    """Print a timestamped line, mirroring it to the configured log.

    Args:
        *args: Values passed through to `print`.
        **kwargs: Options consumed here are `to_stdout`, `_above` and
            `_origin`; the rest are passed to `print`.
    """
    def check_stdout(use_stdout=None):
        """Return True when the line must also be appended to the log."""
        if not (_log.max_kb > 0 and _log.path):
            return False
        if use_stdout is False:
            _log.keep_stdout = False
        if _log.keep_stdout:
            return False
        if not _log.opened_path and (sys.stdout.closed
                or (not _is_tty() and not _is_reg())):
            _reopen_log()
        if _log.opened_path:
            if rotate_log():
                _reopen_log()
            return False
        if _is_reg() and _same_file(sys.stdout.fileno(), _log.path):
            return False
        return True

    to_stdout = None
    if 'to_stdout' in kwargs:
        to_stdout = bool(kwargs['to_stdout'])
        del kwargs['to_stdout']
    above = 0
    if '_above' in kwargs:
        above = int(kwargs['_above'])
        del kwargs['_above']
    origin = None
    if '_origin' in kwargs:
        origin = kwargs['_origin']
        del kwargs['_origin']
    mirror = check_stdout(to_stdout)

    dt = datetime.now().strftime('%m-%d^%H:%M:%S')
    s = StringIO()
    print(dt, end=' ', file=s)
    kwargs['end'] = ' '
    kwargs['file'] = s
    print(*args, **kwargs)
    footer = where(above) if origin is None else str(origin)
    string = f'{s.getvalue()} {footer}'.rstrip()
    if _is_error_message(args):
        note_problem(string)
    print(string, flush=True)
    if mirror:
        _append_log(string)


def prt_qt(text, origin=''):
    """Print a Qt message with a timestamp and origin.

    Args:
        text: Message text.
        origin: `[file:line]` reported by Qt.
    """
    prt('qt:', text, _origin=origin)


def note_problem(message):
    """Record a failure this session reported.

    Args:
        message: Failure text.

    Returns:
        int: The new failure count.
    """
    _problems.count += 1
    _problems.last = str(message)
    return _problems.count


def session_problems():
    """Return the number of failures this process reported.

    Returns:
        int: The failure count.
    """
    return _problems.count


def clear_session_problems():
    """Forget the failure tally."""
    _problems.count = 0
    _problems.last = ''


def log_session_start(name, version, pid=None, note=''):
    """Write the log line that starts a session.

    Args:
        name: Applet name.
        version: Applet version.
        pid: Process ID; defaults to the current process.
        note: Extra text appended to the line.
    """
    clear_session_problems()
    pid = os.getpid() if pid is None else pid
    prt(f'=== {name} v{version} started, pid {pid}{note} ===', _origin='')


def log_session_stop(name, version, pid=None, note=''):
    """Write the log line that ends a session.

    Args:
        name: Applet name.
        version: Applet version.
        pid: Process ID; defaults to the current process.
        note: Extra text appended to the line.
    """
    pid = os.getpid() if pid is None else pid
    problems = _problems.count
    tail = f', {problems} error{"" if problems == 1 else "s"}' if problems else ''
    prt(f'=== {name} v{version} stopped, pid {pid}{note}{tail} ===', _origin='')


def install_termination_log(name, version, signals=(signal.SIGTERM, signal.SIGHUP)):
    """Log the stop line when a signal ends the process.

    Args:
        name: Applet name.
        version: Applet version.
        signals: Signals to handle.
    """
    def handler(signum, _frame):
        """Log the stop line, then die of the signal."""
        log_session_stop(name, version, note=f' (signal {signum})')
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for number in signals:
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            return


def prt_once(key, *args, **kwargs):
    """Call prt() at most once per `key` in this process.

    Args:
        key: Identity of the failure.
        *args: Values passed through to `prt`.
        **kwargs: Options passed through to `prt`.
    """
    if key in _prt_seen:
        return
    if len(_prt_seen) >= PRT_ONCE_MAX:
        _prt_seen.clear()
    _prt_seen.add(key)
    kwargs['_above'] = kwargs.get('_above', 0) + 1
    prt(*args, **kwargs)


_console_fd = None


def capture_console():
    """Keep a duplicate of stderr for start-up errors.

    Returns:
        int: The duplicated descriptor, or -1 when unavailable.
    """
    global _console_fd
    if _console_fd is None:
        try:
            _console_fd = os.dup(2)
        except OSError:
            _console_fd = -1
    return _console_fd


def error_print(message):
    """Print `message` to the console the user started us from.

    Args:
        message: Text to print.
    """
    text = message if message.endswith('\n') else f'{message}\n'
    if _console_fd is not None and _console_fd >= 0:
        try:
            os.write(_console_fd, text.encode('utf-8', 'replace'))
            return
        except OSError:
            pass
    with contextlib.suppress(OSError, ValueError, AttributeError):
        stream = sys.__stderr__ or sys.stderr
        stream.write(text)
        stream.flush()


def in_systemd_service(env=None):
    """Return True when a systemd unit started this process.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        bool: Whether `INVOCATION_ID` is set.
    """
    return bool((os.environ if env is None else env).get('INVOCATION_ID'))


def spawn_detached(command, spawn=None, env=None, quiet=False):
    """Start `command` so that it outlives this process.

    Args:
        command: Command and arguments.
        spawn: Callable used to start the process; defaults to `Popen`.
        env: Environment mapping to read; defaults to `os.environ`.
        quiet: Connect the child's standard streams to `/dev/null`.

    Returns:
        subprocess.Popen | None: The started process.
    """
    command = [str(part) for part in command]
    if not command:
        return None
    env = os.environ if env is None else env
    runner = shutil.which('systemd-run', path=env.get('PATH'))
    if runner and in_systemd_service(env):
        command = [runner, '--user', '--scope', '--quiet', '--', *command]
    spawn = subprocess.Popen if spawn is None else spawn
    stdio = {}
    if quiet:
        stdio = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                 'stderr': subprocess.DEVNULL}
    return spawn(command, start_new_session=True, **stdio)


# Terminal emulators that accept a command, with flag and argument style.
_TERMINALS = (
    ('x-terminal-emulator', '-e', 'argv'),
    ('gnome-terminal', '--', 'argv'),
    ('konsole', '-e', 'argv'),
    ('kitty', '-e', 'argv'),
    ('alacritty', '-e', 'argv'),
    ('foot', '-e', 'argv'),
    ('xterm', '-e', 'argv'),
    ('xfce4-terminal', '-e', 'string'),
    ('mate-terminal', '-e', 'string'),
    ('lxterminal', '-e', 'string'),
)

_less_follow_name = None


def terminal_argv(command, preferred=''):
    """Wrap `command` so it runs in a terminal window.

    Args:
        command: Command and arguments to run.
        preferred: Configured command prefix; empty to auto-detect.

    Returns:
        list: The wrapped argv, or `[]` when no terminal was found.
    """
    command = [str(part) for part in command]
    if preferred and preferred.strip():
        try:
            return shlex.split(preferred) + command
        except ValueError:
            prt_once(f'log_viewer {preferred!r}',
                     'ERROR: cannot parse log_viewer; auto-detecting a terminal')
    for name, flag, style in _TERMINALS:
        exe = shutil.which(name)
        if not exe:
            continue
        if style == 'string':
            return [exe, flag, shlex.join(command)] if command else [exe, flag]
        return [exe, flag, *command]
    return []


def less_follow_name_supported():
    """Return True when the installed `less` supports `--follow-name`.

    Returns:
        bool: Whether `less --help` mentions the option.
    """
    global _less_follow_name
    if _less_follow_name is None:
        _less_follow_name = False
        try:
            result = subprocess.run(['less', '--help'], capture_output=True,
                                    timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return _less_follow_name
        text = (result.stdout + result.stderr).decode('utf-8', 'replace')
        _less_follow_name = '--follow-name' in text
    return _less_follow_name


def log_viewer_argv(log_path, old_path=None, preferred=''):
    """Return argv that pages the applet log in a terminal window.

    Args:
        log_path: Current log path.
        old_path: Rotated log path, when it exists.
        preferred: Configured command prefix; empty to auto-detect.

    Returns:
        list: The viewer argv, or `[]` when it cannot be started.
    """
    command = ['less']
    if less_follow_name_supported():
        command.append('--follow-name')
    command.append('+F')
    command.append(str(log_path))
    if old_path and os.path.exists(old_path):
        command.append(str(old_path))
    return terminal_argv(command, preferred)


_PYTHON_RE = re.compile(r'^python[0-9.]*$')


def script_key(name):
    """Reduce a path, script or module name to a comparable key.

    Args:
        name: Path, script or module name.

    Returns:
        str: The lower-cased key.
    """
    base = os.path.basename(str(name)).lower()
    if base.endswith('.py'):
        base = base[:-3]
    return base.replace('_', '-')


def command_names(argv):
    """Return the names a process argv claims to be running.

    Args:
        argv: Process arguments.

    Returns:
        list: Candidate names for matching.
    """
    if not argv:
        return []
    exe = os.path.basename(argv[0])
    if not _PYTHON_RE.match(exe):
        return [exe]
    names = []
    rest = argv[1:]
    for index, token in enumerate(rest):
        if token == '-m' and index + 1 < len(rest):
            module = rest[index + 1]
            parts = [part for part in module.split('.') if part]
            names.extend('.'.join(parts[:end]) for end in range(1, len(parts) + 1))
            break
        if token in ('-c',):
            break
        if token.startswith('-'):
            continue
        script = token
        names.append(os.path.basename(script))
        parent = os.path.basename(os.path.dirname(script))
        if parent:
            names.append(parent)
        break
    return names


class PyKill:
    """Find other applet instances and terminate one on request."""

    @staticmethod
    def _iter_matches(targets):
        """Yield `(pid, ps line, matched keys)` for each live target.

        Args:
            targets: Names to match.

        Yields:
            tuple: Process id, command line and matched keys.
        """
        wanted = {script_key(target) for target in targets}
        for line in os.popen("ps -eo pid,args"):
            words = line.split()
            if len(words) < 2:
                continue
            try:
                pid = int(words[0])
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            names = {script_key(name) for name in command_names(words[1:])}
            matched = wanted & names
            if matched:
                yield pid, line.strip(), matched

    @staticmethod
    def matching_pids(targets):
        """Return `{pid: ps line}` for live processes that look like targets.

        Args:
            targets: Names to match.

        Returns:
            dict: Matching processes.
        """
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        return {pid: line for pid, line, _ in PyKill._iter_matches(targets)}

    @staticmethod
    def kill_pid(pid, targets, grace=3.0, delay=0.1):
        """Terminate one process, but only if its argv still names a target.

        Args:
            pid: Process id to terminate.
            targets: Names the process must still match.
            grace: Seconds to wait after SIGTERM.
            delay: Polling interval while waiting.

        Returns:
            bool: True when the process is gone.
        """
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        if pid not in PyKill.matching_pids(targets):
            return False
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
            try:
                os.kill(pid, sig)
            except OSError:
                return True
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(delay)
                if pid not in PyKill.matching_pids(targets):
                    return True
        return pid not in PyKill.matching_pids(targets)


class SingleInstance:
    """Exclusive PID/lock file that keeps two trays from coexisting."""

    current = None

    def __init__(self, path):
        """Record the lock path and start with no descriptor.

        Args:
            path: Lock file path.
        """
        self.path = path
        self.fd = None
        self.pid = None

    @staticmethod
    def read_pid(path):
        """Return the pid recorded in `path`, or None.

        Args:
            path: Lock file path.

        Returns:
            int | None: The recorded pid.
        """
        try:
            with open(path, encoding='utf-8') as handle:
                return int(handle.read().strip()) or None
        except (OSError, ValueError):
            return None

    def acquire(self, retries=0, delay=0.2):
        """Take the lock.

        Args:
            retries: Additional attempts when the lock is held.
            delay: Seconds between attempts.

        Returns:
            bool: False when another live process holds the lock.
        """
        if fcntl is None:
            return True
        if self.fd is not None:
            return True
        for attempt in range(max(1, retries + 1)):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                if attempt < retries:
                    time.sleep(delay)
                    continue
                return False
            os.ftruncate(fd, 0)
            os.write(fd, f'{os.getpid()}\n'.encode('ascii'))
            os.fsync(fd)
            self.fd, self.pid = fd, os.getpid()
            SingleInstance.current = self
            return True
        return False

    def release(self):
        """Drop the lock."""
        if SingleInstance.current is self:
            SingleInstance.current = None
        if self.fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = None

    @classmethod
    def release_current(cls):
        """Release the lock held by this process, if any."""
        if cls.current is not None:
            cls.current.release()
