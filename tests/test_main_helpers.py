"""The `LuksTray` helpers that decide *which directories may be deleted*.

`auto_mount_folder` comes straight from `config.ini` and
[`remove_unused_automounts()`](../luks_tray/Main.py) `rmdir`s empty
subdirectories below it, so these checks are the only thing between a typo and
a sweep over unrelated paths.  They are tested as policy, not as plumbing:
every refusal is asserted by its message, because "skip the cleanup and say
why" is the intended behaviour.  The dependency report is here for the same
reason: when it says a command is missing, the message has to be actionable,
and on a host that has the command installed but out of PATH it was not.

The tests never construct a real `LuksTray` -- the helpers are reached through
`LuksTray.__new__` with the three attributes they read, so no
`QApplication`/`QSystemTrayIcon` is needed.
"""

import os
import stat as stat_module
from types import SimpleNamespace

import pytest

from luks_tray import Main
from luks_tray.IniTool import IniTool
from luks_tray.Main import LuksTray


def write_auto_root(tool, value):
    with open(tool.ini_path, 'w', encoding='utf-8') as handle:
        handle.write(f'[ui]\nauto_mount_folder = {value}\n')
    stamp = os.path.getmtime(tool.ini_path) + 5
    os.utime(tool.ini_path, (stamp, stamp))
    tool.update_config()


def make_tray(auto_root=None, upons=(), history_upons=()):
    """A `LuksTray` stand-in with only the attributes the helpers read."""
    tray = LuksTray.__new__(LuksTray)
    tray.ini_tool = IniTool()
    tray.ini_tool.update_config()
    if auto_root is not None:
        write_auto_root(tray.ini_tool, auto_root)
    tray.history = SimpleNamespace(upons=set(history_upons))
    tray.upons = set(upons)
    return tray


# --- the configured root ---------------------------------------------------


def test_get_auto_mount_root_uses_the_configured_value(vault_root):
    tray = make_tray(auto_root=vault_root)
    assert tray.get_auto_mount_root() == vault_root


def test_get_auto_mount_root_expands_a_tilde():
    tray = make_tray(auto_root='~/Vaults')
    assert tray.get_auto_mount_root() == os.path.expanduser('~/Vaults')


def test_get_auto_mount_root_refuses_a_relative_value_and_warns(capsys):
    tray = make_tray(auto_root='Vaults')
    root = tray.get_auto_mount_root()
    # The default is used, *not* `<cwd>/Vaults`: resolving it would make the
    # typo pass every later validation and silently relocate the vault area.
    assert root == os.path.expanduser('~/Vaults')
    assert 'is not an absolute path' in capsys.readouterr().out


# --- validate_auto_mount_root ---------------------------------------------


@pytest.mark.parametrize('path', ['/', '/home', '/usr', '/etc'])
def test_a_system_root_is_refused(path):
    assert 'system directory' in LuksTray.validate_auto_mount_root(path)


def test_a_path_too_close_to_the_root_is_refused():
    assert 'too close to the filesystem root' in (
        LuksTray.validate_auto_mount_root('/vaults'))


def test_a_symlinked_root_is_refused(tmp_path, vault_root):
    link = str(tmp_path / 'link')
    os.symlink(vault_root, link)
    assert 'is a symlink' in LuksTray.validate_auto_mount_root(link)


def test_the_home_directory_itself_is_refused(isolated_home):
    problem = LuksTray.validate_auto_mount_root(str(isolated_home))
    assert 'home directory itself' in problem


def test_an_ancestor_of_home_is_refused(isolated_home):
    problem = LuksTray.validate_auto_mount_root(str(isolated_home.parent))
    assert 'contains the home directory' in problem


def test_a_path_inside_a_system_tree_is_refused():
    problem = LuksTray.validate_auto_mount_root('/usr/local/Vaults')
    assert 'inside the system directory' in problem


