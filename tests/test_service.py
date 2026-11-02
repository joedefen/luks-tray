"""`service`: the shipped units, the renderer, and the `--svc-*` verbs.

The plan/execute split is what makes these tests possible without a systemd
session: every verb is asserted on the *steps* it builds, with the subprocess
call injected, and only `execute()` and the two end-to-end `dispatch()` cases
ever touch a file.
"""
import os
from pathlib import Path

import pytest

from luks_tray import service

UNIT_TEXT = (
    '[Unit]\n'
    'Description=test\n'
    '\n'
    '[Service]\n'
    'ExecStart=%h/.local/bin/luks-tray\n'
    'Restart=on-failure\n'
    '\n'
    '[Install]\n'
    'WantedBy=graphical-session.target\n'
)


@pytest.fixture
def env(isolated_home):
    """A Wayland session's environment, pointing at the test's private home."""
    return {
        'HOME': str(isolated_home),
        'XDG_CONFIG_HOME': str(isolated_home / '.config'),
        'XDG_SESSION_TYPE': 'wayland',
        'WAYLAND_DISPLAY': 'wayland-1',
        'XDG_CURRENT_DESKTOP': 'niri',
    }


@pytest.fixture
def launcher(isolated_home):
    """`~/.local/bin/luks-tray`, executable, as every install method writes it."""
    path = isolated_home / '.local' / 'bin' / 'luks-tray'
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


class FakeRun:
    """Answer `systemctl --user` the way a unit in a given state would."""

    def __init__(self, enabled=False, active=False, failed=False, failing=()):
        self.calls = []
        self.answers = {
            'is-enabled': 0 if enabled else 1,
            'is-active': 0 if active else 1,
            'is-failed': 0 if failed else 1,
        }
        self.failing = set(failing)

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        verb = argv[2] if len(argv) > 2 else ''
        if verb in self.failing:
            return 1, 'boom'
        return self.answers.get(verb, 0), ''

    def verbs(self):
        return [call[2] for call in self.calls]


def make_status(path=None, installed=True, enabled=True, active=False,
                up_to_date=True, failed=False):
    """A `Status` for the installed unit, unless a test names another path."""
    return service.Status(path=Path(path) if path else service.installed_unit(),
                          installed=installed, enabled=enabled, active=active,
                          up_to_date=up_to_date, failed=failed)


def install_unit(env, text, name=None):
    """Put `text` where `--svc-install` would put it; return the path."""
    path = service.installed_unit(env) if name is None else Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


# -- the shipped units -----------------------------------------------------

def test_both_sessions_ship_a_unit():
    assert sorted(service.templates()) == ['wayland', 'x11']


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_runs_the_launcher(session, env):
    text = service.read_unit(service.template_for(session))
    assert service.installed_executable(text, env) == str(service.launcher_path(env))


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_pins_its_session_type(session):
    text = service.read_unit(service.template_for(session))
    assert f'Environment=XDG_SESSION_TYPE={session}' in text


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_keeps_the_sbin_directories_on_path(session):
    # `cryptsetup`, `losetup`, `mkfs.ext4` and `blkid` live in /usr/sbin on a
    # Debian-style or non-usrmerged host, and check_dependencies() looks them
    # up with `shutil.which()`.  A unit that pinned PATH without sbin made the
    # applet exit at startup with "missing system commands" while the same
    # shell found every one of them.
    text = service.read_unit(service.template_for(session))
    path_line = next(line for line in text.splitlines()
                     if line.startswith('Environment=PATH='))
    directories = path_line.split('=', 1)[1].split(':')
    assert '/usr/local/sbin' in directories
    assert '/usr/sbin' in directories
    assert '/sbin' in directories


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_stops_restarting_on_a_lock_or_a_crash(session):
    text = service.read_unit(service.template_for(session))
    # 3 is another instance holding the lock, 15 is luks-tray's own
    # "unhandled exception" exit; retrying either just fails the same way.
    assert 'RestartPreventExitStatus=3 15' in text


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_stays_down_when_a_logout_stops_it(session):
    # `Restart=always` treated the SIGTERM that ends a logout as a crash and put
    # the applet back into a session that was waiting for its processes to exit,
    # so the logout never reached the display manager (black screen).  A stop
    # signal must therefore be final, while a crash must still be recovered.
    text = service.read_unit(service.template_for(session))
    directives = [line for line in text.splitlines() if not line.startswith('#')]
    assert 'Restart=on-failure' in directives
    # The comment above the directive names it; the directive itself must be gone.
    assert 'Restart=always' not in directives
    prevent = next(line for line in directives
                   if line.startswith('RestartPreventExitStatus='))
    assert 'SIGTERM' in prevent


