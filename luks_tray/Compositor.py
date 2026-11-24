#!/usr/bin/env python3
"""Compositor window rules for the app's Wayland dialogs."""
import os

from luks_tray.AppInfo import APP_NAME

NIRI = 'niri'
SWAY = 'sway'

NIRI_RULE = f'''window-rule {{
    match app-id="{APP_NAME}"
    open-floating true
}}'''

SWAY_RULE = (
    f'for_window [app_id="{APP_NAME}"] floating enable, '
    'move position center, border normal'
)

NIRI_INTRO = (
    '# niri: add this to ~/.config/niri/config.kdl, then run'
    ' `niri msg action load-config-file`'
)
SWAY_INTRO = (
    '# sway: add this to ~/.config/sway/config, then run `swaymsg reload`'
)


def _environment(env):
    """Return `env`, or `os.environ` when it is None."""
    return os.environ if env is None else env


def desktop_names(env=None):
    """Return the lower-cased desktop names advertised by the session.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        list: Unique names from `XDG_CURRENT_DESKTOP` and `DESKTOP_SESSION`.
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
    """Return the first supported compositor named in `names`."""
    for compositor in (NIRI, SWAY):
        if any(compositor in name for name in names):
            return compositor
    return ''


def detect(env=None):
    """Return `'niri'`, `'sway'`, or `''` when neither is advertised.

    Args:
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        str: The detected compositor, if any.
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
    """Return the rule snippet for `compositor`, or `''` when unknown.

    Args:
        compositor: Compositor name.

    Returns:
        str: The rule snippet.
    """
    if compositor == NIRI:
        return NIRI_RULE
    if compositor == SWAY:
        return SWAY_RULE
    return ''


def show_rules(echo=print, env=None):
    """Print the rules for this session and return an exit status.

    Args:
        echo: Callable used to print each line.
        env: Environment mapping to read; defaults to `os.environ`.

    Returns:
        int: Process exit status, always 0.
    """
    compositor = detect(env)
    if compositor:
        echo(f'# luks-tray window rules for {compositor}')
        echo(NIRI_INTRO if compositor == NIRI else SWAY_INTRO)
        echo(rules_for(compositor))
        return 0

    echo('# luks-tray did not recognize this desktop session.')
    echo('# If dialogs open tiled, add the rule for your compositor:')
    echo('')
    echo(NIRI_INTRO)
    echo(NIRI_RULE)
    echo('')
    echo(SWAY_INTRO)
    echo(SWAY_RULE)
    return 0