def test_another_users_home_is_refused():
    problem = LuksTray.validate_auto_mount_root('/home/somebodyelse/Vaults')
    assert "inside another user's home directory" in problem


def test_a_dedicated_directory_that_does_not_exist_yet_is_accepted(vault_root):
    assert LuksTray.validate_auto_mount_root(str(vault_root) + '/not-yet') is None


def test_a_dedicated_directory_is_accepted(vault_root):
    assert LuksTray.validate_auto_mount_root(vault_root) is None


def test_an_existing_file_is_refused(tmp_path, vault_root):
    target = vault_root + '/file'
    open(target, 'w', encoding='utf-8').close()
    assert 'not a directory' in LuksTray.validate_auto_mount_root(target)


def test_a_mount_point_is_refused(vault_root, monkeypatch):
    monkeypatch.setattr(os.path, 'ismount', lambda _path: True)
    assert 'is a mount point' in LuksTray.validate_auto_mount_root(vault_root)


def test_a_directory_owned_by_someone_else_is_refused(vault_root, monkeypatch):
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path).startswith(vault_root):
            return SimpleNamespace(st_uid=4242, st_mode=stat_module.S_IFDIR | 0o755)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', fake_stat)
    problem = LuksTray.validate_auto_mount_root(vault_root)
    assert 'is owned by uid 4242' in problem


def test_a_world_writable_directory_is_refused(vault_root):
    os.chmod(vault_root, 0o777)
    try:
        assert 'writable by other' in (
            LuksTray.validate_auto_mount_root(vault_root))
    finally:
        os.chmod(vault_root, 0o700)


def test_a_group_writable_directory_is_accepted(vault_root):
    # `umask 0002` (user-private groups) makes `mkdir` create 0775; refusing
    # that would refuse the app's own directory. The caller tightens it.
    os.chmod(vault_root, 0o775)
    try:
        assert LuksTray.validate_auto_mount_root(vault_root) is None
    finally:
        os.chmod(vault_root, 0o700)


def test_a_directory_that_cannot_be_stat_ed_is_refused(vault_root, monkeypatch):
    # `os.path.exists()`/`isdir()` swallow the error and answer False, so the
    # stat has to fail only after those have answered, which is what a stale
    # handle on a live path looks like.
    real_stat = os.stat
    monkeypatch.setattr(os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(os.path, 'isdir', lambda _path: True)

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path).startswith(vault_root):
            raise OSError('stale handle')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', fake_stat)
    assert 'cannot be inspected' in LuksTray.validate_auto_mount_root(vault_root)


# --- ensure_auto_mount_root -----------------------------------------------


def test_ensure_creates_a_missing_root(monkeypatch, vault_root):
    seen = []
    monkeypatch.setattr('luks_tray.Main.run_cmd',
                        lambda args, *a, **k: seen.append(args) or None)
    target = vault_root + '/new'
    assert LuksTray.ensure_auto_mount_root(target) is None
    # The mode is stated, not inherited from the umask.
    assert seen == [['mkdir', '-p', '-m', '0700', target]]


def test_ensure_creates_the_root_owner_only(vault_root):
    target = os.path.join(vault_root, 'new')
    old_umask = os.umask(0o002)
    try:
        assert LuksTray.ensure_auto_mount_root(target) is None
    finally:
        os.umask(old_umask)
    assert stat_module.S_IMODE(os.stat(target).st_mode) == 0o700


def test_ensure_leaves_a_user_owned_root_alone(vault_root):
    assert LuksTray.ensure_auto_mount_root(vault_root) is None


def test_ensure_tightens_a_group_writable_user_owned_root(vault_root, capsys):
    os.chmod(vault_root, 0o775)
    try:
        assert LuksTray.ensure_auto_mount_root(vault_root) is None
        assert stat_module.S_IMODE(os.stat(vault_root).st_mode) == 0o700
        assert 'owner-only' in capsys.readouterr().out
    finally:
        os.chmod(vault_root, 0o700)