@pytest.mark.parametrize('session', ['wayland', 'x11'])
def test_each_shipped_unit_asks_to_be_started_at_login(session):
    text = service.read_unit(service.template_for(session))
    assert 'PartOf=graphical-session.target' in text
    assert 'WantedBy=graphical-session.target' in text


def test_no_shipped_unit_redirects_stdout():
    # prt() points itself at debug.log; a StandardOutput= line would give
    # systemd a directory it refuses to create (EXIT_STDOUT, status 209).
    for path in service.templates().values():
        assert 'StandardOutput=' not in service.read_unit(path)


def test_template_for_names_what_ships():
    with pytest.raises(service.ServiceError) as info:
        service.template_for('tty')
    assert 'wayland' in str(info.value) and 'x11' in str(info.value)


# -- the session -----------------------------------------------------------

@pytest.mark.parametrize('value', ['wayland', 'WAYLAND', ' wayland ', 'x11', 'X11'])
def test_session_of_reads_the_display_server(value):
    assert service.session_of({'XDG_SESSION_TYPE': value}) in ('wayland', 'x11')


def test_session_of_refuses_to_guess():
    with pytest.raises(service.ServiceError) as info:
        service.session_of({})
    assert 'XDG_SESSION_TYPE' in str(info.value)


# -- render() -------------------------------------------------------------

def test_render_appends_the_session_variables_after_exec_start():
    rendered = service.render(UNIT_TEXT, {'WAYLAND_DISPLAY': 'wayland-1',
                                         'XDG_CURRENT_DESKTOP': 'niri'})
    lines = rendered.splitlines()
    at = lines.index('ExecStart=%h/.local/bin/luks-tray')
    assert lines[at + 1] == 'Environment=WAYLAND_DISPLAY=wayland-1'
    assert lines[at + 2] == 'Environment=XDG_CURRENT_DESKTOP=niri'
    assert rendered.endswith('\n')


def test_render_never_invents_an_unexported_variable():
    assert service.render(UNIT_TEXT, {}) == UNIT_TEXT


def test_render_skips_an_empty_variable():
    assert service.render(UNIT_TEXT, {'DISPLAY': '', 'WAYLAND_DISPLAY': 'wayland-0'}) == (
        UNIT_TEXT.replace('ExecStart=%h/.local/bin/luks-tray\n',
                          'ExecStart=%h/.local/bin/luks-tray\n'
                          'Environment=WAYLAND_DISPLAY=wayland-0\n'))


def test_render_refuses_a_unit_without_an_exec_start():
    with pytest.raises(service.ServiceError) as info:
        service.render('[Service]\nRestart=always\n')
    assert 'ExecStart' in str(info.value)


