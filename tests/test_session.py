"""`Session`: which display server is listening, and the variables that follow.

A unit's `Environment=` lines are a snapshot of the login that ran
`--svc-install`, and a unit's own value beats what the session exports, so the
snapshot is what Qt reads -- in whichever session the unit is started next.
These tests pin down the two properties that make a switch harmless: a snapshot
of an *ended* session is replaced, and an environment that already names a
listening server is left exactly as it is.
"""
import os
import socket

import pytest

from luks_tray import Session


@pytest.fixture
def runtime(tmp_path):
    """An empty `XDG_RUNTIME_DIR`, where a compositor would publish a socket."""
    path = tmp_path / 'run'
    path.mkdir()
    return path


@pytest.fixture
def x11_dir(tmp_path, monkeypatch):
    """A private `/tmp/.X11-unix`, so this machine's X server cannot answer."""
    path = tmp_path / 'X11-unix'
    path.mkdir()
    monkeypatch.setattr(Session, 'X11_SOCKET_DIR', str(path))
    return path


def listen(path):
    """Create a unix socket at `path`: what a listening server leaves behind."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.close()
    return path


def env_for(runtime, **values):
    """The environment of a unit started in a user manager with this runtime."""
    environment = {'XDG_RUNTIME_DIR': str(runtime), 'USER': 'joe'}
    environment.update(values)
    return environment


def loginctl(answers):
    """Answer `loginctl` from a dict keyed by the joined arguments."""
    def query(*args):
        return answers.get(' '.join(args), '')
    return query


def loginctl_unused(*args):
    raise AssertionError(f'loginctl was asked {args} about a live display')


# --- parsing a DISPLAY value ----------------------------------------------


def test_x11_socket_path_reads_the_local_display_form(x11_dir):
    assert Session.x11_socket_path(':0') == str(x11_dir / 'X0')
    assert Session.x11_socket_path(':0.0') == str(x11_dir / 'X0')
    assert Session.x11_socket_path(':12') == str(x11_dir / 'X12')


def test_x11_socket_path_refuses_a_display_it_cannot_check(x11_dir):
    # A TCP display has no socket here, and a "tray for another host" is not a
    # case a user unit can serve: both are "nothing is listening".
    assert Session.x11_socket_path('host:0') == ''
    assert Session.x11_socket_path('') == ''
    assert Session.x11_socket_path('nonsense') == ''


# --- an environment that is already right ----------------------------------


def test_a_live_compositor_is_left_alone(runtime, x11_dir):
    listen(runtime / 'wayland-1')
    environment = env_for(runtime, XDG_SESSION_TYPE='wayland',
                          WAYLAND_DISPLAY='wayland-1')
    assert Session.repair_environment(env=environment, query=loginctl_unused) == []
    assert environment['XDG_SESSION_TYPE'] == 'wayland'


def test_an_xwayland_display_is_kept_next_to_a_live_compositor(runtime, x11_dir):
    # An niri login has both `wayland-1` and `:1`; the marker says which of the
    # two is the session, and the other is a display a client may still want.
    listen(runtime / 'wayland-1')
    listen(x11_dir / 'X1')
    environment = env_for(runtime, XDG_SESSION_TYPE='wayland',
                          WAYLAND_DISPLAY='wayland-1', DISPLAY=':1')
    assert Session.repair_environment(env=environment, query=loginctl_unused) == []
    assert environment['DISPLAY'] == ':1'


# --- a snapshot of a session that has ended --------------------------------


def test_a_wayland_snapshot_yields_to_the_live_x_server(runtime, x11_dir):
    # The unit was installed from niri, the user logged into Plasma on X11:
    # `wayland-1` is a socket nobody listens on any more, `:0` is the session.
    listen(x11_dir / 'X0')
    environment = env_for(runtime, XDG_SESSION_TYPE='wayland',
                          WAYLAND_DISPLAY='wayland-1', DISPLAY=':0')
    changes = Session.repair_environment(env=environment, query=loginctl_unused)
    assert environment['XDG_SESSION_TYPE'] == 'x11'
    assert environment['DISPLAY'] == ':0'
    assert 'WAYLAND_DISPLAY' not in environment
    assert ('XDG_SESSION_TYPE', 'wayland', 'x11') in changes
    assert ('WAYLAND_DISPLAY', 'wayland-1', None) in changes


def test_an_x11_snapshot_yields_to_the_live_compositor(runtime, x11_dir):
    # The other direction: `DISPLAY=:0` is gone, and Qt must not be told the
    # session is X11 just because the unit file says so.
    listen(runtime / 'wayland-1')
    environment = env_for(runtime, XDG_SESSION_TYPE='x11', DISPLAY=':0',
                          WAYLAND_DISPLAY='wayland-1')
    Session.repair_environment(env=environment, query=loginctl_unused)
    assert environment['XDG_SESSION_TYPE'] == 'wayland'
    assert environment['WAYLAND_DISPLAY'] == 'wayland-1'
    assert 'DISPLAY' not in environment


def test_the_live_compositor_wins_when_no_marker_is_set(runtime, x11_dir):
    listen(runtime / 'wayland-1')
    listen(x11_dir / 'X1')
    environment = env_for(runtime, WAYLAND_DISPLAY='wayland-1', DISPLAY=':1')
    Session.repair_environment(env=environment, query=loginctl_unused)
    assert environment['XDG_SESSION_TYPE'] == 'wayland'


# --- nothing in the environment is usable ----------------------------------


def test_loginctl_supplies_an_x11_session_the_environment_lost(runtime, x11_dir):
    listen(x11_dir / 'X0')
    environment = env_for(runtime)
    Session.repair_environment(env=environment, query=loginctl({
        'show-user joe -p Display --value': '7',
        'show-session 7 -p Type --value': 'x11',
        'show-session 7 -p Display --value': ':0',
    }))
    assert environment['XDG_SESSION_TYPE'] == 'x11'
    assert environment['DISPLAY'] == ':0'
    assert 'WAYLAND_DISPLAY' not in environment


def test_loginctl_names_a_wayland_session_and_its_socket_is_found(runtime, x11_dir):
    older = listen(runtime / 'wayland-0')
    newer = listen(runtime / 'wayland-1')
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    environment = env_for(runtime)
    Session.repair_environment(env=environment, query=loginctl({
        'show-user joe -p Display --value': '4',
        'show-session 4 -p Type --value': 'wayland',
    }))
    assert environment['XDG_SESSION_TYPE'] == 'wayland'
    assert environment['WAYLAND_DISPLAY'] == 'wayland-1'


def test_a_session_id_from_the_environment_is_the_fallback(runtime, x11_dir):
    environment = env_for(runtime, XDG_SESSION_ID='9')
    Session.repair_environment(env=environment, query=loginctl({
        'show-session 9 -p Type --value': 'x11',
    }))
    assert environment['XDG_SESSION_TYPE'] == 'x11'
    # logind leaves `Display=` out for a session that is closing; `:0` is the
    # only useful guess left, and it is better than handing Qt nothing.
    assert environment['DISPLAY'] == ':0'


def test_nothing_listening_and_no_session_is_reported_not_guessed(runtime, x11_dir):
    environment = env_for(runtime, XDG_SESSION_TYPE='wayland',
                          WAYLAND_DISPLAY='wayland-1')
    notes = []
    changes = Session.repair_environment(env=environment, query=loginctl({}),
                                         note=notes.append)
    assert changes == []
    assert environment['XDG_SESSION_TYPE'] == 'wayland'
    assert environment['WAYLAND_DISPLAY'] == 'wayland-1'
    assert any('no display server' in note for note in notes)


def test_a_repair_reports_the_value_it_replaced(runtime, x11_dir):
    listen(x11_dir / 'X0')
    environment = env_for(runtime, XDG_SESSION_TYPE='wayland', DISPLAY=':0')
    changes = Session.repair_environment(env=environment, query=loginctl_unused)
    assert changes == [('XDG_SESSION_TYPE', 'wayland', 'x11')]