def test_repair_leaves_a_world_writable_root_alone(vault_root):
    # World-writable is refused by the policy, so it must not be "repaired"
    # into acceptance either: the caller has to hear about it.
    os.chmod(vault_root, 0o777)
    try:
        assert LuksTray.repair_auto_mount_root(vault_root) is False
        assert stat_module.S_IMODE(os.stat(vault_root).st_mode) == 0o777
    finally:
        os.chmod(vault_root, 0o700)


def test_ensure_chowns_a_root_owned_by_someone_else(monkeypatch, vault_root):
    seen = []
    monkeypatch.setattr(LuksTray, 'get_real_user_ids', staticmethod(lambda: (999, 998)))
    monkeypatch.setattr(LuksTray, 'validate_auto_mount_root',
                        staticmethod(lambda _path: None))
    monkeypatch.setattr('luks_tray.Main.sudo_cmd',
                        lambda args, *a, **k: seen.append(args) or None)
    assert LuksTray.ensure_auto_mount_root(vault_root) is None
    # Non-recursive: `-R` would rewrite ownership inside whatever is mounted.
    assert seen == [['chown', '999:998', vault_root]]


def test_ensure_refuses_to_chown_a_root_validation_rejects(
        monkeypatch, isolated_home):
    # The directory exists but belongs to "somebody else", so the repair path is
    # entered -- and then refused, because the root is an ancestor of $HOME.
    monkeypatch.setattr(LuksTray, 'get_real_user_ids', staticmethod(lambda: (999, 998)))
    monkeypatch.setattr('luks_tray.Main.sudo_cmd',
                        lambda *a, **k: pytest.fail('must not chown'))
    problem = LuksTray.ensure_auto_mount_root(str(isolated_home.parent))
    assert problem.startswith('FAIL: not repairing ownership')


# --- generate_auto_mount_folder -------------------------------------------


def test_generate_refuses_a_root_that_is_not_a_directory(tmp_path, vault_root):
    target = vault_root + '/file'
    open(target, 'w', encoding='utf-8').close()
    tray = make_tray(auto_root=target)
    with pytest.raises(RuntimeError, match='exists but is not a directory'):
        tray.generate_auto_mount_folder()


def test_generate_refuses_when_the_root_cannot_be_created(monkeypatch, vault_root):
    tray = make_tray(auto_root=vault_root + '/blocked')
    monkeypatch.setattr(LuksTray, 'ensure_auto_mount_root',
                        staticmethod(lambda _path: 'FAIL: permission denied'))
    with pytest.raises(RuntimeError, match='cannot create auto_mount_folder'):
        tray.generate_auto_mount_folder()


def test_generate_prefers_the_short_suffix_of_a_petname(monkeypatch, vault_root):
    tray = make_tray(auto_root=vault_root)
    monkeypatch.setattr('luks_tray.Main.petname.Generate',
                        lambda *_a, **_k: 'stoic_turing')
    assert tray.generate_auto_mount_folder() == os.path.join(vault_root, 'turing')


def test_generate_skips_a_name_that_is_already_mounted(monkeypatch, vault_root):
    taken = os.path.join(vault_root, 'turing')
    tray = make_tray(auto_root=vault_root, upons={taken})
    names = iter(['stoic_turing', 'brave_hopper'] + ['stoic_turing'] * 40)
    monkeypatch.setattr('luks_tray.Main.petname.Generate',
                        lambda *_a, **_k: next(names))
    assert tray.generate_auto_mount_folder() == os.path.join(vault_root, 'hopper')


def test_generate_skips_a_name_that_is_in_the_history(monkeypatch, vault_root):
    taken = os.path.join(vault_root, 'turing')
    tray = make_tray(auto_root=vault_root, history_upons={taken})
    names = iter(['stoic_turing', 'brave_hopper'] + ['stoic_turing'] * 40)
    monkeypatch.setattr('luks_tray.Main.petname.Generate',
                        lambda *_a, **_k: next(names))
    assert tray.generate_auto_mount_folder() == os.path.join(vault_root, 'hopper')


