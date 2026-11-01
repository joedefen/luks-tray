"""Qt's own console output: what reaches the log, and what is dropped.

Qt talks to its process through a message handler, not through `print()`.  On
a desktop that uses the Qt6CT platform theme the plugin prints its whole
palette table on every widget it is asked about, and Qt's layout code warns
about a null widget that a *plugin* handed it -- neither has anything to do
with luks-tray, and both used to land in `debug.log` between the lines a
reader actually wanted, in a place where they looked like our own bugs.

`install_qt_messages()` puts a handler in front of that stream: debug and
information levels are dropped, the theme's palette chatter is dropped, and a
warning is kept -- once per distinct text -- stamped with the `[file:line]` of
the C++ side Qt reports.  That footer is the point: it says where to look.

These tests call the handler directly.  Installing it would capture the
warnings of the whole test session, and the handler is a plain function.
"""

from types import SimpleNamespace

from PyQt6.QtCore import QtMsgType

from luks_tray import Main

PALETTE = ('virtual const QPalette* Qt6CTPlatformTheme::palette('
           'QPlatformTheme::Palette) const')


def context(file='/home/joe/src/qtbase/src/gui/painting/qpainter.cpp', line=42):
    """The `QMessageLogContext` Qt passes: the C++ file and line of the message."""
    return SimpleNamespace(file=file, line=line, function='drawText')


def test_debug_and_information_messages_are_dropped(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(QtMsgType.QtDebugMsg, context(), 'painter is sad')
    Main.qt_message_handler(QtMsgType.QtInfoMsg, context(), 'device pixel ratio')

    assert capsys.readouterr().out == ''


def test_the_theme_plugin_palette_table_is_dropped(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    # This one arrives at warning level on some Qt builds, so it is matched by
    # text as well as by level.
    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(), PALETTE)

    assert capsys.readouterr().out == ''


def test_the_qt5_form_of_that_plugin_is_dropped_too(capsys, monkeypatch):
    # This applet is a Qt6 one, but the same plugin exists for Qt5 (qt5ct) and
    # a log is often read against both; the marker is the part of the class
    # name the two spellings share.
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(
        QtMsgType.QtWarningMsg, context(),
        PALETTE.replace('Qt6CTPlatformTheme', 'Qt5CTPlatformTheme'))

    assert capsys.readouterr().out == ''


def test_the_shutdown_thread_storage_notice_is_dropped(capsys, monkeypatch):
    # Printed once, at exit, with a fresh address each run: dropping it by
    # prefix is the only way a reader stops meeting it in the log.
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(
        QtMsgType.QtWarningMsg, context(file=None, line=0),
        'QThreadStorage: Thread 0x55e6bec0b700 exited after QThreadStorage 8'
        ' destroyed')

    assert capsys.readouterr().out == ''


def test_the_portal_registration_failure_is_dropped(capsys, monkeypatch):
    # Verbatim from a real start on niri under Qt 6.12.  Naming a desktop file
    # for the Wayland app_id is what keeps the surface app_id at `luks-tray`,
    # and an app_id without a matching installed entry is something the portal
    # rejects; nothing here uses a portal feature, so the line is stable noise.
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(
        QtMsgType.QtWarningMsg, context(file=None, line=0),
        'Failed to register with host portal QDBusError('
        '"org.freedesktop.portal.Error.Failed", "Could not register app ID: '
        "App info not found for 'luks-tray'\")")

    assert capsys.readouterr().out == ''


def test_a_portal_problem_that_is_not_the_registration_is_kept(
        capsys, monkeypatch):
    # The marker is the registration call, not the word "portal": a portal
    # failure that would matter (a file chooser, say) has to reach the log.
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(file=None, line=0),
                            'Failed to open a portal document chooser')

    assert 'portal document chooser' in capsys.readouterr().out


def test_a_warning_is_kept_with_the_origin_qt_reported(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'bad paint engine')

    out = capsys.readouterr().out
    assert 'qt: bad paint engine' in out
    # The footer is Qt's file and line, not a line of this package: the reader
    # has to be able to see that the message is not ours.
    assert '[qpainter.cpp:42]' in out
    assert '[Main.py' not in out


def test_a_warning_is_reported_once_not_on_every_repaint(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    for _ in range(3):
        Main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'bad paint')
    out = capsys.readouterr().out

    assert out.count('bad paint') == 1


def test_a_different_warning_still_gets_through(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'first')
    Main.qt_message_handler(QtMsgType.QtCriticalMsg, context(), 'second')

    out = capsys.readouterr().out
    assert 'qt: first' in out
    assert 'qt: second' in out


def test_a_message_without_a_file_still_gets_a_footer(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    # Release builds of Qt are compiled without QT_MESSAGELOGCONTEXT, so the
    # context arrives empty; the line must still say it came from Qt.
    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(file=None, line=0),
                            'anonymous complaint')

    out = capsys.readouterr().out
    assert 'qt: anonymous complaint' in out
    assert '[qt]' in out


def test_an_empty_message_is_ignored(capsys, monkeypatch):
    monkeypatch.setattr(Main, '_qt_seen', set())

    Main.qt_message_handler(QtMsgType.QtWarningMsg, context(), '   ')

    assert capsys.readouterr().out == ''


def test_install_registers_that_handler(monkeypatch):
    recorded = []
    monkeypatch.setattr(Main, 'qInstallMessageHandler', recorded.append)

    Main.install_qt_messages()

    assert recorded == [Main.qt_message_handler]


def test_the_app_name_is_the_one_every_other_piece_uses(capsys):
    # The session banner, the unit file and the menu all name the applet; a
    # second spelling of it would show up in the log as another program.
    assert Main.APP_NAME == 'luks-tray'
