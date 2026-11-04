"""`install_signal_wakeup()`: a stop signal reaches its handler without a poll tick.

The applet answers a stop by dying of the signal (`Utils.install_termination_log`),
but CPython runs that handler only between bytecodes in the main thread, and a
tray spends its life inside `QApplication.exec()`.  Without the wakeup pipe the
handler ran up to one 3 s poll late -- measured against journald, a stop job
accepted at 13:16:48 produced its `(signal 15)` line at 13:16:50 -- which is
exactly the window `TimeoutStopSec=3` exists to cover.

These tests pin the mechanism, not a GUI: `signal.set_wakeup_fd()` is what puts a
byte in the pipe, the notifier's slot is what empties it, and the handler that
decides the exit status is still the one `install_termination_log()` installs.
"""

import os
import signal

import pytest
from PyQt6.QtWidgets import QApplication

from luks_tray import Main


@pytest.fixture
def app():
    """The one `QApplication` for this process (conftest.py asks for offscreen)."""
    return QApplication.instance() or QApplication([])


def test_a_signal_lands_in_the_wakeup_pipe(app):
    # set_wakeup_fd() writes the signal number into the pipe from the C-level
    # handler; a byte appearing there is the whole mechanism -- the notifier
    # then makes the loop return to Python, which runs the handler.
    _, read_fd, _write_fd = Main.install_signal_wakeup(app)
    seen = []
    previous = signal.signal(signal.SIGUSR1, lambda *_: seen.append(1))
    try:
        signal.raise_signal(signal.SIGUSR1)
        assert os.read(read_fd, 8)
        assert seen == [1]
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_draining_the_pipe_leaves_it_empty(app):
    # A notifier that did not drain would fire on every loop iteration, so the
    # slot must consume everything that was written.
    _, read_fd, write_fd = Main.install_signal_wakeup(app)
    os.write(write_fd, b'\x0f' * 4)
    Main._drain_signal_pipe(read_fd)
    with pytest.raises(BlockingIOError):
        os.read(read_fd, 8)


def test_a_second_call_reuses_the_pipe(app):
    # install_signal_wakeup() runs from LuksTray.__init__; a second tray in one
    # process (a test, a takeover) must not replace the pipe the loop is
    # watching, which would leave the handlers deaf again.
    assert Main.install_signal_wakeup(app) is Main.install_signal_wakeup(app)
