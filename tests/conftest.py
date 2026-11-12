"""Shared test setup. Qt runs offscreen so GUI tests need no display."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
