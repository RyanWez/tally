#!/usr/bin/env python3
"""Entrypoint wrapper for Tally Bot.

Delegates execution to main.py for full functionality.
"""

import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import main

if __name__ == "__main__":
    sys.exit(main.main())
