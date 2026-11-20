"""AI Dev Department client entry point (PySide6).

Usage:
    python -m app.main          (from client/app)
    python main.py              (from client/app)
"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from app.api_client import ApiClient
from app.auth_store import AuthStore
from app.windows.login import LoginWindow
from app.windows.main import MainWindow

DEFAULT_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Dev Department")
    app.setApplicationDisplayName("AI Dev Department Client")

    # One shared AuthStore: ApiClient writes tokens into it on login, and
    # MainWindow clears the very same instance on logout.
    auth = AuthStore()
    api = ApiClient(DEFAULT_BASE_URL, auth)

    login = LoginWindow(api, auth)
    if login.exec() != LoginWindow.DialogCode.Accepted:
        api.close()
        return 0

    window = MainWindow(api, auth)
    window.show()

    code = app.exec()
    api.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