def test_rendered_unit_picks_the_template_for_the_session(isolated_home):
    env = {'HOME': str(isolated_home), 'XDG_CONFIG_HOME': str(isolated_home / '.config'),
           'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':1'}
    rendered = service.rendered_unit(env=env)
    assert 'Environment=XDG_SESSION_TYPE=x11' in rendered
    assert 'Environment=DISPLAY=:1' in rendered


def test_a_wayland_unit_keeps_a_leftover_display_out(env):
    # XWayland leaves DISPLAY set in a Wayland session; pinning it would follow
    # whatever X display happened to be exported when --svc-install ran.
    rendered = service.rendered_unit('wayland', {**env, 'DISPLAY': ':1'})
    assert 'Environment=WAYLAND_DISPLAY=wayland-1' in rendered
    assert 'Environment=DISPLAY=' not in rendered


def test_an_x11_unit_keeps_a_stale_wayland_display_out(isolated_home):
    env = {'HOME': str(isolated_home), 'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':0',
           'WAYLAND_DISPLAY': 'wayland-9'}
    rendered = service.rendered_unit(env=env)
    assert 'Environment=DISPLAY=:0' in rendered
    assert 'Environment=WAYLAND_DISPLAY=' not in rendered


# -- paths ---------------------------------------------------------------

def test_unit_dir_follows_xdg_config_home(isolated_home, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(isolated_home / 'cfg'))
    assert service.installed_unit() == isolated_home / 'cfg' / 'systemd' / 'user' / (
        service.UNIT_NAME)


def test_log_path_names_the_applet_log(isolated_home):
    assert service.log_path({'HOME': str(isolated_home)}) == (
        isolated_home / '.config' / 'luks-tray' / 'debug.log')


def test_launcher_problem_is_none_for_an_executable(tmp_path):
    path = tmp_path / 'luks-tray'
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)
    assert service.launcher_problem(path) is None


def test_launcher_problem_reports_a_missing_file(tmp_path):
    assert 'does not exist' in service.launcher_problem(tmp_path / 'luks-tray')


def test_launcher_problem_reports_a_dangling_symlink(tmp_path):
    link = tmp_path / 'luks-tray'
    link.symlink_to(tmp_path / 'gone')
    assert 'dangling' in service.launcher_problem(link)


def test_launcher_problem_reports_a_file_that_is_not_executable(tmp_path):
    path = tmp_path / 'luks-tray'
    path.write_text('#!/bin/sh\n')
    path.chmod(0o644)
    assert 'not executable' in service.launcher_problem(path)


def test_require_launcher_refuses_and_says_how_to_install(env):
    with pytest.raises(service.ServiceError) as info:
        service.require_launcher(env)
    assert 'pipx install' in str(info.value)


def test_require_launcher_returns_the_path(env, launcher):
    assert service.require_launcher(env) == launcher


def test_changed_lines_marks_both_sides():
    assert service.changed_lines('a\nb\n', 'a\nc\n') == ['- b', '+ c']


def test_changed_lines_summarises_a_unit_that_was_rewritten():
    old = '\n'.join(f'old{i}' for i in range(20))
    new = '\n'.join(f'new{i}' for i in range(20))
    assert service.changed_lines(old, new) == ['  (40 lines differ)']


# -- probe() -------------------------------------------------------------

def test_probe_of_an_absent_unit_runs_nothing(env):
    run = FakeRun()
    status = service.probe(env=env, run=run)
    assert status.installed is False
    assert status.path == service.installed_unit(env)
    assert run.calls == []


def test_probe_asks_systemd_about_an_installed_unit(env):
    install_unit(env, UNIT_TEXT)
    run = FakeRun(enabled=True, active=True)
    status = service.probe(env=env, run=run)
    assert run.verbs() == ['is-enabled', 'is-active', 'is-failed']
    assert (status.installed, status.enabled, status.active, status.failed) == (
        True, True, True, False)


def test_probe_compares_the_file_with_the_rendered_unit(env):
    install_unit(env, UNIT_TEXT)
    assert service.probe(rendered=UNIT_TEXT, env=env, run=FakeRun()).up_to_date is True
    assert service.probe(rendered='other\n', env=env, run=FakeRun()).up_to_date is False


# -- plan_install() ------------------------------------------------------

