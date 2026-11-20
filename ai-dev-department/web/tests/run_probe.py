"""Run app.tools.adapter_probe against the e2e stack (Chromium instead of Camoufox)."""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_server_patch import ChromiumAsCamoufox  # noqa: E402
from app.services import browser_manager as bm  # noqa: E402
from app.tools import adapter_probe  # noqa: E402

bm.AsyncCamoufox = ChromiumAsCamoufox
parser_args = type("A", (), {"account": None, "url": None, "no_send": False, "headless": os.environ.get("PROBE_HEADLESS") == "1"})()
sys.exit(asyncio.run(adapter_probe.main(parser_args)))
