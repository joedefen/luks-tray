#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Application identity shared by the tray, the dialogs and compositor rules.

    `APP_NAME` is the stable process name, `DESKTOP_FILE_NAME` is the name Qt
    hands the Wayland compositor as the surface `app_id`, and `window_title()`
    is the one place that appends the suffix the titles have always carried.

    Keeping these values in one module means the dialog headings, the error
    boxes and `--show-window-rules` cannot drift apart: a compositor rule is
    written against `app_id`, not against a title, so the identity has to be
    the same string everywhere.
"""

APP_NAME = 'luks-tray'
DESKTOP_FILE_NAME = APP_NAME
APPLICATION_DISPLAY_NAME = 'LUKS Tray'
APP_TITLE_SUFFIX = f' [{APP_NAME}]'


def window_title(purpose):
    """ Return the window title for `purpose`, with the app suffix.

        The dialog body uses the bare `purpose` (see
        [`CommonDialog.set_title()`](luks_tray/dialogs/Common.py:96)), so a
        compositor that hides decorations loses the suffix but not the
        information.
    """
    purpose = (purpose or '').strip()
    if purpose.endswith(APP_TITLE_SUFFIX):
        return purpose
    return f'{purpose}{APP_TITLE_SUFFIX}'


def heading_text(purpose):
    """ Return `purpose` without the window-title suffix.

        Accepts either a bare purpose ("Mount Crypt File") or a legacy title
        that still carries the suffix, so call sites can be migrated without
        changing what the user sees.
    """
    purpose = (purpose or '').strip()
    if purpose.endswith(APP_TITLE_SUFFIX):
        purpose = purpose[:-len(APP_TITLE_SUFFIX)].rstrip()
    return purpose