def test_install_writes_reloads_and_enables(env, launcher):
    plan = service.plan_install(UNIT_TEXT, make_status(installed=False, enabled=False),
                                env=env)
    assert [step.describe for step in plan.steps] == [
        f'write {service.installed_unit(env)}',
        'reload the unit files',
        f'enable {service.UNIT_NAME}',
    ]
    assert any('the unit will run' in note for note in plan.notes)


def test_install_does_not_start_the_applet(env, launcher):
    plan = service.plan_install(UNIT_TEXT, make_status(installed=False), env=env)
    assert not [step for step in plan.steps if step.argv[2:3] == ('start',)]


def test_install_is_a_no_op_when_the_unit_is_current(env, launcher):
    plan = service.plan_install(UNIT_TEXT, make_status(), env=env)
    assert plan.steps == []
    assert plan.notes == [f'{service.installed_unit(env)} is already up to date']


def test_install_prompts_before_replacing_a_different_unit(env, launcher):
    plan = service.plan_install(UNIT_TEXT, make_status(up_to_date=False), env=env)
    assert 'replace it?' in plan.prompt
    assert plan.steps[0].write == (service.installed_unit(env), UNIT_TEXT)


def test_install_clears_a_failed_state(env, launcher):
    plan = service.plan_install(UNIT_TEXT.replace('Restart=on-failure\n', ''),
                                make_status(failed=True), env=env)
    assert f'clear the failed state of {service.UNIT_NAME}' in [
        step.describe for step in plan.steps]


def test_install_notes_an_instance_running_outside_systemd(env, launcher):
    plan = service.plan_install(UNIT_TEXT, make_status(installed=False), holder=4242,
                                env=env)
    assert any('pid 4242' in note and 'next login' in note for note in plan.notes)


# -- plan_start() --------------------------------------------------------

def test_start_refuses_when_nothing_is_installed():
    with pytest.raises(service.ServiceError) as info:
        service.plan_start(UNIT_TEXT, make_status(installed=False))
    assert '--svc-install' in str(info.value)


def test_start_enables_a_disabled_unit(env, launcher):
    status = make_status(path=install_unit(env, UNIT_TEXT), enabled=False)
    plan = service.plan_start(UNIT_TEXT, status, env=env)
    assert f'enable {service.UNIT_NAME}' in [step.describe for step in plan.steps]
    assert any('next login' in note for note in plan.notes)


def test_start_notes_an_already_running_unit(env, launcher):
    status = make_status(path=install_unit(env, UNIT_TEXT), active=True)
    plan = service.plan_start(UNIT_TEXT, status, env=env)
    assert plan.steps == []
    assert plan.notes == [f'{service.UNIT_NAME} is already running']


def test_start_refuses_an_instance_that_systemd_does_not_own(env, launcher):
    status = make_status(path=install_unit(env, UNIT_TEXT))
    with pytest.raises(service.ServiceError) as info:
        service.plan_start(UNIT_TEXT, status, holder=4242, env=env)
    assert 'exit 3' in str(info.value)


def test_start_refuses_a_unit_that_runs_something_else(env, launcher):
    moved = UNIT_TEXT.replace('%h/.local/bin/luks-tray', '/opt/other/luks-tray')
    status = make_status(path=install_unit(env, moved))
    with pytest.raises(service.ServiceError) as info:
        service.plan_start(UNIT_TEXT, status, env=env)
    assert '--svc-install' in str(info.value)


def test_start_points_at_install_when_the_unit_is_stale(env, launcher):
    status = make_status(path=install_unit(env, UNIT_TEXT), up_to_date=False)
    plan = service.plan_start(UNIT_TEXT, status, env=env)
    assert any('differs from the shipped unit' in note for note in plan.notes)


# -- plan_restart() ------------------------------------------------------

def test_restart_restarts_the_unit_and_says_which_code_runs(env, launcher):
    plan = service.plan_restart(UNIT_TEXT, make_status(), env=env)
    assert [step.argv for step in plan.steps] == [service.systemctl('restart', service.UNIT_NAME)]
    assert any(str(launcher) in note for note in plan.notes)


