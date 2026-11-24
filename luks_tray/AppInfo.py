#!/usr/bin/env python3
"""Application identity shared by the tray, dialogs and compositor rules."""

APP_NAME = 'luks-tray'
DESKTOP_FILE_NAME = APP_NAME
APPLICATION_DISPLAY_NAME = 'LUKS Tray'
APP_TITLE_SUFFIX = f' [{APP_NAME}]'


def window_title(purpose):
    """Return the window title for `purpose`, including the app suffix.

    Args:
        purpose: Dialog purpose, with or without the suffix.

    Returns:
        str: The window title.
    """
    purpose = (purpose or '').strip()
    if purpose.endswith(APP_TITLE_SUFFIX):
        return purpose
    return f'{purpose}{APP_TITLE_SUFFIX}'


def heading_text(purpose):
    """Return `purpose` without the window-title suffix.

    Args:
        purpose: Dialog purpose, with or without the suffix.

    Returns:
        str: The text to use as the in-dialog heading.
    """
    purpose = (purpose or '').strip()
    if purpose.endswith(APP_TITLE_SUFFIX):
        purpose = purpose[:-len(APP_TITLE_SUFFIX)].rstrip()
    return purpose
