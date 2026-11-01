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