def test_generate_falls_back_to_a_numeric_name(monkeypatch, vault_root):
    tray = make_tray(auto_root=vault_root)
    monkeypatch.setattr('luks_tray.Main.petname.Generate',
                        lambda *_a, **_k: 'stoic_turing')
    # Every short and every full name is taken, so the 10000-try fallback runs.
    monkeypatch.setattr('luks_tray.Main.random.randint', lambda _a, _b: 1234)
    tray.upons = {os.path.join(vault_root, 'turing'),
                  os.path.join(vault_root, 'stoic_turing')}
    assert tray.generate_auto_mount_folder() == os.path.join(vault_root, 'vault1234')


# --- is_strictly_within ----------------------------------------------------


def test_is_strictly_within_accepts_a_child(vault_root):
    assert LuksTray.is_strictly_within(os.path.join(vault_root, 'a'), vault_root)


def test_is_strictly_within_rejects_the_parent_itself(vault_root):
    assert not LuksTray.is_strictly_within(vault_root, vault_root)


def test_is_strictly_within_rejects_a_sibling_with_a_shared_prefix(vault_root):
    sibling = vault_root + 'x'
    assert not LuksTray.is_strictly_within(os.path.join(sibling, 'a'), vault_root)


def test_is_strictly_within_reports_an_uncomparable_argument(capsys):
    assert LuksTray.is_strictly_within(None, '/tmp') is False
    assert 'cannot compare' in capsys.readouterr().out


# --- remove_empty_dir / is_empty_dir --------------------------------------


def test_remove_empty_dir_removes_an_empty_directory(vault_root):
    target = os.path.join(vault_root, 'gone')
    os.mkdir(target)
    assert LuksTray.remove_empty_dir(target) is True
    assert not os.path.exists(target)


def test_remove_empty_dir_keeps_a_directory_with_files(vault_root):
    target = os.path.join(vault_root, 'in-use')
    os.mkdir(target)
    open(os.path.join(target, 'file'), 'w', encoding='utf-8').close()
    assert LuksTray.remove_empty_dir(target) is False
    assert os.path.isdir(target)


def test_remove_empty_dir_ignores_a_symlink(tmp_path, vault_root):
    link = str(tmp_path / 'link')
    os.symlink(vault_root, link)
    assert LuksTray.remove_empty_dir(link) is False
    assert os.path.islink(link)


def test_remove_empty_dir_ignores_a_file(tmp_path):
    target = str(tmp_path / 'file')
    open(target, 'w', encoding='utf-8').close()
    assert LuksTray.remove_empty_dir(target) is False


def test_remove_empty_dir_ignores_a_mount_point(vault_root, monkeypatch):
    monkeypatch.setattr(os.path, 'ismount', lambda _path: True)
    assert LuksTray.remove_empty_dir(vault_root) is False


def test_remove_empty_dir_escalates_on_a_permission_error(vault_root, monkeypatch):
    seen = []

    # `*args, **kwargs` because `shutil.rmtree()` -- the fixture's teardown --
    # also calls `os.rmdir(..., dir_fd=...)` while this patch is installed.
    def denied(_path, *_args, **_kwargs):
        raise PermissionError('parent not writable')

    monkeypatch.setattr(os, 'rmdir', denied)
    monkeypatch.setattr('luks_tray.Main.sudo_cmd',
                        lambda args, *a, **k: seen.append(args) or None)
    assert LuksTray.remove_empty_dir(vault_root) is True
    assert seen == [['rmdir', vault_root]]


def test_remove_empty_dir_reports_a_failed_escalation(vault_root, monkeypatch, capsys):
    def denied(*_args, **_kwargs):
        raise PermissionError('parent not writable')

    monkeypatch.setattr(os, 'rmdir', denied)
    monkeypatch.setattr('luks_tray.Main.sudo_cmd', lambda *a, **k: 'FAIL: nope')
    assert LuksTray.remove_empty_dir(vault_root) is False
    assert 'not removing' in capsys.readouterr().out