def test_restart_notes_a_unit_that_will_not_come_back(env, launcher):
    plan = service.plan_restart(UNIT_TEXT, make_status(enabled=False), env=env)
    assert any('not enabled' in note for note in plan.notes)


def test_restart_clears_a_failed_state_first(env, launcher):
    plan = service.plan_restart(UNIT_TEXT, make_status(failed=True), env=env)
    assert [step.argv[2] for step in plan.steps] == ['reset-failed', 'restart']


# -- plan_stop() and plan_uninstall() ------------------------------------

def test_stop_refuses_when_nothing_is_installed():
    with pytest.raises(service.ServiceError) as info:
        service.plan_stop(make_status(installed=False))
    assert 'nothing to stop' in str(info.value)


def test_stop_keeps_the_unit_enabled(env, launcher):
    plan = service.plan_stop(make_status(path=install_unit(env, UNIT_TEXT), active=True))
    assert [step.argv for step in plan.steps] == [service.systemctl('stop', service.UNIT_NAME)]
    assert any('stays enabled' in note for note in plan.notes)


def test_stop_of_a_stopped_unit_does_nothing():
    plan = service.plan_stop(make_status(active=False))
    assert plan.steps == []
    assert plan.notes == [f'{service.UNIT_NAME} is not running']


def test_uninstall_stops_disables_and_removes(env, launcher):
    path = install_unit(env, UNIT_TEXT)
    plan = service.plan_uninstall(make_status(path=path, active=True))
    assert [step.argv[2] for step in plan.steps if step.argv] == [
        'stop', 'disable', 'daemon-reload']
    assert plan.steps[-2].remove == path
    assert any('left in place' in note for note in plan.notes)


def test_uninstall_refuses_a_unit_it_did_not_write(env, launcher):
    path = install_unit(env, UNIT_TEXT)
    with pytest.raises(service.ServiceError) as info:
        service.plan_uninstall(make_status(path=path, up_to_date=False))
    assert '--force' in str(info.value)


def test_uninstall_with_force_deletes_an_edited_unit(env, launcher):
    path = install_unit(env, UNIT_TEXT)
    plan = service.plan_uninstall(make_status(path=path, up_to_date=False), force=True)
    assert plan.steps[-2].remove == path


def test_uninstall_of_nothing_does_nothing():
    plan = service.plan_uninstall(make_status(installed=False))
    assert plan.steps == []
    assert plan.notes == [f'{service.UNIT_NAME} is not installed; nothing to do']


# -- execute() -----------------------------------------------------------

def test_execute_performs_every_kind_of_step(tmp_path):
    target = tmp_path / 'unit' / 'luks-tray.service'
    removable = tmp_path / 'gone.service'
    removable.write_text('bye')
    run = FakeRun()
    plan = service.Plan(steps=[
        service.Step('write the unit', write=(target, 'hello\n')),
        service.Step('make the folder', mkdir=tmp_path / 'logs'),
        service.Step('remove the unit', remove=removable),
        service.Step('reload the unit files', argv=service.systemctl('daemon-reload')),
    ])
    service.execute(plan, run=run)
    assert target.read_text(encoding='utf-8') == 'hello\n'
    assert (tmp_path / 'logs').is_dir()
    assert not removable.exists()
    assert run.calls == [('systemctl', '--user', 'daemon-reload')]


def test_execute_reports_a_failed_command_with_its_hint():
    plan = service.Plan(steps=[service.Step(
        'start luks-tray.service',
        argv=service.systemctl('start', service.UNIT_NAME),
        hint='section   journalctl --user -xeu luks-tray.service')])
    with pytest.raises(service.ServiceError) as info:
        service.execute(plan, run=FakeRun(failing={'start'}))
    assert 'start luks-tray.service failed (exit 1): boom' in str(info.value)
    assert 'journalctl --user -xeu luks-tray.service' in str(info.value)


