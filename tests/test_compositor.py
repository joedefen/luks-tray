"""`Compositor`: the app-id rule snippets and the `--show-window-rules` verb.

The snippets are the contract between the app and a tiling compositor: they
must match the `app_id` set in [`luks_tray.AppInfo`](../luks_tray/AppInfo.py),
and the sway rule must not carry `no_focus` -- a password dialog that cannot
take keyboard focus is unusable.
"""
import pytest

from luks_tray import Compositor, Main
from luks_tray.AppInfo import (APP_NAME, heading_text, window_title)


# --- app identity ----------------------------------------------------------


def test_window_title_appends_one_suffix():
    assert window_title('Options') == 'Options [luks-tray]'
    assert window_title('Options [luks-tray]') == 'Options [luks-tray]'


def test_heading_text_strips_the_window_suffix():
    assert heading_text('Options') == 'Options'
    assert heading_text('Options [luks-tray]') == 'Options'


def test_the_rule_uses_the_same_app_id_as_the_tray():
    assert APP_NAME == 'luks-tray'


# --- detection -------------------------------------------------------------


def test_detect_reads_xdg_current_desktop():
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'niri'}) == 'niri'


def test_detect_reads_desktop_session():
    assert Compositor.detect({'DESKTOP_SESSION': 'Sway-custom'}) == 'sway'


def test_detect_is_case_insensitive_and_splits_colon_lists():
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'GNOME:niri'}) == 'niri'


def test_detect_prefers_xdg_current_desktop_over_a_stale_desktop_session():
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'sway',
                              'DESKTOP_SESSION': 'niri'}) == 'sway'
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'niri',
                              'DESKTOP_SESSION': 'sway'}) == 'niri'


def test_detect_falls_back_to_desktop_session():
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'GNOME',
                              'DESKTOP_SESSION': 'sway'}) == 'sway'


def test_detect_returns_empty_for_an_unknown_desktop():
    assert Compositor.detect({'XDG_CURRENT_DESKTOP': 'KDE'}) == ''
    assert Compositor.detect({}) == ''


# --- snippets --------------------------------------------------------------


def test_niri_rule_opens_the_app_id_floating():
    rule = Compositor.rules_for('niri')
    assert 'window-rule' in rule
    assert 'match app-id="luks-tray"' in rule
    assert 'open-floating true' in rule


def test_sway_rule_uses_bracketed_criteria_and_no_no_focus():
    rule = Compositor.rules_for('sway')
    assert 'for_window [app_id="luks-tray"]' in rule
    assert 'floating enable' in rule
    assert 'move position center' in rule
    assert 'no_focus' not in rule


def test_unknown_compositor_has_no_rule():
    assert Compositor.rules_for('hyprland') == ''


def test_the_intros_name_the_reload_command_each_compositor_accepts():
    # `niri msg action reload-config` is rejected by current niri
    # ("unrecognized subcommand"); `load-config-file` is the accepted spelling.
    assert 'niri msg action load-config-file' in Compositor.NIRI_INTRO
    assert 'swaymsg reload' in Compositor.SWAY_INTRO


def test_show_rules_prints_both_snippets_when_unknown():
    said = []
    assert Compositor.show_rules(echo=said.append, env={}) == 0
    text = '\n'.join(said)
    assert 'window-rule' in text
    assert 'for_window [app_id="luks-tray"]' in text


# --- the CLI verb ----------------------------------------------------------


def test_cli_show_window_rules_prints_sway_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['luks-tray', '--show-window-rules'])
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'sway')
    monkeypatch.delenv('DESKTOP_SESSION', raising=False)

    with pytest.raises(SystemExit) as raised:
        Main.main()

    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert 'for_window [app_id="luks-tray"]' in out
    assert 'no_focus' not in out


def test_cli_show_window_rules_rejects_a_service_verb(monkeypatch, capsys):
    monkeypatch.setattr(
        'sys.argv', ['luks-tray', '--show-window-rules', '--svc-status'])

    with pytest.raises(SystemExit) as raised:
        Main.main()

    assert raised.value.code == 2
    assert 'cannot be combined' in capsys.readouterr().err