def test_is_empty_dir_distinguishes_empty_from_in_use(vault_root):
    assert LuksTray.is_empty_dir(vault_root) is True
    os.mkdir(os.path.join(vault_root, 'sub'))
    assert LuksTray.is_empty_dir(vault_root) is False


def test_is_empty_dir_reports_an_unlistable_directory(vault_root, monkeypatch, capsys):
    monkeypatch.setattr(os, 'scandir',
                        lambda _path: (_ for _ in ()).throw(PermissionError()))
    assert LuksTray.is_empty_dir(vault_root) is False
    assert 'cannot list' in capsys.readouterr().out


# --- remove_if_auto --------------------------------------------------------


def test_remove_if_auto_ignores_a_path_outside_the_root(vault_root, tmp_path):
    tray = make_tray(auto_root=vault_root)
    assert tray.remove_if_auto(str(tmp_path / 'elsewhere')) is False


def test_remove_if_auto_removes_an_empty_vault(vault_root):
    tray = make_tray(auto_root=vault_root)
    target = os.path.join(vault_root, 'turing')
    os.mkdir(target)
    assert tray.remove_if_auto(target) is True
    assert not os.path.exists(target)


def test_remove_if_auto_refuses_when_the_root_fails_validation(
        monkeypatch, vault_root, capsys):
    tray = make_tray(auto_root=vault_root)
    monkeypatch.setattr(LuksTray, 'validate_auto_mount_root',
                        staticmethod(lambda _path: 'not a dedicated area'))
    target = os.path.join(vault_root, 'turing')
    os.mkdir(target)
    assert tray.remove_if_auto(target) is False
    assert 'not a dedicated area' in capsys.readouterr().out
    assert os.path.isdir(target)


# --- remove_unused_automounts ---------------------------------------------


def test_cleanup_skips_an_invalid_root(vault_root, monkeypatch, capsys):
    tray = make_tray(auto_root=vault_root)
    monkeypatch.setattr(LuksTray, 'validate_auto_mount_root',
                        staticmethod(lambda _path: 'system directory'))
    assert tray.remove_unused_automounts() == 0
    assert 'skipping auto-mount cleanup' in capsys.readouterr().out


def test_cleanup_does_nothing_when_the_root_does_not_exist(vault_root):
    tray = make_tray(auto_root=vault_root + '/not-yet')
    assert tray.remove_unused_automounts() == 0


def test_cleanup_tightens_a_group_writable_root(vault_root):
    os.chmod(vault_root, 0o775)
    empty = os.path.join(vault_root, 'empty')
    os.mkdir(empty)
    try:
        tray = make_tray(auto_root=vault_root)
        assert tray.remove_unused_automounts() == 1
        assert not os.path.exists(empty)
        assert stat_module.S_IMODE(os.stat(vault_root).st_mode) == 0o700
    finally:
        os.chmod(vault_root, 0o700)


def test_cleanup_removes_only_the_empty_plain_vaults(vault_root, capsys):
    empty = os.path.join(vault_root, 'empty')
    in_use = os.path.join(vault_root, 'in-use')
    dotted = os.path.join(vault_root, '.hidden')
    for path in (empty, in_use, dotted):
        os.mkdir(path)
    open(os.path.join(in_use, 'file'), 'w', encoding='utf-8').close()
    open(os.path.join(vault_root, 'stray.txt'), 'w', encoding='utf-8').close()
    os.symlink(empty, os.path.join(vault_root, 'link'))

    tray = make_tray(auto_root=vault_root)
    assert tray.remove_unused_automounts() == 1
    assert not os.path.exists(empty)
    assert os.path.isdir(in_use)
    assert os.path.isdir(dotted)          # dot-entries are never touched
    assert os.path.isfile(os.path.join(vault_root, 'stray.txt'))
    assert os.path.islink(os.path.join(vault_root, 'link'))
    assert 'removed unused auto-mount folder' in capsys.readouterr().out


