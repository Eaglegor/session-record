"""PyInstaller entry point for the console tool."""

import sys

from session_record.cli import main

sys.exit(main())
