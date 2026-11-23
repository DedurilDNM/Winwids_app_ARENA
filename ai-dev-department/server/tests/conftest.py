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
os.environ["GITHUB_TOKEN"] = ""  # never use a real token from the developer environment
os.environ["AUTO_RESUME_INTERRUPTED"] = "false"  # tests decide themselves (see test_projects)
os.environ["ADAPTER_MIN_NAV_INTERVAL_SECONDS"] = "0"  # no pacing between page loads in tests (own tests below)

os.environ["TEST_DB_PATH"] = str(_TMP / "test.db")


# Reset the in-memory rate limiter before every test so login/register rate
# limits don't accumulate across test files in one pytest session.
import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.deps import limiter

    limiter._storage.reset()