def test_cleanup_skips_a_mount_point(vault_root, monkeypatch):
    mounted = os.path.join(vault_root, 'mounted')
    os.mkdir(mounted)
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, 'ismount',
                        lambda path: True if os.fspath(path) == mounted
                        else real_ismount(path))
    tray = make_tray(auto_root=vault_root)
    assert tray.remove_unused_automounts() == 0
    assert os.path.isdir(mounted)


def test_cleanup_skips_a_directory_owned_by_someone_else(vault_root, monkeypatch):
    other = os.path.join(vault_root, 'theirs')
    os.mkdir(other)
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path) == other:
            return SimpleNamespace(st_uid=4242, st_mode=stat_module.S_IFDIR | 0o755)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', fake_stat)
    tray = make_tray(auto_root=vault_root)
    assert tray.remove_unused_automounts() == 0
    assert os.path.isdir(other)


def test_cleanup_reports_an_unlistable_root(vault_root, monkeypatch, capsys):
    tray = make_tray(auto_root=vault_root)
    monkeypatch.setattr(os, 'listdir',
                        lambda _path: (_ for _ in ()).throw(PermissionError()))
    assert tray.remove_unused_automounts() == 0
    assert 'cannot list auto_mount_folder' in capsys.readouterr().out


def test_cleanup_reports_a_directory_it_cannot_inspect(
        vault_root, monkeypatch, capsys):
    hard = os.path.join(vault_root, 'hard')
    os.mkdir(hard)
    real_stat = os.stat
    real_isdir = os.path.isdir
    monkeypatch.setattr(os.path, 'isdir',
                        lambda path: True if os.fspath(path) == hard
                        else real_isdir(path))

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path) == hard:
            raise OSError('stale handle')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', fake_stat)
    tray = make_tray(auto_root=vault_root)
    assert tray.remove_unused_automounts() == 0
    assert 'cannot inspect' in capsys.readouterr().out


# --- check_dependencies -----------------------------------------------------

def test_a_complete_host_is_reported_quietly(monkeypatch, capsys):
    monkeypatch.setattr(Main.shutil, 'which', lambda util: f'/usr/bin/{util}')
    found, missing = LuksTray.check_dependencies()
    assert missing == []
    assert 'cryptsetup' in found
    assert capsys.readouterr().out == ''


def test_missing_is_printed_with_the_install_hint(monkeypatch, capsys):
    monkeypatch.setattr(Main.shutil, 'which',
                        lambda util: None if util == 'cryptsetup' else f'/usr/bin/{util}')
    _, missing = LuksTray.check_dependencies()
    assert missing == ['cryptsetup']
    assert 'cryptsetup - please install' in capsys.readouterr().out


def test_a_missing_report_names_the_path_it_searched(monkeypatch, capsys):
    # The lookup is `shutil.which()`, so "not installed" and "installed out of
    # PATH" print identically unless the report names the PATH.  A unit that
    # pinned PATH without /usr/sbin once produced exactly that confusion.
    monkeypatch.setattr(Main.shutil, 'which', lambda util: None)
    monkeypatch.setenv('PATH', '/usr/local/bin:/usr/bin')
    _, missing = LuksTray.check_dependencies()
    assert missing
    assert 'PATH=/usr/local/bin:/usr/bin' in capsys.readouterr().out


# --- ids and paths ---------------------------------------------------------


def test_real_user_ids_default_to_the_effective_ids(monkeypatch):
    monkeypatch.delenv('SUDO_UID', raising=False)
    monkeypatch.delenv('SUDO_GID', raising=False)
    assert LuksTray.get_real_user_ids() == (os.getuid(), os.getgid())


