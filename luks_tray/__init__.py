"""luks-tray: a Qt tray app for managing LUKS encrypted volumes.

The release number lives here and nowhere else.  flit_core reads this line at
build time (`dynamic = ["version"]` in pyproject.toml), `luks-tray --version`
prints it, and ./bless takes it from here to decide what to call the upload and
what to tag.  tests/test_metadata.py fails if a second copy appears.
"""

__version__ = "3.0.0"
