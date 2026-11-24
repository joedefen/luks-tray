#!/usr/bin/env python3
"""Determine the real display server and repair the session variables."""
import os
import socket
import stat
import subprocess

X11_SOCKET_DIR = '/tmp/.X11-unix'

SESSION_VARIABLES = ('XDG_SESSION_TYPE', 'WAYLAND_DISPLAY', 'DISPLAY')

WAYLAND = 'wayland'
X11 = 'x11'

X11_PROBE_TIMEOUT = 2.0

COOKIE_NAME = b'MIT-MAGIC-COOKIE-1'

FAMILY_WILD = 0xffff

SETUP_FAILED = 0
SETUP_SUCCESS = 1
SETUP_AUTHENTICATE = 2


def _is_socket(path):
    """Return True when `path` is a unix socket."""
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _runtime_dir(env):
    """Return the stripped `XDG_RUNTIME_DIR` value."""
    return (env.get('XDG_RUNTIME_DIR') or '').strip()


def x11_socket_path(display):
    """Return the socket path an X11 `DISPLAY` names, or `''`.

    Args:
        display: X11 display string.

    Returns:
        str: The socket path.
    """
    host, _, rest = (display or '').partition(':')
    if host or not rest:
        return ''
    number = rest.split('.')[0]
    return os.path.join(X11_SOCKET_DIR, f'X{number}') if number.isdigit() else ''


def live_wayland(env=None):
    """Return `$WAYLAND_DISPLAY` when its socket is listening, else `''`.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The live compositor name.
    """
    env = os.environ if env is None else env
    name = (env.get('WAYLAND_DISPLAY') or '').strip()
    if not name:
        return ''
    if os.path.isabs(name):
        path = name
    else:
        runtime = _runtime_dir(env)
        if not runtime:
            return ''
        path = os.path.join(runtime, name)
    return name if _is_socket(path) else ''


def live_x11(env=None):
    """Return `$DISPLAY` when its socket is listening, else `''`.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The live display string.
    """
    env = os.environ if env is None else env
    display = (env.get('DISPLAY') or '').strip()
    path = x11_socket_path(display)
    return display if path and _is_socket(path) else ''


def _named_kind(env, wayland, display):
    """Return the server type the environment points at, or `''`.

    Args:
        env: Environment mapping to read.
        wayland: Live Wayland display, or `''`.
        display: Live X11 display, or `''`.

    Returns:
        str: `'wayland'`, `'x11'` or `''`.
    """
    named = (env.get('XDG_SESSION_TYPE') or '').strip().lower()
    if wayland and display:
        return named if named in (WAYLAND, X11) else WAYLAND
    if wayland:
        return WAYLAND
    if display:
        return X11
    return ''


def display_server(env=None):
    """Return `'wayland'`, `'x11'`, or `''` when no display server is listening.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The detected display server type.
    """
    env = os.environ if env is None else env
    return _named_kind(env, live_wayland(env), live_x11(env))


def _loginctl(*args):
    """Run one `loginctl` query and return its stripped output.

    Args:
        *args: Query arguments.

    Returns:
        str: The output, or `''` on error.
    """
    try:
        done = subprocess.run(('loginctl', *args), capture_output=True, text=True,
                              check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (done.stdout or '').strip() if done.returncode == 0 else ''


def newest_wayland_socket(env=None):
    """Return the newest `wayland-*` socket in the runtime directory, or `''`.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The socket name.
    """
    env = os.environ if env is None else env
    try:
        names = os.listdir(_runtime_dir(env))
    except OSError:
        return ''
    found = []
    for name in names:
        if not name.startswith(f'{WAYLAND}-') or name.endswith('.lock'):
            continue
        path = os.path.join(_runtime_dir(env), name)
        try:
            info = os.stat(path)
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode):
            found.append((info.st_mtime, name))
    return max(found)[1] if found else ''