def test_real_user_ids_prefer_the_sudo_environment(monkeypatch):
    monkeypatch.setenv('SUDO_UID', '1000')
    monkeypatch.setenv('SUDO_GID', '1000')
    assert LuksTray.get_real_user_ids() == (1000, 1000)


def test_real_user_ids_warn_and_fall_back_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv('SUDO_UID', 'not-a-number')
    monkeypatch.setenv('SUDO_GID', '1000')
    assert LuksTray.get_real_user_ids() == (os.getuid(), 1000)
    assert 'ignoring non-numeric id' in capsys.readouterr().out


def test_real_user_home_is_the_env_home_without_sudo(isolated_home):
    assert LuksTray.get_real_user_home() == str(isolated_home)


def test_real_user_home_reads_passwd_for_sudo_user(monkeypatch):
    monkeypatch.setenv('SUDO_USER', 'root')
    assert LuksTray.get_real_user_home() == '/root'


def test_real_user_home_guesses_and_warns_for_an_unknown_user(monkeypatch, capsys):
    monkeypatch.setenv('SUDO_USER', 'nobody-here-xyz')
    assert LuksTray.get_real_user_home() == '/home/nobody-here-xyz'
    assert 'has no passwd entry' in capsys.readouterr().out


def test_expand_real_user_handles_the_bare_tilde(monkeypatch):
    monkeypatch.setenv('SUDO_USER', 'joe')
    assert LuksTray.expand_real_user('~') == '/home/joe'
    assert LuksTray.expand_real_user('~/Vaults') == '/home/joe/Vaults'


def test_expand_real_user_leaves_other_users_and_plain_paths(monkeypatch):
    monkeypatch.setenv('SUDO_USER', 'joe')
    assert LuksTray.expand_real_user('/mnt/x') == '/mnt/x'
    assert LuksTray.expand_real_user('relative') == 'relative'


def test_expand_real_user_falls_back_to_expanduser(monkeypatch, isolated_home):
    monkeypatch.delenv('SUDO_USER', raising=False)
    assert LuksTray.expand_real_user('~/Vaults') == str(isolated_home / 'Vaults')


# --- the stand-in for a container lsblk cannot see -------------------------


def make_file_tray(mount_infos=None):
    """A `LuksTray` stand-in with only what `history_file_container()` reads."""
    tray = LuksTray.__new__(LuksTray)
    tray.mount_infos = mount_infos or {}
    return tray


def vital(**values):
    defaults = {'uuid': 'salt-abc', 'back_file': '/home/joe/Vaults/old.crypt',
                'upon': '', 'meta_type': 'tcrypt'}
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_a_history_only_container_offers_the_mapper_name_it_would_have(monkeypatch):
    # A TrueCrypt/VeraCrypt file has no loop device, so lsblk never reports it
    # and this namespace is the menu's only handle on it.
    monkeypatch.setattr(Main.os.path, 'exists', lambda _path: False)
    container = make_file_tray().history_file_container(vital())
    # `mount_luks_container()` names a file's mapper `<basename>-luks`; the
    # menu needs the same name to be able to close it again.
    assert container.name == 'old.crypt-luks'
    assert container.type == 'crypt'
    assert container.back_file == '/home/joe/Vaults/old.crypt'
    assert container.uuid == 'salt-abc'
    assert container.meta_type == 'tcrypt'
    assert container.opened is False
    assert container.upon == ''

def test_an_open_history_only_container_carries_its_mount_point(monkeypatch):
    mount_infos = {'/dev/mapper/old.crypt-luks':
                   SimpleNamespace(upon='/home/joe/Vaults/old', readonly=True)}
    monkeypatch.setattr(Main.os.path, 'exists', lambda _path: True)
    container = make_file_tray(mount_infos).history_file_container(vital())
    assert container.opened is True
    assert container.upon == '/home/joe/Vaults/old'
    assert container.mounts == ['/home/joe/Vaults/old']
    assert container.readonly is True


