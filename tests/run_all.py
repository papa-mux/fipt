#!/usr/bin/env python3
"""Запускает все тесты по отдельности (у test_app_stub своя заглушка Textual)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failed = 0
for name in ("test_logic.py", "test_mpv_ipc.py", "test_app_stub.py"):
    print(f"== {name}")
    failed |= subprocess.call([sys.executable, os.path.join(HERE, name)]) != 0
sys.exit(1 if failed else 0)