def _from_loginctl(env, query):
    """Ask logind about the user's session.

    Args:
        env: Environment mapping to read.
        query: Callable that runs a `loginctl` query.

    Returns:
        tuple: `(kind, wayland name, display)`.
    """
    user = (env.get('USER') or env.get('LOGNAME') or '').strip()
    session = query('show-user', user, '-p', 'Display', '--value') if user else ''
    if not session:
        session = (env.get('XDG_SESSION_ID') or '').strip()
    if not session:
        return '', '', ''
    kind = query('show-session', session, '-p', 'Type', '--value').strip().lower()
    if kind == WAYLAND:
        return WAYLAND, newest_wayland_socket(env), ''
    if kind == X11:
        display = query('show-session', session, '-p', 'Display', '--value').strip()
        return X11, '', display or ':0'
    return '', '', ''


def session_values(env=None, query=None):
    """Return the values the session variables should have.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.
        query: Callable that runs a `loginctl` query.

    Returns:
        dict | None: Values by variable name, or None when unknown. A value of
        None means the variable should be removed.
    """
    env = os.environ if env is None else env
    query = _loginctl if query is None else query
    wayland = live_wayland(env)
    display = live_x11(env)
    kind = _named_kind(env, wayland, display)
    if not kind:
        kind, wayland, display = _from_loginctl(env, query)
    if kind == WAYLAND:
        return {'XDG_SESSION_TYPE': WAYLAND, 'WAYLAND_DISPLAY': wayland,
                'DISPLAY': display or None}
    if kind == X11:
        return {'XDG_SESSION_TYPE': X11, 'WAYLAND_DISPLAY': wayland or None,
                'DISPLAY': display}
    return None


def repair_environment(env=None, query=None, note=None):
    """Point the session variables at the display server that is listening.

    Args:
        env: Environment mapping to edit; defaults to `os.environ`.
        query: Callable that runs a `loginctl` query.
        note: Callable that receives a message when nothing can be repaired.

    Returns:
        list: Applied changes as `(name, old, new)` tuples.
    """
    env = os.environ if env is None else env
    note = (lambda *_args, **_kwargs: None) if note is None else note
    wanted = session_values(env, query)
    if wanted is None:
        note('no display server is listening and loginctl named no session;'
             ' leaving the display variables alone')
        return []
    changes = []
    for name in SESSION_VARIABLES:
        new = wanted[name]
        old = env.get(name)
        if new is None:
            if old is not None:
                del env[name]
                changes.append((name, old, None))
        elif old != new:
            env[name] = new
            changes.append((name, old, new))
    return changes


def authority_file(env=None):
    """Return the Xauthority file Qt will read, or `''`.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The authority file path.
    """
    env = os.environ if env is None else env
    named = (env.get('XAUTHORITY') or '').strip()
    if named:
        return named
    home = (env.get('HOME') or '').strip()
    return os.path.join(home, '.Xauthority') if home else ''


def _field(blob, at):
    """Read one length-prefixed Xauthority field.

    Args:
        blob: Xauthority bytes.
        at: Offset of the length prefix.

    Returns:
        tuple: The field bytes and the next offset.

    Raises:
        ValueError: When the field is truncated.
    """
    if at + 2 > len(blob):
        raise ValueError('truncated')
    length = int.from_bytes(blob[at:at + 2], 'big')
    start = at + 2
    if start + length > len(blob):
        raise ValueError('truncated')
    return blob[start:start + length], start + length


def _authority_records(blob):
    """Yield `(family, number, name, cookie)` from an Xauthority file.

    Args:
        blob: Xauthority bytes.

    Yields:
        tuple: One record per iteration.

    Raises:
        ValueError: When a record is truncated.
    """
    at = 0
    while at < len(blob):
        if at + 2 > len(blob):
            raise ValueError('truncated')
        family = int.from_bytes(blob[at:at + 2], 'big')
        at += 2
        _address, at = _field(blob, at)
        number, at = _field(blob, at)
        name, at = _field(blob, at)
        cookie, at = _field(blob, at)
        yield family, number, name, cookie


