#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utility functions
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import contextlib
import os
import sys
import stat
import shlex
import shutil
import subprocess
import inspect
import re
import signal
import time
from datetime import datetime
from io import StringIO
from types import SimpleNamespace

try:
    import fcntl
except ImportError:     # non-POSIX: SingleInstance degrades to a no-op
    fcntl = None

# Where prt() sends its output, and the size that rotates the log. One private
# object replaces the pair of bare globals this used to be (`prt_path` plus an
# "already redirected" flag): the flag could only ever be set, so assigning the
# path a second time was a silent no-op and the target was decided by whichever
# prt() call happened to run first. `configure_log()` owns that transition now.
# `opened_path` is the file *this process* redirected fd 1 onto ('' = never), so
# a later `configure_log()` may take the stream over again -- while a stdout the
# caller redirected themselves (a regular file we did not open) stays untouched.
# `keep_stdout` is the `-o/--stdout` request: the path is still recorded (the
# menu and the service verbs point at it) but nothing is redirected, so a
# hand-run `luks-tray -o` logs to the terminal it was started from.
_log = SimpleNamespace(path='', opened_path='', max_kb=512, keep_stdout=False)
# Keys already emitted through prt_once(). Bounded, because the tray runs for
# weeks: when the set fills up it is cleared wholesale, which at worst repeats
# a warning the user has already seen.
_prt_seen = set()
PRT_ONCE_MAX = 256

# Failures this session reported, for the menu's "the log has something to say"
# marker. Counted where the message is made rather than by reading the file
# back: prt() counts its own "ERROR: ..." / "exception: ..." lines, the dialogs
# count the errors they are about to show, and a warning that is part of normal
# operation ("skipping auto-mount cleanup: ...") is deliberately not an error.
_problems = SimpleNamespace(count=0, last='')
# A prt() call whose first argument starts with one of these is a failure. The
# strings are the ones the code already uses; nothing has to remember to report
# itself twice.
_ERROR_PREFIXES = ('ERROR', 'FAIL:', 'exception:')

# One generation of history: `debug.log` becomes `debug.log.old`, and the .old
# that was there before is discarded. Two files are all we keep, and the log
# viewer pages both of them (`:n` in less), so the suffix is a constant rather
# than a literal spelled out at each of the places that name the file.
LOG_OLD_SUFFIX = '.old'

# Deadline, in seconds, for external commands that change system state
# (cryptsetup, mount, umount, mkfs, losetup). They all run on the Qt main
# thread, so a wedged child freezes the tray menu until it returns -- and
# main() points stdin at /dev/null, so there is no terminal left to
# interrupt it. 60s leaves room for a slow argon2id unlock on old hardware.
CMD_TIMEOUT = 60
# Deadline for read-only probes (lsblk, blkid, fuser). They are expected to
# answer at once; a hung probe just keeps the previous snapshot and is
# retried on the next menu refresh.
PROBE_TIMEOUT = 15
# Deadline for the one-shot setup commands that legitimately take a while
# (mke2fs on a multi-terabyte container, luksFormat's PBKDF benchmark on a
# slow machine). Still bounded -- just far more generous than CMD_TIMEOUT.
FORMAT_TIMEOUT = 900
# Status reported when a deadline is hit (the value coreutils `timeout` uses);
# every caller already treats a non-zero status as failure.
TIMEOUT_RC = 124


def where(above=0):
    """Get the file and line of the caller. Arguments:
     -  above -- how many frames to go up (or down) from the reference
        frame (which is 2 above). above=0 means the caller of the
        caller of the function (which is the frame ofkjk interest usually).
    Then keep going up until we get out of this file if possible
    Returns:
        [file:line]
    """
    # context=0: only the filename and line number are wanted, and asking for
    # source text makes every stack frame read a file. prt() runs on the 3 s
    # refresh path, so that cost is paid constantly for an unread answer.
    stack, frameNo = inspect.stack(context=0), 2 + above
    if frameNo < 0 or len(stack) < frameNo + 2:
        return '[n/a]'
    filename, line_number = stack[frameNo][1:3]
    return f'[{filename.split("/")[-1]}:{line_number}]'


