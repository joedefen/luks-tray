"""`Session`: which display server is listening, and the variables that follow.

A unit's `Environment=` lines are a snapshot of the login that ran
`--svc-install`, and a unit's own value beats what the session exports, so the
snapshot is what Qt reads -- in whichever session the unit is started next.
These tests pin down the two properties that make a switch harmless: a snapshot
of an *ended* session is replaced, and an environment that already names a
listening server is left exactly as it is.

The second half covers `display_problem()`, the probe `main()` runs before
`QApplication` exists: a display that answers "authenticate" is the case that
used to end in Qt's `abort()` and a core dump, so it is worth pinning down which
answers count as a refusal and which are left to Qt.
"""
import os
import socket
import threading

import pytest

from luks_tray import Session

#: One MIT-MAGIC-COOKIE-1 payload, as an Xauthority record carries it.
COOKIE = bytes.fromhex('9d200ef0aa1d70dc0d4effc19dd122d6')
MIT_MAGIC_COOKIE = b'MIT-MAGIC-COOKIE-1'


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


def authority_record(path, cookie, number=b'0', family=0xffff, address=b'',
                     name=MIT_MAGIC_COOKIE):
    """Write one Xauthority record, in the format `xauth add` writes.

        `xauth add :0 . <cookie>` stores `CARD16 family` and then four
        length-prefixed fields -- address, number, name, cookie -- with no
        padding (the four-byte alignment belongs to the wire protocol, not to
        this file).  `family=0xffff` is the `#ffff##:0` record `xauth` writes
        for "any host".
    """
    def field(value):
        return len(value).to_bytes(2, 'big') + value
    path.write_bytes(family.to_bytes(2, 'big') + field(address) + field(number)
                     + field(name) + field(cookie))
    return path


def read_setup(conn):
    """Read one X11 connection setup request: `(auth name, auth data)`."""
    head = b''
    while len(head) < 12:
        chunk = conn.recv(64)
        if not chunk:
            return b'', b''
        head += chunk
    name_length = int.from_bytes(head[6:8], 'little')
    data_length = int.from_bytes(head[8:10], 'little')
    body = head[12:]
    at = -name_length % 4 + name_length
    while len(body) < at + -data_length % 4 + data_length:
        chunk = conn.recv(64)
        if not chunk:
            break
        body += chunk
    return body[:name_length], body[at:at + data_length]


class FakeXServer:
    """A unix socket that answers one connection setup with `status`.

        The probe is only useful if it sends what libxcb sends, so the fake
        server reads the request properly instead of assuming a shape: `auth`
        is the `(name, data)` pair the applet offered.
    """

    def __init__(self, path, status):
        self.status = status
        self.auth = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.socket.listen(1)
        self.thread = threading.Thread(target=self._answer, daemon=True)
        self.thread.start()

    def _answer(self):
        with self.socket:
            conn, _address = self.socket.accept()
            with conn:
                self.auth = read_setup(conn)
                conn.sendall(bytes([self.status]) + b'\x00' * 7)

    def done(self):
        """Wait for the answer to be sent and return self, for the assertions."""
        self.thread.join(5)
        assert not self.thread.is_alive(), 'the probe never connected'
        return self


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


# --- which display server is live ------------------------------------------


def test_display_server_names_the_live_compositor(runtime, x11_dir):
    listen(runtime / 'wayland-1')
    assert Session.display_server(env_for(runtime, WAYLAND_DISPLAY='wayland-1',
                                          XDG_SESSION_TYPE='wayland')) == 'wayland'


def test_display_server_names_the_live_x_server(runtime, x11_dir):
    listen(x11_dir / 'X0')
    assert Session.display_server(env_for(runtime, DISPLAY=':0')) == 'x11'


def test_display_server_answers_nothing_when_nothing_listens(runtime, x11_dir):
    # A unit rendered over ssh gets '' and pins no display at all; the applet
    # asks the machine at startup instead.  The marker alone is not enough.
    assert Session.display_server(env_for(runtime, XDG_SESSION_TYPE='wayland',
                                          WAYLAND_DISPLAY='wayland-1')) == ''


def test_display_server_uses_the_marker_only_when_both_are_live(runtime, x11_dir):
    listen(runtime / 'wayland-1')
    listen(x11_dir / 'X1')
    both = env_for(runtime, WAYLAND_DISPLAY='wayland-1', DISPLAY=':1')
    assert Session.display_server(both) == 'wayland'
    assert Session.display_server({**both, 'XDG_SESSION_TYPE': 'x11'}) == 'x11'


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


