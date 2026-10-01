"""PyInstaller entry point for the windowed app (gui.py uses relative imports)."""

import sys

from session_record.gui import main

sys.exit(main())
