#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Development launcher: `python runner.py` starts the tray from the source tree.

    The installed entry point (`luks-tray = luks_tray.main:main`, see
    pyproject.toml) does the same thing; this file exists so the project can be
    run without being installed.
"""

import sys
from luks_tray.main import main

if __name__ == "__main__":
    # Call the main entry point defined inside your package
    sys.exit(main())