def test_an_open_container_that_is_not_mounted_has_no_mount_point(monkeypatch):
    monkeypatch.setattr(Main.os.path, 'exists', lambda _path: True)
    container = make_file_tray().history_file_container(vital())
    assert container.opened is True
    assert container.upon == ''
    assert container.mounts == []


# --- keying a container that lsblk could not identify ----------------------


def loop_container(back_file, uuid=None):
    """A file container as `parse_lsblk()` builds one for a loop device."""
    container = Main.DeviceInfo.make_partition_namespace('x.img-luks', '64M')
    container.type = 'crypt'
    container.uuid = uuid
    container.back_file = back_file
    container.upon = '/home/joe/Vaults/x'
    container.opened = True
    return container


def refuse(_path):
    """Stand-in for `get_container_identity()` when it must not be asked."""
    raise AssertionError('an identifiable container must not be probed')


def test_a_loop_container_lsblk_cannot_identify_is_keyed_by_its_header(monkeypatch):
    # A TrueCrypt/VeraCrypt header is encrypted, so `lsblk` reports no UUID for
    # the loop device and the entry arrives keyed by None while the history
    # keys the same file by its salt. That mismatch is what put two identical
    # rows in the menu (lsblk's one and the history stand-in) and let
    # `merge_containers_history()` write a None entry of its own.
    container = loop_container('/home/joe/.Vaults/x.img')
    tray = make_file_tray()
    tray.containers = {None: container}
    monkeypatch.setattr(Main, 'get_container_identity', lambda _path: 'salt-abc')

    tray.identify_file_containers()

    assert list(tray.containers) == ['salt-abc']
    assert tray.containers['salt-abc'] is container
    assert container.uuid == 'salt-abc'


def test_a_container_that_already_has_a_uuid_is_not_probed(monkeypatch):
    tray = make_file_tray()
    tray.containers = {'UUID-1': loop_container('/home/joe/.Vaults/y.img',
                                                uuid='UUID-1')}
    monkeypatch.setattr(Main, 'get_container_identity', refuse)

    tray.identify_file_containers()

    assert list(tray.containers) == ['UUID-1']


def test_a_device_entry_has_no_file_to_identify(monkeypatch):
    # Device containers are keyed by the LUKS UUID; there is nothing to probe
    # even when the key is empty.
    container = Main.DeviceInfo.make_partition_namespace('sda1', '3T')
    tray = make_file_tray()
    tray.containers = {'': container}
    monkeypatch.setattr(Main, 'get_container_identity', refuse)

    tray.identify_file_containers()

    assert list(tray.containers) == ['']


def test_a_file_whose_identity_cannot_be_read_stays_visible(monkeypatch):
    # '' means "no answer": keeping the entry under its own key is what makes
    # the container stay in the menu, and a password typed into its dialog
    # still finds it.
    container = loop_container('/home/joe/.Vaults/gone.img')
    tray = make_file_tray()
    tray.containers = {None: container}
    monkeypatch.setattr(Main, 'get_container_identity', lambda _path: '')

    tray.identify_file_containers()

    assert list(tray.containers) == [None]
    assert tray.containers[None] is container


# --- menu text: the real user's home is abbreviated ------------------------


def test_shorten_home_abbreviates_the_real_users_home(isolated_home):
    home = str(isolated_home)
    assert LuksTray.shorten_home(f'{home}/Vaults/x') == '~/Vaults/x'
    assert LuksTray.shorten_home(home) == '~'


def test_shorten_home_leaves_everything_else_alone(isolated_home):
    home = str(isolated_home)
    assert LuksTray.shorten_home('') == ''
    assert LuksTray.shorten_home('/mnt/x') == '/mnt/x'
    # A sibling whose name merely starts the same way is not inside the home.
    assert LuksTray.shorten_home(f'{home}x/y') == f'{home}x/y'
    assert LuksTray.shorten_home('/home/somebody-else/x') == '/home/somebody-else/x'