# -- applet_holder(), unit_in_cgroup() and detached_restart() -------------

def test_applet_holder_is_none_without_a_lock_file(tmp_path):
    assert service.applet_holder(tmp_path) is None


def test_applet_holder_reports_the_pid_that_holds_the_lock(tmp_path, monkeypatch):
    from luks_tray.Utils import SingleInstance

    held = SingleInstance(str(tmp_path / 'luks-tray.lock'))
    monkeypatch.setattr(SingleInstance, 'current', None)
    assert held.acquire() is True
    try:
        assert service.applet_holder(tmp_path) == os.getpid()
    finally:
        held.release()


def test_applet_holder_ignores_a_stale_pid_file(tmp_path):
    (tmp_path / 'luks-tray.lock').write_text('999999\n')
    assert service.applet_holder(tmp_path) is None


def test_unit_in_cgroup_finds_the_deepest_service():
    text = '0::/user.slice/user-1000.slice/user@1000.service/app.slice/luks-tray.service\n'
    assert service.unit_in_cgroup(text) == 'luks-tray.service'


def test_unit_in_cgroup_ignores_the_user_manager_itself():
    text = ('0::/user.slice/user-1000.slice/user@1000.service/app.slice/'
            'vte-spawn-1.scope\n')
    assert service.unit_in_cgroup(text) is None


def test_supervised_by_is_none_for_a_process_that_does_not_exist():
    assert service.supervised_by(999999999) is None


def test_supervised_here_is_false_for_this_test_process():
    # The heuristic `INVOCATION_ID` is not enough: a terminal started under a
    # systemd unit inherits it, so the cgroup is what decides.
    assert service.supervised_here() is False


def test_detached_restart_refuses_outside_a_unit(monkeypatch):
    monkeypatch.setattr(service, 'supervised_here', lambda: False)
    with pytest.raises(service.ServiceError) as info:
        service.detached_restart({})
    assert "not running inside the applet's unit" in str(info.value)


def test_detached_restart_asks_systemd_from_outside_the_cgroup(monkeypatch):
    monkeypatch.setattr(service, 'supervised_here', lambda: True)
    spawned = []

    def spawn(command, start_new_session=False):
        spawned.append((command, start_new_session))
        return 'handle'

    # An empty PATH keeps `systemd-run` out of the argv, so the assertion pins
    # the command itself rather than whatever this machine happens to have.
    assert service.detached_restart({'INVOCATION_ID': 'abc', 'PATH': ''},
                                    spawn=spawn) == 'handle'
    assert spawned == [([service.COMMAND, '--user', 'restart', service.UNIT_NAME], True)]


# -- dispatch() ----------------------------------------------------------

def test_status_reports_an_absent_unit_and_says_how_to_install(env):
    said = []
    assert service.dispatch('status', env=env, echo=said.append, run=FakeRun()) == (
        service.STATUS_ABSENT)
    assert said[0].startswith(f'{service.UNIT_NAME}: not installed')
    assert 'luks-tray --svc-install' in said[1]


def test_status_reports_a_running_unit(env):
    install_unit(env, UNIT_TEXT)
    said = []
    code = service.dispatch('status', env=env, echo=said.append,
                            run=FakeRun(enabled=True, active=True))
    assert code == service.STATUS_ACTIVE
    assert said[0] == f'{service.UNIT_NAME}: active (enabled)'


def test_status_reports_an_installed_unit_that_is_not_running(env):
    install_unit(env, UNIT_TEXT)
    said = []
    code = service.dispatch('status', env=env, echo=said.append, run=FakeRun(enabled=True))
    assert code == service.STATUS_INACTIVE
    assert said[0] == f'{service.UNIT_NAME}: installed, not running (enabled)'