def authority_cookie(display, env=None):
    """Return the cookie libxcb would send for `display`.

    Args:
        display: X11 display string.
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        tuple: `(cookie, known)`, where `known` is False when the authority
        file could not be read or parsed.
    """
    path = authority_file(env)
    if not path:
        return b'', True
    try:
        with open(path, 'rb') as handle:
            blob = handle.read()
    except FileNotFoundError:
        return b'', True
    except OSError:
        return b'', False
    try:
        records = list(_authority_records(blob))
    except ValueError:
        return b'', False
    number = display.partition(':')[2].split('.')[0].encode()
    for family, record_number, name, cookie in records:
        if name != COOKIE_NAME:
            continue
        if family == FAMILY_WILD or record_number == number:
            return cookie, True
    return b'', True


def x11_setup_status(socket_path, cookie=b''):
    """Return the answer an X server gives to a connection setup request.

    Args:
        socket_path: X11 socket path.
        cookie: Authority cookie to offer.

    Returns:
        int: `SETUP_FAILED`, `SETUP_SUCCESS` or `SETUP_AUTHENTICATE`.
    """
    name = COOKIE_NAME
    request = (b'l\x00' + (11).to_bytes(2, 'little') + (0).to_bytes(2, 'little')
               + len(name).to_bytes(2, 'little')
               + len(cookie).to_bytes(2, 'little') + b'\x00\x00'
               + name + b'\x00' * (-len(name) % 4)
               + cookie + b'\x00' * (-len(cookie) % 4))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(X11_PROBE_TIMEOUT)
        conn.connect(socket_path)
        conn.sendall(request)
        answer = b''
        while len(answer) < 8:
            chunk = conn.recv(64)
            if not chunk:
                break
            answer += chunk
    return answer[0] if answer else SETUP_FAILED


def _wayland_problem(env):
    """Return why this Wayland session has no reachable compositor.

    Args:
        env: Environment mapping to read.

    Returns:
        str: The problem text, or `''` when Qt can proceed.
    """
    if live_wayland(env):
        return ''
    display = (env.get('DISPLAY') or '').strip()
    if display and not x11_socket_path(display):
        return ''
    if live_x11(env):
        return ''
    named = (env.get('WAYLAND_DISPLAY') or '').strip()
    return (f'the Wayland compositor named by WAYLAND_DISPLAY={named or "(unset)"}'
            f' is not listening, and no X11 server is available: the session is'
            f' not up yet, or it has just ended')


def display_problem(env=None):
    """Return why Qt could not use this session's display, or `''`.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The problem text, or `''` when the display looks usable.
    """
    env = os.environ if env is None else env
    kind = (env.get('XDG_SESSION_TYPE') or '').strip().lower()
    if kind == WAYLAND:
        return _wayland_problem(env)
    if kind != X11:
        return ''
    display = (env.get('DISPLAY') or '').strip()
    path = x11_socket_path(display)
    if not path:
        return ''
    cookie, known = authority_cookie(display, env)
    try:
        status = x11_setup_status(path, cookie)
    except OSError as error:
        try:
            status = x11_setup_status('\0' + path, cookie)
        except OSError:
            return (f'the X server named by DISPLAY={display} cannot be reached'
                    f' ({error.strerror or error}): the session is not up yet,'
                    f' or it has just ended')
    if status == SETUP_SUCCESS:
        return ''
    if status == SETUP_AUTHENTICATE:
        if not known:
            return ''
        named = authority_file(env) or '(no authority file)'
        return (f'the X server on {display} refused the connection: it wants an'
                f' authority cookie and {named} holds none for that display.'
                f' Qt reports this as "could not connect to display" and then'
                f' aborts, so the applet leaves first. An X11 login keeps its'
                f' cookie in $XAUTHORITY; a systemd --user unit only has that'
                f' variable if the session exported the environment to the'
                f' manager (dbus-update-activation-environment --systemd'
                f' XAUTHORITY DISPLAY)')
    return (f'the X server on {display} refused the connection (setup status'
            f' {status})')