# --- the start-up probe: may Qt actually use this display? ------------------


def x11_env(**values):
    """The environment of a unit that landed in an X11 session."""
    environment = {'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':0'}
    environment.update(values)
    return environment


def test_the_authority_file_is_xauthority_first_then_the_home_default(tmp_path):
    assert Session.authority_file({'XAUTHORITY': '/run/user/1000/xauth'}) == (
        '/run/user/1000/xauth')
    assert Session.authority_file({'HOME': str(tmp_path)}) == str(
        tmp_path / '.Xauthority')
    assert Session.authority_file({}) == ''


def test_the_cookie_of_the_display_is_what_the_probe_offers(x11_dir, tmp_path):
    # The one thing that must match libxcb: the record for this display number,
    # sent as MIT-MAGIC-COOKIE-1, or the server answers "authenticate" and Qt
    # calls it "could not connect to display".
    file = authority_record(tmp_path / 'auth', COOKIE)
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_SUCCESS)
    assert Session.display_problem(x11_env(XAUTHORITY=str(file))) == ''
    assert server.done().auth == (MIT_MAGIC_COOKIE, COOKIE)


def test_a_wild_family_record_matches_any_display(x11_dir, tmp_path):
    # `#ffff##:0` is the record `xauth` writes for "any host"; its number is
    # ignored by libxcb, so the probe must ignore it as well.
    file = authority_record(tmp_path / 'auth', COOKIE, number=b'7',
                            family=Session.FAMILY_WILD)
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_SUCCESS)
    assert Session.display_problem(x11_env(XAUTHORITY=str(file))) == ''
    assert server.done().auth == (MIT_MAGIC_COOKIE, COOKIE)


def test_a_cookie_for_another_display_is_not_offered(x11_dir, tmp_path):
    file = authority_record(tmp_path / 'auth', COOKIE, number=b'1',
                            family=0x0100, address=b'ofc')
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_AUTHENTICATE)
    problem = Session.display_problem(x11_env(XAUTHORITY=str(file)))
    assert server.done().auth == (MIT_MAGIC_COOKIE, b'')
    assert 'XAUTHORITY' in problem


def test_a_refusal_with_no_cookie_is_reported_as_the_reason(x11_dir, tmp_path):
    # The measured login: no `$XAUTHORITY` in the unit's environment, an empty
    # `~/.Xauthority`, and a server that wants a cookie.  Qt aborted here.
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_AUTHENTICATE)
    problem = Session.display_problem(
        x11_env(HOME=str(tmp_path), XAUTHORITY=str(tmp_path / 'none')))
    assert server.done().auth == (MIT_MAGIC_COOKIE, b'')
    assert 'could not connect to display' in problem
    assert str(tmp_path / 'none') in problem


def test_a_refused_connection_is_reported(x11_dir):
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_FAILED)
    problem = Session.display_problem(x11_env())
    server.done()
    assert 'refused the connection' in problem


def test_a_socket_that_is_not_there_is_reported(x11_dir):
    # No server at all: Qt would abort the same way, and this is the login race
    # the unit's `Restart=on-failure` is expected to retry.
    problem = Session.display_problem(x11_env())
    assert 'cannot be reached' in problem


def test_a_stale_socket_from_an_ended_session_is_reported(x11_dir):
    listen(x11_dir / 'X0')      # the file is there, nothing accepts on it
    assert 'cannot be reached' in Session.display_problem(x11_env())


def test_an_authority_file_it_cannot_parse_is_not_a_verdict(x11_dir, tmp_path):
    # A file this module failed to read says nothing about what Qt will send,
    # so the probe must not refuse a start on the strength of it.
    broken = tmp_path / 'auth'
    broken.write_bytes(b'\x01\x00\x00')
    server = FakeXServer(x11_dir / 'X0', Session.SETUP_AUTHENTICATE)
    assert Session.display_problem(x11_env(XAUTHORITY=str(broken))) == ''
    server.done()


def test_a_wayland_session_is_never_probed(runtime, x11_dir):
    # Nothing authenticates over a compositor socket, and DISPLAY may still name
    # the Xwayland Qt is not using.
    listen(x11_dir / 'X0')
    assert Session.display_problem({'XDG_SESSION_TYPE': 'wayland',
                                    'DISPLAY': ':0'}) == ''


def test_a_tcp_display_is_left_to_qt(x11_dir):
    # `host:0` has no socket here; refusing it would be a guess.
    assert Session.display_problem({'XDG_SESSION_TYPE': 'x11',
                                    'DISPLAY': 'host:0'}) == ''
