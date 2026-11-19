"""Pytest fixtures.

Isolated environment, set BEFORE any app module is imported:
- temp SQLite database
- VNC_HEADLESS=true (no Xvfb/x11vnc needed)
- deterministic ADMIN_PASSWORD
- example.com as SERVICE_URL (never contacted: Camoufox is faked)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="aidd-test-"))

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'test.db'}"
os.environ["VNC_HEADLESS"] = "true"
os.environ["ADMIN_PASSWORD"] = "admin-test-pass"
os.environ["SERVICE_URL"] = "https://example.com"
os.environ["PROXY_URL"] = "http://user:pass@1.2.3.4:8080"  # parsed, never dialed (fake browser)
os.environ["NOVNC_PUBLIC_BASE_URL"] = "http://127.0.0.1:6080"

os.environ["TEST_DB_PATH"] = str(_TMP / "test.db")
