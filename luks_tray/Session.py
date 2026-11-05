#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Which display server this login is really on, and the variables for it.

    `systemd --user` cannot hand a unit the session's display, so
    `service.render()` copies `WAYLAND_DISPLAY` (or `DISPLAY`) and the desktop
    name out of the installing shell and into the unit file.  That copy is a
    *snapshot of one login*: log out of a Wayland session and into one on X11,
    and the unit still says `WAYLAND_DISPLAY=wayland-1`.  A unit's own
    `Environment=` wins over what the session exports, so Qt reads the snapshot
    and either dies ("could not connect to display") or lands on a platform
    whose tray host is not there.  Until now the remedy was to remember
    `--svc-install` after every switch.

    The machine knows the answer, so it is asked instead:

    * a display server is *listening* when its socket exists -- the
      compositor's in `$XDG_RUNTIME_DIR`, the X server's in `/tmp/.X11-unix` --
      so a snapshot naming a server that is not listening is a snapshot of a
      session that has ended;
    * `loginctl` names the type for the case where the environment holds
      nothing usable at all (a unit started by a manager that never imported
      the session).

    `repair_environment()` applies that to `os.environ`, and `main()` calls it
    before `QApplication` exists, so one installed unit works in both sessions
    and switching needs no re-install.

    Naming a listening X11 server is not the same as being allowed to talk to
    it: an X11 login authenticates with a cookie, and the cookie file of an
    sddm/gdm login lives at a path (`$XAUTHORITY`) that a unit only has when the
    session imported it.  When it is missing, Qt's xcb plugin prints
    "Authorization required, but no authorization protocol specified" and
    "could not connect to display", and then *aborts* -- a core dump in
    `coredumpctl`, and a SIGABRT that the unit reads as a crash.  So the module
    also answers the question Qt is about to ask, by doing the same connection
    setup the plugin does (`display_problem()`), and `main()` turns a refusal
    into a log line and a retryable exit status instead.
"""
import os
import socket
import stat
import subprocess

#: Where an X server publishes one socket per display number.
X11_SOCKET_DIR = '/tmp/.X11-unix'

#: The variables that decide which platform Qt connects to.  `QT_QPA_PLATFORM`
#: is deliberately absent: it is the user's own override, and a repair must not
#: undo a decision somebody made on purpose.
SESSION_VARIABLES = ('XDG_SESSION_TYPE', 'WAYLAND_DISPLAY', 'DISPLAY')

WAYLAND = 'wayland'
X11 = 'x11'

#: How long the start-up probe waits for an X server to answer its connection
#: setup.  A server that cannot answer in two seconds cannot serve a tray
#: either, and this runs on the way into `QApplication`.
X11_PROBE_TIMEOUT = 2.0

#: The auth protocol every local X11 login uses for its cookie.
COOKIE_NAME = b'MIT-MAGIC-COOKIE-1'

#: Xauthority's "any host" family: what `xauth add :0 ...` writes when it stores
#: a cookie for the machine rather than for one hostname.
FAMILY_WILD = 0xffff

#: The three answers an X server can give to a connection setup request.
SETUP_FAILED = 0        # the server refused the connection outright
SETUP_SUCCESS = 1       # accepted; Qt's xcb plugin will be accepted as well
SETUP_AUTHENTICATE = 2  # the cookie we sent was missing or wrong


def _is_socket(path):
    """ True when `path` is a unix socket -- what "listening" looks like on disk. """
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _runtime_dir(env):
    return (env.get('XDG_RUNTIME_DIR') or '').strip()


def x11_socket_path(display):
    """ Return the socket path an X11 `DISPLAY` names, or '' when it names none.

        Only the local form can be checked this way: `:0` and `:0.0` are
        `/tmp/.X11-unix/X0`, while `host:0` reaches a server over TCP and has no
        socket here at all -- treating that as "not listening" is right, because
        a tray applet on one host for another host's session is not a case this
        unit can serve.
    """
    host, _, rest = (display or '').partition(':')
    if host or not rest:
        return ''
    number = rest.split('.')[0]
    return os.path.join(X11_SOCKET_DIR, f'X{number}') if number.isdigit() else ''


def live_wayland(env=None):
    """ Return `$WAYLAND_DISPLAY` when its socket is listening, else ''. """
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
    """ Return `$DISPLAY` when the X socket it names is listening, else ''. """
    env = os.environ if env is None else env
    display = (env.get('DISPLAY') or '').strip()
    path = x11_socket_path(display)
    return display if path and _is_socket(path) else ''


def _named_kind(env, wayland, display):
    """ Return the server type the environment points at, '' when none is live.

        Both sockets exist whenever Xwayland is up -- on this machine an niri
        login has `wayland-1` *and* `:1` -- so the pair alone cannot say which
        one is the session.  The marker can, and it is only trusted when it
        names a server that is actually listening; otherwise the live socket
        wins, which is what makes an ended session's snapshot harmless.
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
    """ Return `'wayland'`, `'x11'`, or '' when no display server is listening.

        Judged from the sockets, with `XDG_SESSION_TYPE` trusted only when both
        are live -- which is what an Xwayland login looks like -- and `loginctl`
        never asked, so this answers from an environment alone.  That is what
        `service.render()` needs to decide which display variable a unit should
        pin, and it is deliberately stricter than trusting the marker: a
        snapshot naming a server that is not listening is a snapshot of a
        session that has ended.
    """
    env = os.environ if env is None else env
    return _named_kind(env, live_wayland(env), live_x11(env))


