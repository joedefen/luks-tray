#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Which display server this login is really on, and the variables for it.

    `systemd --user` cannot hand a unit the session's display, so
    [`service.render()`](luks_tray/service.py:277) copies `WAYLAND_DISPLAY` (or
    `DISPLAY`) and the desktop name out of the installing shell and into the
    unit file.  That copy is a *snapshot of one login*: log out of niri and into
    Plasma on X11, and the unit still says `XDG_SESSION_TYPE=wayland`,
    `WAYLAND_DISPLAY=wayland-1`.  A unit's own `Environment=` wins over what the
    session exports, so Qt reads the snapshot and either dies ("could not
    connect to display") or lands on a platform whose tray host is not there.
    Until now the remedy was to remember `--svc-install` after every switch.

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
"""
import os
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
