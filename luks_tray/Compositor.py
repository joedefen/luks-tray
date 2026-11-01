#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Compositor setup instructions for the app's Wayland windows.

    Wayland has no client-side request to make a window float: an
    `xdg_toplevel` is placed by the compositor, and `move()` is ignored.  The
    stable trait a rule can match is the surface `app_id`, which
    [`luks_tray.AppInfo`](luks_tray/AppInfo.py:1) sets to the same name for the
    dialogs, the error boxes and the file pickers.  This module turns that rule
    into the two snippets the supported tiling compositors understand.

    `--show-window-rules` is deliberately informational: it prints the snippet
    and never edits a user's compositor configuration.
"""
import os

from luks_tray.AppInfo import APP_NAME

NIRI = 'niri'
SWAY = 'sway'

#: niri reads this from ~/.config/niri/config.kdl.
NIRI_RULE = f'''window-rule {{
    match app-id="{APP_NAME}"
    open-floating true
}}'''

#: sway reads this from ~/.config/sway/config.  Do not add `no_focus`: a
#: password dialog that cannot take keyboard focus is worse than a tiled one.
SWAY_RULE = (
    f'for_window [app_id="{APP_NAME}"] floating enable, '
    'move position center, border normal'
)

#: niri renamed this action: `reload-config` is rejected by current releases
#: ("unrecognized subcommand"), and `load-config-file` is the accepted spelling.
NIRI_INTRO = (
    '# niri: add this to ~/.config/niri/config.kdl, then run'
    ' `niri msg action load-config-file`'
)
SWAY_INTRO = (
    '# sway: add this to ~/.config/sway/config, then run `swaymsg reload`'
)


def _environment(env):
    return os.environ if env is None else env


def desktop_names(env=None):
    """ Return the lower-cased desktop names this session advertises.

        `XDG_CURRENT_DESKTOP` is a colon-separated list on some desktops, and
        `DESKTOP_SESSION` is the fallback used by lighter setups.  Both are
        read, and duplicates are dropped, so a session that sets both does not
        make the caller decide which one wins.
    """
    environment = _environment(env)
    names = []
    for key in ('XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION'):
        for name in (environment.get(key) or '').split(':'):
            name = name.strip().lower()
            if name and name not in names:
                names.append(name)
    return names


def _detect_in(names):
    """ Return the first supported compositor named in `names`. """
    for compositor in (NIRI, SWAY):
        if any(compositor in name for name in names):
            return compositor
    return ''


def detect(env=None):
    """ Return 'niri', 'sway', or '' when neither is advertised.

        `XDG_CURRENT_DESKTOP` is checked first and `DESKTOP_SESSION` only as a
        fallback: the latter can be left over from the session that started the
        shell (a niri login running an explicit sway test, for example), and a
        stale value must not outrank the variable the user just set.
    """
    environment = _environment(env)
    for key in ('XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION'):
        names = [name.strip().lower()
                 for name in (environment.get(key) or '').split(':')
                 if name.strip()]
        compositor = _detect_in(names)
        if compositor:
            return compositor
    return ''


def rules_for(compositor):
    """ Return the rule snippet for `compositor`, or '' when unknown. """
    if compositor == NIRI:
        return NIRI_RULE
    if compositor == SWAY:
        return SWAY_RULE
    return ''


def show_rules(echo=print, env=None):
    """ Print the rules for this session and return a process exit status.

        An unknown desktop gets both snippets: the flag is most useful on a
        machine where the user is not yet sure what the compositor expects,
        and printing the alternatives is more helpful than refusing to answer.
    """
    compositor = detect(env)
    if compositor:
        echo(f'# luks-tray window rules for {compositor}')
        echo(NIRI_INTRO if compositor == NIRI else SWAY_INTRO)
        echo(rules_for(compositor))
        return 0

    echo('# luks-tray did not recognise this desktop session.')
    echo('# If dialogs open tiled, add the rule for your compositor:')
    echo('')
    echo(NIRI_INTRO)
    echo(NIRI_RULE)
    echo('')
    echo(SWAY_INTRO)
    echo(SWAY_RULE)
    return 0