def _is_tty():
    """ True if stdout is a terminal. Silent by design: called from prt(). """
    try:
        os.ttyname(sys.stdout.fileno())
        return True
    except Exception:                               # pylint: disable=broad-except
        return False


def _is_reg():
    """ True if stdout is already an open regular file. Silent, same reason. """
    try:
        return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
    except Exception:                               # pylint: disable=broad-except
        return False


def _same_file(fd, path):
    """ True when `fd` and `path` are the same file. Silent, same reason. """
    try:
        open_file, named_file = os.fstat(fd), os.stat(path)
    except (OSError, ValueError):
        return False
    return (open_file.st_dev, open_file.st_ino) == (named_file.st_dev,
                                                    named_file.st_ino)


def _is_error_message(args):
    """ True when a prt() call reports a failure, judged by its first argument. """
    if not args or not isinstance(args[0], str):
        return False
    return args[0].lstrip().startswith(_ERROR_PREFIXES)


def _reopen_log():
    """ Point stdout and stderr at the configured log, in append mode.

        The file is created owner-only (`0o600`) and an older, wider one is
        tightened in place. The log names container paths, loop devices and
        mount points -- the same facts `history.json` keeps behind a password --
        and a desktop that starts the tray decides the umask we inherit, so
        leaving the mode to `open()` is not a decision at all.
    """
    try:
        fd = os.open(_log.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return              # Cannot log there; keep whatever stdout already is.
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o600)    # an already existing file keeps its old mode
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
    """ Append one line to the log without owning stdout.

        The mirror case: stdout is a terminal (or a file the caller redirected
        themselves), so prt() keeps writing there -- but the log is still the
        default destination, and "Open Log" has to be able to show a session the
        user started from a terminal, not only one a desktop started.

        The file is opened and closed per line, because this path only exists
        when we are *not* holding the stream; rotation is decided here, on the
        log's own size, before the append that would cross the cap.
    """
    if not _log.path:
        return
    if _log.max_kb > 0:
        rotate_log()
    try:
        fd = os.open(_log.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return              # Cannot write there; the terminal copy still printed.
    try:
        os.write(fd, f'{text}\n'.encode('utf-8', 'replace'))
    except OSError:
        pass
    finally:
        os.close(fd)


def rotate_log(path=None, max_bytes=None):
    """ Move the live log to `<path>.old`; True when a rotation happened.

        `os.replace()` gives the wanted order -- the previous .old is discarded,
        the current log becomes .old -- in a single atomic rename, so there is
        never a moment in which no log file exists; the caller's reopen then
        creates the fresh current file.

        The size measured is the size of *the log*, not of whatever stdout
        happens to be: a terminal (or a `-o/--stdout` run) must not rotate a
        file we are not writing.
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
    """ Point prt() at `path` and redirect stdout/stderr onto it.

        Called from `main()` in place of assigning `Utils.prt_path` directly.
        The redirection happens here rather than lazily on the next prt(), so a
        message logged while the program is still coming up (the `IniTool`
        constructor, for instance) reaches the file instead of a stdout a
        desktop launch has already pointed nowhere.

        A second call *moves* the log: `opened_path` records the file this
        process opened, so the stream can be taken over again, where the old
        globals kept the very first decision for the lifetime of the process. A
        terminal is not taken over -- prt() keeps printing to it *and* mirrors
        the line into the log, see `_append_log()` -- and neither is a stdout the
        caller redirected to a file of their own. `max_kb <= 0` disables the
        redirection and the size cap, so prt() just prints.

        `keep_stdout` is `-o/--stdout`: the path is recorded but the streams are
        not taken over, so the messages go where the shell pointed them. The
        caller decides whether that request applies -- under systemd stdout is
        the unit's own redirect, not a request from a person.
    """
    _log.path = path or ''
    _log.max_kb = max_kb
    _log.keep_stdout = bool(keep_stdout)
    if not _log.path or _log.max_kb <= 0:
        return
    if _log.keep_stdout:
        return              # Asked to keep stdout (see main()'s -o handling).
    if _is_tty():
        return
    if _is_reg() and not _log.opened_path:
        return              # Someone redirected stdout; not ours to replace.
    _reopen_log()


def prt(*args, **kwargs):
    """ Our custom print routine ...
     - use instead of print() to get time stamps.
     - the log is the default destination: a stdout that is not a terminal is
       taken over (`~/.config/luks-tray/debug.log`), and one that *is* a terminal
       keeps the line *and* gets it mirrored into the log, so a session started
       by hand is visible in "Open Log" like one a desktop started
     - if we create a log file, its size is capped at `_log.max_kb` and then it
       rotates: debug.log becomes debug.log.old (one generation, see
       `rotate_log()`), so the log the menu shows never grows without bound
     - `to_stdout=True` keeps whatever stdout is, `to_stdout=False` insists on
       the log file (`configure_log(..., keep_stdout=True)` is the same request
       made once, for the whole run -- that is what `-o/--stdout` sets)
     - `_origin=` replaces the `[file:line]` footer, for lines that know better
       than a stack frame where they come from (the session banner, Qt's own
       messages)
    """
    def check_stdout(use_stdout=None):
        """ Point stdout at the log; True when the line must also be appended.

            The probes above (`_is_tty()` / `_is_reg()`) stay silent on purpose:
            they run *inside* prt(), so reporting a failure here would call prt()
            again and recurse until the stack runs out. "Not a tty" / "not a
            regular file" is also the normal answer for a tray started by a
            desktop session.
        """
        if not (_log.max_kb > 0 and _log.path): # non-positive disables tuning
            return False
        if use_stdout is False:       # explicit "write the log file"
            _log.keep_stdout = False
        if _log.keep_stdout:
            return False      # -o: whatever stdout already is, the log stays there.
        if not _log.opened_path and (sys.stdout.closed # stdout is closed
                or (not _is_tty() and not _is_reg())):
            _reopen_log()
        if _log.opened_path:
            # Only a stream we opened ourselves can be rotated: a terminal, or
            # a file the caller redirected, is not ours to move.
            if rotate_log():
                _reopen_log()   # the file we were appending to is now the .old
            return False        # stdout *is* the log, so printing is logging.
        if _is_reg() and _same_file(sys.stdout.fileno(), _log.path):
            return False        # stdout already *is* the log: do not write twice
        return True             # a terminal, or the caller's own file: mirror it

    to_stdout = None
    if 'to_stdout' in kwargs:
        to_stdout = bool(kwargs['to_stdout'])
        del kwargs['to_stdout']
    # prt_once() wraps this function, so it asks for one extra frame in the
    # [file:line] footer -- otherwise every one-time note would point at the
    # wrapper instead of at the handler that actually noticed the failure.
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
    """ Print a message Qt itself produced, stamped and footed like our lines.

        `origin` is the `[file:line]` Qt reported -- the C++ side, which is where
        the reader has to look, not a line of this package.
    """
    prt('qt:', text, _origin=origin)


# --- the session's first line, its last, and how many failures it saw -------


def note_problem(message):
    """ Record a failure this session reported; returns the new count.

        prt() calls this for its own error lines, and `alert_errors()` calls it
        before showing a message box, so the menu's marker counts what the user
        would call failures -- not every line in the file, and not the warnings
        that belong to normal operation.
    """
    _problems.count += 1
    _problems.last = str(message)
    return _problems.count


def session_problems():
    """ How many failures this process has reported (what the menu shows). """
    return _problems.count


def clear_session_problems():
    """ Forget the tally: a new session, or a test that wants a clean slate. """
    _problems.count = 0
    _problems.last = ''


def log_session_start(name, version, pid=None, note=''):
    """ Write the "this run started here" line: name, version, pid (prt dates it).

        What a reader of a log needs first is *which* run they are reading: the
        applet may have been restarted twice since the interesting line was
        written, and two instances of different builds can share one file. The
        failure tally is cleared here because the menu's marker is about this
        session, not about everything the file has ever collected.
    """
    clear_session_problems()
    pid = os.getpid() if pid is None else pid
    prt(f'=== {name} v{version} started, pid {pid}{note} ===', _origin='')


def log_session_stop(name, version, pid=None, note=''):
    """ Write the matching "this run ended here" line, with its failure count. """
    pid = os.getpid() if pid is None else pid
    problems = _problems.count
    tail = f', {problems} error{"" if problems == 1 else "s"}' if problems else ''
    prt(f'=== {name} v{version} stopped, pid {pid}{note}{tail} ===', _origin='')


def install_termination_log(name, version, signals=(signal.SIGTERM, signal.SIGHUP)):
    """ Log the stop line when a signal ends us, then die of that signal.

        `systemctl --user stop`, `--replace` and a closing terminal all end the
        process with a signal, and no atexit handler runs then -- so without this
        the most common way for a tray to go away left no trace at all. The
        handler restores the default disposition and re-raises, so the exit
        status the supervisor sees is exactly what it was before: death *by*
        the signal, which the shipped unit's `Restart=on-failure` and
        `RestartPreventExitStatus=` both read as "do not start me again".
    """
    def handler(signum, _frame):
        log_session_stop(name, version, note=f' (signal {signum})')
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for number in signals:
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):   # not the main thread, or no such signal
            return


def prt_once(key, *args, **kwargs):
    """ prt() at most once per `key` for the lifetime of this process.

        The menu refresh runs every 3 s, and several handlers on that path
        raise the *same* exception on every tick -- a block device without a
        `/sys/.../device/model` node, a loop device with no `backing_file`, an
        auto-mount folder that is not empty. Logging those with plain prt()
        would rotate the 512 KB debug log within minutes and bury the message
        that actually matters, so they log through here instead: the key is
        the identity of the failure (usually the path plus what failed), so a
        genuinely *new* failure still gets printed.
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
    """ Keep a duplicate of stderr, so start-up errors cannot be swallowed.

        prt() redirects both stdout and stderr into debug.log whenever they are
        not a terminal (a desktop autostart, a systemd unit).  A message the
        user must act on -- "another luks-tray is already running" -- has to
        bypass that redirect, so main() calls this *before* configure_log() and
        reports through error_print().
    """
    global _console_fd
    if _console_fd is None:
        try:
            _console_fd = os.dup(2)
        except OSError:
            _console_fd = -1
    return _console_fd


def error_print(message):
    """ Print `message` to the console the user started us from. """
    text = message if message.endswith('\n') else f'{message}\n'
    if _console_fd is not None and _console_fd >= 0:
        try:
            os.write(_console_fd, text.encode('utf-8', 'replace'))
            return
        except OSError:
            pass
    # Last resort: never let reporting an error raise a second one.
    with contextlib.suppress(OSError, ValueError, AttributeError):
        stream = sys.__stderr__ or sys.stderr
        stream.write(text)
        stream.flush()


def in_systemd_service(env=None):
    """ True when a systemd unit started this process.

        systemd sets INVOCATION_ID for every service it launches.  The tray
        reads it for two things, neither of which is "am I the applet's own
        unit?": where the log goes (inside a unit, `-o` is ignored because
        stdout is systemd's own redirect) and whether a spawned child has to be
        moved into a transient scope to outlive us (see `spawn_detached()`).
        The other question is asked of the cgroup, by
        `service.supervised_here()`.
    """
    return bool((os.environ if env is None else env).get('INVOCATION_ID'))


def spawn_detached(command, spawn=None, env=None, quiet=False):
    """ Start `command` so that it outlives this process.

        Under systemd the applet and everything it spawns share one cgroup, and
        systemd kills that whole cgroup on stop/restart (the default
        `KillMode=control-group`). `systemd-run --user --scope` puts the command
        into a transient scope of its own, a sibling of our unit, so a viewer
        opened from the menu survives the applet being restarted or stopped.
        `start_new_session=True` is setsid(2): it detaches the child from our
        session and controlling terminal, so the same call also works when the
        applet was started from a terminal or a DE autostart.

        This is for *viewers*.  A control request must not be detached: the
        transient scope is created out of our cgroup, so a stop job that begins
        at once kills the helper before it has asked systemd anything.  The
        menu's restart therefore runs `systemctl --no-block` synchronously
        (see `service.request_restart()`).

        `quiet=True` gives the child `/dev/null` on all three standard streams.
        It is for the children that are *viewers* rather than workers: by the
        time we spawn one, `prt()` has already `dup2()`-ed fds 1 and 2 onto
        debug.log, so an inheriting child writes straight into the log -- and a
        terminal emulator is itself a Qt application (konsole is Qt6), so it
        appends its own Qt chatter, palette table and all, in Qt's raw format.
        `qInstallMessageHandler()` cannot stop that: a message handler filters
        the messages of *its own* process, and those lines are another process's
        output. Giving the viewer /dev/null is the only place the leak can be
        closed, and a viewer has nothing to say that belongs in our log. The
        applet's own restart deliberately keeps the inherited descriptors: its
        output *is* this session's log.
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


# Terminals that can be asked to run a command, with the flag each one takes.
# 'string' means the flag takes the whole command line as one shell-quoted
# argument (the GLib terminals), 'argv' means it takes it as separate arguments.
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
    """ Wrap `command` so it runs in a terminal window; [] if none can be found.

        The applet normally has no tty -- prt() has redirected fds 1 and 2 into
        debug.log -- so a full-screen program such as less cannot draw on what
        it inherits. It has to be started inside a terminal emulator.

        `preferred` is the `log_viewer` setting: a command prefix such as
        'xterm -maximized -e' or 'gnome-terminal --'. When it is empty (or
        cannot be parsed, which only a hand-edited config.ini can produce) the
        first installed terminal from `_TERMINALS` is used, with the flag it
        expects.

        Called with an empty `command` it answers "can a viewer be started at
        all?", which is what the menu asks before offering the entry.
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
    """ True when the installed less knows --follow-name (less >= 451).

        With it, less reopens the log by name after a rotation instead of
        holding the renamed inode, which is what makes following debug.log
        survive our debug.log -> debug.log.old rename.
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
    """ argv that pages the applet log in a terminal window; [] if impossible.

        The current log is named first, so `less +F` starts following the live
        file; the rotated file (when it exists) follows and is reachable with
        `:n`.
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
    """ Reduce a path/script/module name to a comparable key.

        'luks_tray.Main', 'Main.py' and '/usr/bin/luks-tray' become
        'luks-tray.main', 'main' and 'luks-tray'.  Underscores are folded into
        dashes because the executable (luks-tray) and the package (luks_tray)
        use different spellings.
    """
    base = os.path.basename(str(name)).lower()
    if base.endswith('.py'):
        base = base[:-3]
    return base.replace('_', '-')


def command_names(argv):
    """ Return the names a process argv claims to be running.

        ``ps`` shows the *interpreter* command line, so a restarted instance is
        ``python -m luks_tray.Main``.  This returns the executable, the ``-m``
        module (and its package prefixes), or the script path, plus the
        script's directory name (which is the project name when running
        ``python luks_tray/Main.py``).
    """
    if not argv:
        return []
    exe = os.path.basename(argv[0])
    if not _PYTHON_RE.match(exe):
        return [exe]     # a real executable, e.g. an installed luks-tray
    names = []
    rest = argv[1:]
    for index, token in enumerate(rest):
        if token == '-m' and index + 1 < len(rest):
            module = rest[index + 1]
            parts = [part for part in module.split('.') if part]
            names.extend('.'.join(parts[:end]) for end in range(1, len(parts) + 1))
            break
        if token in ('-c',):
            break        # inline program: nothing to match on
        if token.startswith('-'):
            continue     # interpreter option (e.g. -u, -O, -X utf8=1)
        script = token
        names.append(os.path.basename(script))
        parent = os.path.basename(os.path.dirname(script))
        if parent:
            names.append(parent)
        break
    return names


class PyKill:
    """ Find other instances of the applet, and terminate one on request.

        There is deliberately no "kill every instance" entry point: a plain
        start must never shoot the incumbent, because with a supervisor that
        restarts it (a DE autostart, a --user unit) two launchers would then
        fight forever.  Killing is the explicit ``--replace`` takeover path in
        ``main()``, and even then only a pid whose command line still names the
        target is signalled -- which avoids suicide, and avoids hitting an
        unrelated process that recycled a stale pid.
    """

    @staticmethod
    def _iter_matches(targets):
        """ Yield ``(pid, ps line, matched keys)`` for every live target.

            One ``ps`` call, shared by `matching_pids()` and `kill_pid()` so a
            pid can never be signalled on evidence the lookup would not accept.
        """
        wanted = {script_key(target) for target in targets}
        for line in os.popen("ps -eo pid,args"):
            words = line.split()
            if len(words) < 2:
                continue
            try:
                pid = int(words[0])
            except ValueError:
                continue     # header line
            if pid == os.getpid():
                continue     # no suicides
            names = {script_key(name) for name in command_names(words[1:])}
            matched = wanted & names
            if matched:
                yield pid, line.strip(), matched

    @staticmethod
    def matching_pids(targets):
        """ Return ``{pid: ps line}`` for live processes that look like targets. """
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        return {pid: line for pid, line, _ in PyKill._iter_matches(targets)}

    @staticmethod
    def kill_pid(pid, targets, grace=3.0, delay=0.1):
        """ Terminate one process, but only if its argv still names a target.

            Returns True when the process is gone.  The name check is what makes
            ``--replace`` safe: the pid recorded in a lock file may since have
            been reused by an unrelated process, and a blind ``kill(2)`` would
            then hit the wrong program.
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
                return True      # already gone
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(delay)
                if pid not in PyKill.matching_pids(targets):
                    return True
        return pid not in PyKill.matching_pids(targets)


class SingleInstance:
    """ Exclusive PID/lock file: the guarantee that two trays never coexist.

        The lock -- not the process-name scan -- is authoritative.  A start
        tries to take it *first*; if another live process holds it, the
        newcomer reports the holder and exits unless ``--replace`` was given.
        The kernel drops the lock when the holder exits -- even on SIGKILL --
        so a crash cannot leave a stale lock that blocks the next start.  The
        file's contents are informational (the holder's pid).
    """
    current = None   # the instance holding the lock in this process

    def __init__(self, path):
        self.path = path
        self.fd = None
        self.pid = None

    @staticmethod
    def read_pid(path):
        """ Return the pid recorded in `path`, or None if unreadable. """
        try:
            with open(path, encoding='utf-8') as handle:
                return int(handle.read().strip()) or None
        except (OSError, ValueError):
            return None

    def acquire(self, retries=0, delay=0.2):
        """ Take the lock.  Returns False if another live process holds it.

            `retries`/`delay` cover the few milliseconds between restart_self()
            spawning the replacement and the old process finally exiting.
        """
        if fcntl is None:      # not POSIX: no locking available
            return True
        if self.fd is not None:    # already held by this object
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
        """ Drop the lock (also used before spawning a restarted instance). """
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
        """ Release the lock held by this process, if any. """
        if cls.current is not None:
            cls.current.release()