def _loginctl(*args):
    """ Run one `loginctl` query and return its stripped output, '' on any error. """
    try:
        done = subprocess.run(('loginctl', *args), capture_output=True, text=True,
                              check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (done.stdout or '').strip() if done.returncode == 0 else ''


def newest_wayland_socket(env=None):
    """ Return the newest `wayland-*` socket in the runtime directory, or ''.

        Wayland publishes no equivalent of X11's `Display=`, and the name
        depends on what else is running: a compositor takes the first free
        number, so a bare one is `wayland-0` while niri with Xwayland started
        first is `wayland-1`.  The directory is read rather than the name
        guessed.
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
    """ Ask logind about the user's session: `(kind, wayland_name, display)`. """
    user = (env.get('USER') or env.get('LOGNAME') or '').strip()
    session = query('show-user', user, '-p', 'Display', '--value') if user else ''
    if not session:
        # A manager that imported nothing may still have been given the id.
        session = (env.get('XDG_SESSION_ID') or '').strip()
    if not session:
        return '', '', ''
    kind = query('show-session', session, '-p', 'Type', '--value').strip().lower()
    if kind == WAYLAND:
        return WAYLAND, newest_wayland_socket(env), ''
    if kind == X11:
        display = query('show-session', session, '-p', 'Display', '--value').strip()
        # `Display=` is the X display; logind leaves it out once the session is
        # closing, and `:0` is then the only useful guess left.
        return X11, '', display or ':0'
    return '', '', ''


def session_values(env=None, query=None):
    """ Return the values the session variables should have, or None if unknown.

        A name mapped to `None` means "this session has no such thing, remove
        it": a `WAYLAND_DISPLAY` a unit pinned must not survive into an X11
        login, because Qt would then try a socket nothing is listening on.  A
        live second display (`DISPLAY=:1` next to a Wayland socket) is kept,
        since it is the Xwayland a client may legitimately want.
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
    """ Point the session variables at the display server that is listening.

        Returns the applied changes as `(name, old, new)` tuples -- `new` is
        `None` when the variable was removed -- so the caller can write down
        what it corrected.  An environment that already agrees is left alone,
        which is the ordinary case for a session started from a terminal or
        from a desktop autostart entry: those inherit the display from the
        session, and only a unit has to be repaired.
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
    """ Return the Xauthority file Qt will read, or '' when none is named.

        `$XAUTHORITY` first -- sddm, gdm and lightdm each write a per-session
        file under `/tmp` or `/run/user` and export its path -- and then
        `~/.Xauthority`, which is where `xauth` writes by default and what
        libxcb falls back to when the variable is absent.
    """
    env = os.environ if env is None else env
    named = (env.get('XAUTHORITY') or '').strip()
    if named:
        return named
    home = (env.get('HOME') or '').strip()
    return os.path.join(home, '.Xauthority') if home else ''


def _field(blob, at):
    """ Read one length-prefixed Xauthority field: `CARD16` length, then data.

        There is no padding: the four-byte alignment people remember belongs to
        the X11 *connection setup* on the wire, not to this file.  `xauth add :0
        . <cookie>` writes `CARD16 family, CARD16 address_length, address,
        CARD16 number_length, number, CARD16 name_length, name, CARD16
        data_length, data` straight through.
    """
    if at + 2 > len(blob):
        raise ValueError('truncated')
    length = int.from_bytes(blob[at:at + 2], 'big')
    start = at + 2
    if start + length > len(blob):
        raise ValueError('truncated')
    return blob[start:start + length], start + length


def _authority_records(blob):
    """ Yield `(family, number, name, cookie)` from an Xauthority file's bytes.

        The format is a flat sequence of records with no header and no count, so
        the only way to read it is to walk it.  The address is skipped: it is
        only meaningful for `FamilyLocal`, and a cookie stored for another
        hostname simply fails at the server, which is the answer the probe
        reports anyway.
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
    """ Return `(cookie, known)` for `display`: the bytes to send, and whether
        "there were none" is a fact rather than a guess.

        The cookie libxcb would send is the one whose Xauthority record names
        this display number, or an `FamilyWild` record, and whose name is
        `MIT-MAGIC-COOKIE-1`.  `known` is False when the file could not be read
        or parsed at all: then "no cookie found" says nothing about what Qt will
        send, and the caller must not turn it into a refusal.
    """
    path = authority_file(env)
    if not path:
        return b'', True
    try:
        with open(path, 'rb') as handle:
            blob = handle.read()
    except FileNotFoundError:
        return b'', True       # an empty file and no file are the same to libxcb
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
    """ Return the answer an X server gives to a connection setup request.

        This is the whole of what libxcb does before Qt declares the display
        unusable: a 12-byte header carrying the protocol version and the
        lengths of the auth name and data, then the two padded blobs.  The
        reply's first byte is the status -- accepted, refused, or "authenticate
        again with a cookie you do not have".
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


def display_problem(env=None):
    """ Return why Qt could not use this session's display, '' when it can.

        Called by `main()` after `repair_environment()` and before
        `QApplication` exists, because the alternative is Qt's own ending: a
        fatal "no Qt platform plugin could be initialized", an `abort()` and a
        core dump -- which is what an X11 login produces when the unit has no
        `$XAUTHORITY` (a systemd user unit does not inherit the display *or* its
        cookie, and only the session's own
        `dbus-update-activation-environment` gives them to the manager).

        Only positive evidence is reported: a server that cannot be reached,
        refuses the connection, or asks for a cookie the readable authority file
        does not contain.  Anything the probe cannot judge -- a Wayland session,
        a TCP display, an authority file it failed to parse -- is left to Qt,
        so a host that authenticates some other way is never refused a start.
    """
    env = os.environ if env is None else env
    if (env.get('XDG_SESSION_TYPE') or '').strip().lower() != X11:
        return ''
    display = (env.get('DISPLAY') or '').strip()
    path = x11_socket_path(display)
    if not path:
        return ''
    cookie, known = authority_cookie(display, env)
    try:
        status = x11_setup_status(path, cookie)
    except OSError as error:
        # libxcb also tries the *abstract* socket for the same name (Linux
        # publishes it there next to the file), so a path that cannot be opened
        # is not a verdict on its own.
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
            return ''       # our reading of the authority file is in doubt
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