def test_status_reports_a_failed_unit_and_how_to_retry(env):
    install_unit(env, UNIT_TEXT)
    said = []
    code = service.dispatch('status', env=env, echo=said.append,
                            run=FakeRun(enabled=True, failed=True))
    assert code == service.STATUS_INACTIVE
    assert said[0] == f'{service.UNIT_NAME}: failed (enabled)'
    assert any('--svc-start clears the failed state' in line for line in said)


def test_status_warns_about_a_missing_launcher(env):
    install_unit(env, UNIT_TEXT)
    said = []
    service.dispatch('status', env=env, echo=said.append, run=FakeRun(enabled=True))
    assert any('cannot start' in line for line in said)


def test_show_file_needs_neither_systemd_nor_a_launcher(env):
    said = []
    code = service.dispatch('show-file', env=env, echo=said.append, systemd=False)
    assert code == service.OK
    assert 'ExecStart=%h/.local/bin/luks-tray' in said
    assert any(line.startswith('Environment=WAYLAND_DISPLAY=wayland-1') for line in said)


def test_every_verb_but_status_and_show_file_needs_a_user_manager(env, launcher):
    with pytest.raises(service.ServiceError) as info:
        service.dispatch('install', env=env, systemd=False)
    assert 'no systemd user manager' in str(info.value)


PROBES = ('is-enabled', 'is-active', 'is-failed')


def test_install_writes_the_rendered_unit_and_enables_it(env, launcher):
    said = []
    run = FakeRun()
    code = service.dispatch('install', env=env, echo=said.append, run=run, systemd=True)
    assert code == service.OK
    written = service.installed_unit(env).read_text(encoding='utf-8')
    assert written == service.rendered_unit(env=env)
    assert [verb for verb in run.verbs() if verb not in PROBES] == ['daemon-reload', 'enable']
    assert 'about to:' in said


def test_install_refuses_without_a_launcher(env):
    with pytest.raises(service.ServiceError) as info:
        service.dispatch('install', env=env, run=FakeRun(), echo=lambda line: None,
                         systemd=True)
    assert 'pipx install' in str(info.value)


def test_install_leaves_a_different_unit_alone_when_the_prompt_is_declined(env, launcher):
    path = install_unit(env, 'stale\n')
    said = []
    run = FakeRun()
    code = service.dispatch('install', env=env, echo=said.append, run=run, systemd=True,
                            ask=lambda _question: False)
    assert code == service.FAILED
    assert path.read_text(encoding='utf-8') == 'stale\n'
    assert said[-1] == '  left unchanged'


def test_install_replaces_a_different_unit_when_the_prompt_is_accepted(env, launcher):
    path = install_unit(env, 'stale\n')
    code = service.dispatch('install', env=env, echo=lambda line: None, systemd=True,
                            run=FakeRun(), ask=lambda _question: True)
    assert code == service.OK
    assert path.read_text(encoding='utf-8') == service.rendered_unit(env=env)


def test_yes_installs_without_asking(env, launcher):
    install_unit(env, 'stale\n')

    def refuse(_question):
        raise AssertionError('the prompt should not have been shown')

    assert service.dispatch('install', env=env, echo=lambda line: None, systemd=True,
                            run=FakeRun(), ask=refuse, yes=True) == service.OK


def test_stop_works_even_when_the_session_is_unknown(env):
    install_unit(env, UNIT_TEXT)
    env = {'HOME': env['HOME'], 'XDG_CONFIG_HOME': env['XDG_CONFIG_HOME']}
    said = []
    code = service.dispatch('stop', env=env, echo=said.append, systemd=True,
                            run=FakeRun(enabled=True, active=True))
    assert code == service.OK
    assert said[0] == f'{service.UNIT_NAME} stays enabled; it starts again at your next login'


def test_an_unknown_verb_is_refused(env, launcher):
    with pytest.raises(service.ServiceError) as info:
        service.dispatch('wobble', env=env, run=FakeRun(), echo=lambda line: None,
                         systemd=True)
    assert 'unknown service verb' in str(info.value)
