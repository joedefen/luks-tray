"""`IniTool`: the config folder, the file it creates, and the parsing.

The parsing is the interesting half.  A hand-edited `config.ini` must never be
able to break start-up, so every mismatch -- unknown key, unparsable bool,
wrong type -- falls back to the golden default and says so.
"""

import os

import pytest

from luks_tray.IniTool import IniTool


def _write(tool, body):
    """Replace config.ini and make sure `update_config()` sees a new mtime."""
    with open(tool.ini_path, 'w', encoding='utf-8') as handle:
        handle.write(body)
    stamp = os.path.getmtime(tool.ini_path) + 5
    os.utime(tool.ini_path, (stamp, stamp))


def test_paths_only_creates_nothing(isolated_home):
    tool = IniTool(paths_only=True)
    assert tool.folder == str(isolated_home / '.config/luks-tray')
    assert not os.path.exists(tool.folder)
    assert tool.ini_path.endswith('config.ini')
    assert tool.log_path.endswith('debug.log')
    assert tool.history_path.endswith('history.json')
    assert tool.lock_path.endswith('luks-tray.lock')


def test_ensure_folder_does_not_write_a_config():
    tool = IniTool(paths_only=True)
    assert tool.ensure_folder() == tool.folder
    assert os.path.isdir(tool.folder)
    assert not os.path.exists(tool.ini_path)


def test_ensure_ini_file_writes_every_default():
    tool = IniTool(paths_only=True)
    tool.ensure_ini_file()
    text = open(tool.ini_path, encoding='utf-8').read()
    assert '[ui]' in text
    assert 'auto_mount_folder = ~/Vaults' in text
    assert 'show_passwords_by_default = True' in text
    assert 'show_anomaly_alerts = True' in text


def test_constructing_without_paths_only_creates_the_file():
    tool = IniTool()
    assert os.path.isfile(tool.ini_path)


def test_get_current_val_falls_back_to_the_default_before_a_read():
    tool = IniTool(paths_only=True)
    assert tool.get_current_val('auto_mount_folder') == '~/Vaults'
    assert tool.get_current_val('show_anomaly_alerts') is True


def test_update_config_reads_once_and_then_reports_no_change():
    tool = IniTool()
    assert tool.update_config() is True
    assert tool.update_config() is False


def test_update_config_notices_a_new_mtime():
    tool = IniTool()
    tool.update_config()
    _write(tool, '[ui]\nauto_mount_folder = /tmp/vaults\n')
    assert tool.update_config() is True
    assert tool.get_current_val('auto_mount_folder') == '/tmp/vaults'


def test_bools_are_parsed_from_true_and_false():
    tool = IniTool()
    _write(tool, '[ui]\nshow_passwords_by_default = false\n'
                 'show_anomaly_alerts = TRUE\n')
    tool.update_config()
    assert tool.get_current_val('show_passwords_by_default') is False
    assert tool.get_current_val('show_anomaly_alerts') is True


def test_a_string_that_is_not_a_bool_keeps_the_default():
    tool = IniTool()
    _write(tool, '[ui]\nshow_passwords_by_default = yes-please\n')
    tool.update_config()
    assert tool.get_current_val('show_passwords_by_default') is True


def test_an_unknown_key_is_skipped_not_kept():
    tool = IniTool()
    _write(tool, '[ui]\nnot_a_setting = 12\n')
    tool.update_config()
    assert not hasattr(tool.params_by_selector['ui'], 'not_a_setting')
    # ...and the known keys still parsed.
    assert tool.get_current_val('auto_mount_folder') == '~/Vaults'


def test_a_missing_section_yields_the_defaults():
    tool = IniTool()
    _write(tool, '[other]\nkey = value\n')
    tool.update_config()
    assert tool.get_current_val('auto_mount_folder') == '~/Vaults'


def test_selectors_are_returned_in_order():
    assert IniTool.get_selectors() == ['ui']


def test_the_default_reads_the_golden_table():
    tool = IniTool(paths_only=True)
    assert tool.the_default('auto_mount_folder') == '~/Vaults'
    with pytest.raises(KeyError):
        tool.the_default('no_such_key')


# --- the write side (`set_values`, used by the Options dialog) --------------


def test_set_values_writes_and_the_next_read_sees_it():
    tool = IniTool()
    tool.update_config()

    assert tool.set_values({'auto_mount_folder': '/mnt/vaults',
                            'show_anomaly_alerts': False}) == []

    assert tool.get_current_val('auto_mount_folder') == '/mnt/vaults'
    assert tool.get_current_val('show_anomaly_alerts') is False
    # ...and the file itself holds them, so the next start agrees.
    fresh = IniTool(paths_only=True)
    fresh.update_config()
    assert fresh.get_current_val('auto_mount_folder') == '/mnt/vaults'
    assert fresh.get_current_val('show_anomaly_alerts') is False


def test_set_values_repairs_a_bool_that_arrives_as_text():
    tool = IniTool()
    assert tool.set_values({'show_passwords_by_default': 'false'}) == []
    assert tool.get_current_val('show_passwords_by_default') is False


def test_set_values_refuses_an_unknown_key_and_writes_nothing():
    tool = IniTool()
    tool.update_config()
    before = open(tool.ini_path, encoding='utf-8').read()

    errors = tool.set_values({'not_a_setting': 1, 'auto_mount_folder': '/mnt/v'})

    assert errors == ['ui.not_a_setting: unknown setting']
    # All or nothing: the valid key in the same call was not applied either.
    assert open(tool.ini_path, encoding='utf-8').read() == before
    assert tool.get_current_val('auto_mount_folder') == '~/Vaults'


def test_set_values_refuses_a_value_of_the_wrong_type():
    tool = IniTool()
    assert tool.set_values({'show_anomaly_alerts': 'sometimes',
                            'auto_mount_folder': 7}) == [
        "ui.show_anomaly_alerts: 'sometimes' is not a bool",
        'ui.auto_mount_folder: 7 is not a string',
    ]


def test_set_values_keeps_a_setting_it_does_not_know():
    tool = IniTool()
    _write(tool, '[ui]\nfuture_setting = 12\nauto_mount_folder = /mnt/v\n')
    tool.update_config()

    assert tool.set_values({'show_anomaly_alerts': False}) == []

    # The tray still skips it, but saving from the dialog must not delete it.
    assert 'future_setting = 12' in open(tool.ini_path, encoding='utf-8').read()


def test_set_values_creates_the_file_when_it_is_missing():
    tool = IniTool(paths_only=True)
    assert tool.set_values({'auto_mount_folder': '/mnt/v'}) == []
    assert os.path.isfile(tool.ini_path)


def test_set_values_reports_a_write_it_cannot_do(monkeypatch):
    tool = IniTool()

    def no_temp_file(*args, **kwargs):
        raise OSError('read-only file system')

    monkeypatch.setattr('luks_tray.IniTool.tempfile.mkstemp', no_temp_file)
    assert tool.set_values({'auto_mount_folder': '/mnt/v'}) == [
        f'cannot write {tool.ini_path!r}: read-only file system']


def test_set_values_leaves_no_temporary_file_behind():
    tool = IniTool()
    tool.set_values({'auto_mount_folder': '/mnt/v'})
    assert [name for name in os.listdir(tool.folder)
            if name.startswith('.config-')] == []


def test_coerce_value_rejects_a_golden_type_it_cannot_handle():
    with pytest.raises(ValueError):
        IniTool.coerce_value([], 1)
