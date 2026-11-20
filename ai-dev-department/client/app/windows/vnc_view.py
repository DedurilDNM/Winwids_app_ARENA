"""Embedded noVNC viewer + modal "manual login" dialog.

The server starts the external service in a real browser on its Xvfb display
and exposes it through websockify/noVNC. The user logs in by hand inside that
remote browser — the service password never touches this client or the API.

- `VncView`        — QWebEngineView-based widget that opens a noVNC URL.
- `VncLoginDialog` — modal window with the viewer and the
                     «Я вошёл / Готово» button (POST /confirm-login).
- `normalize_vnc_url()` — accepts what the server returns (vnc.html page URL,
                     relative path, or a raw ws:// / wss:// websockify URL)
                     and turns it into a page URL a browser can open.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient, ApiError
from app.widgets.api_worker import run_in_thread

try:  # QtWebEngine is a separate, heavy Qt module; degrade gracefully without it.
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
    WEBENGINE_IMPORT_ERROR = ""
except ImportError as _exc:  # pragma: no cover - depends on the installed Qt build
    WEBENGINE_AVAILABLE = False
    WEBENGINE_IMPORT_ERROR = str(_exc)


_NOVNC_DEFAULT_PARAMS = {"autoconnect": "true", "resize": "scale", "reconnect": "true"}


def normalize_vnc_url(url: str, api_base_url: str = "") -> str:
    """Return an http(s) URL of the noVNC page.

    * ``https://host/novnc/vnc.html?token=..`` -> unchanged (+ default params)
    * ``/novnc/vnc.html?token=..``             -> resolved against api_base_url
    * ``wss://host/novnc/websockify?token=..`` -> ``https://host/novnc/vnc.html
      ?host=host&port=443&path=novnc/websockify?token=..&encrypt=1``
      (websockify serves the noVNC web root on the same port, see vnc_manager)
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Пустой адрес VNC")

    parts = urlsplit(url)
    if not parts.scheme:
        if not api_base_url:
            raise ValueError(f"Относительный адрес VNC без базового URL: {url}")
        url = urljoin(api_base_url.rstrip("/") + "/", url.lstrip("/"))
        parts = urlsplit(url)

    scheme = parts.scheme.lower()
    if scheme in ("ws", "wss"):
        secure = scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        ws_path = parts.path.lstrip("/") or "websockify"
        if parts.query:
            ws_path = f"{ws_path}?{parts.query}"
        # vnc.html lives next to the websockify endpoint
        prefix = parts.path.rsplit("/", 1)[0] if "/" in parts.path.strip("/") else ""
        page_path = f"{prefix}/vnc.html" if prefix else "/vnc.html"
        query = dict(_NOVNC_DEFAULT_PARAMS)
        query.update(
            {"host": host, "port": str(port), "path": ws_path, "encrypt": "1" if secure else "0"}
        )
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit(("https" if secure else "http", netloc, page_path, urlencode(query), ""))

    if scheme not in ("http", "https"):
        raise ValueError(f"Неподдерживаемая схема адреса VNC: {parts.scheme}")

    query = dict(_NOVNC_DEFAULT_PARAMS)
    query.update(dict(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/vnc.html", urlencode(query), parts.fragment))


def _safe_for_display(url: str) -> str:
    """Hide the VNC token (a short-lived JWT) in anything shown on screen."""
    parts = urlsplit(url)
    query = [
        (k, "***" if k.lower() in ("token", "password") else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class VncView(QWidget):
    """Embedded browser showing the noVNC page. Falls back to a "open in
    system browser" button when QtWebEngine is not installed."""

    loaded = Signal(bool)

    def __init__(self, url: str | None = None, api_base_url: str = "", parent=None) -> None:
        super().__init__(parent)
        self._api_base_url = api_base_url
        self._url: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #888;")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        if WEBENGINE_AVAILABLE:
            self.web = QWebEngineView(self)
            settings = self.web.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            # noVNC clipboard panel + pasting into the remote browser
            for name in ("JavascriptCanAccessClipboard", "JavascriptCanPaste"):
                attr = getattr(QWebEngineSettings.WebAttribute, name, None)
                if attr is not None:
                    settings.setAttribute(attr, True)
            # Never keep the noVNC page (with its token) in a disk cache.
            self.web.page().profile().setHttpCacheType(
                QWebEngineProfile.HttpCacheType.MemoryHttpCache
            )
            self.web.loadStarted.connect(lambda: self.status.setText("Подключение к VNC…"))
            self.web.loadFinished.connect(self._on_load_finished)
            self.web.page().renderProcessTerminated.connect(self._on_render_crash)
            layout.addWidget(self.web, 1)
            self._fallback = None
        else:
            self.web = None
            self._fallback = QLabel(
                "Встроенный браузер (QtWebEngine) недоступен:\n"
                f"{WEBENGINE_IMPORT_ERROR}\n\n"
                "Откройте VNC в системном браузере кнопкой ниже, "
                "выполните вход и вернитесь сюда."
            )
            self._fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._fallback.setWordWrap(True)
            layout.addWidget(self._fallback, 1)

        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        self.reload_btn = QPushButton("Переподключить")
        self.external_btn = QPushButton("Открыть в браузере")
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.external_btn)
        layout.addLayout(bar)

        self.reload_btn.clicked.connect(self.reload)
        self.external_btn.clicked.connect(self.open_external)

        if url:
            self.load(url)
        else:
            self._set_enabled(False)

    # ------------------------------------------------------------------ api

    @property
    def url(self) -> str | None:
        return self._url

    def load(self, url: str) -> None:
        try:
            self._url = normalize_vnc_url(url, self._api_base_url)
        except ValueError as exc:
            self._url = None
            self.status.setText(str(exc))
            self._set_enabled(False)
            return
        self._set_enabled(True)
        self.status.setText(f"VNC: {_safe_for_display(self._url)}")
        if self.web is not None:
            self.web.setUrl(QUrl(self._url))
            self.web.setFocus()

    def reload(self) -> None:
        if self._url and self.web is not None:
            self.web.setUrl(QUrl(self._url))
        elif self._url:
            self.open_external()

    def open_external(self) -> None:
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))

    def stop(self) -> None:
        """Drop the websocket connection (navigating away closes noVNC)."""
        if self.web is not None:
            self.web.stop()
            self.web.setUrl(QUrl("about:blank"))

    # ------------------------------------------------------------- internals

    def _set_enabled(self, enabled: bool) -> None:
        self.reload_btn.setEnabled(enabled)
        self.external_btn.setEnabled(enabled)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self.status.setText("VNC подключён. Войдите в аккаунт в окне браузера сервера.")
        else:
            self.status.setText(
                "Не удалось открыть страницу noVNC. Проверьте NOVNC_PUBLIC_BASE_URL "
                "на сервере и нажмите «Переподключить»."
            )
        self.loaded.emit(ok)

    def _on_render_crash(self, *_args) -> None:
        self.status.setText("Процесс встроенного браузера упал — нажмите «Переподключить».")


class VncLoginDialog(QDialog):
    """Modal window: noVNC viewer + «Я вошёл / Готово».

    The VNC session must already be started (POST /vnc/start); pass its
    response as `vnc`. On «Готово» calls POST /api/accounts/{id}/confirm-login
    and accepts the dialog. On cancel stops the noVNC path (browser on the
    server stays alive; the account stays in `pending_login`).
    """

    def __init__(
        self,
        api: ApiClient,
        account: dict,
        vnc: dict,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self.account = account
        self.confirmed_account: dict | None = None
        self._busy = False

        self.setWindowTitle(f"Вход в аккаунт: {account.get('name', account['id'])}")
        self.setModal(True)
        self.resize(1280, 820)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Войдите в сервис вручную в окне ниже (логин/пароль вводятся прямо "
            "в браузере сервера и не передаются в приложение). "
            "Когда вход выполнен — нажмите «Я вошёл / Готово»."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        vnc_url = vnc.get("vnc_url")
        if vnc.get("headless") or not vnc_url:
            # Test/CI server mode (VNC_HEADLESS=true): there is nothing to show.
            self.view = None
            notice = QLabel(
                "Сервер работает в headless-режиме (VNC отключён). "
                "Ручной вход невозможен; «Готово» сохранит текущую сессию браузера."
            )
            notice.setWordWrap(True)
            notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(notice, 1)
        else:
            self.view = VncView(vnc_url, api_base_url=api.base_url, parent=self)
            layout.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        self.restart_btn = QPushButton("Перезапустить VNC")
        self.cancel_btn = QPushButton("Отмена")
        self.done_btn = QPushButton("Я вошёл / Готово")
        self.done_btn.setDefault(True)
        self.done_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        buttons.addWidget(self.restart_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.done_btn)
        layout.addLayout(buttons)

        self.restart_btn.setEnabled(self.view is not None)
        self.restart_btn.clicked.connect(self._restart_vnc)
        self.cancel_btn.clicked.connect(self.reject)
        self.done_btn.clicked.connect(self._confirm)

    # --------------------------------------------------------------- actions

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self._busy = busy
        self.done_btn.setEnabled(not busy)
        self.restart_btn.setEnabled(not busy and self.view is not None)
        self.cancel_btn.setEnabled(not busy)
        if text and self.view is not None:
            self.view.status.setText(text)

    def _confirm(self) -> None:
        self._set_busy(True, "Сохраняю сессию…")
        account_id = self.account["id"]
        run_in_thread(
            lambda: self.api.confirm_login(account_id),
            self._on_confirmed,
            self._on_confirm_failed,
        )

    def _on_confirmed(self, account: dict) -> None:
        self._set_busy(False)
        self.confirmed_account = account
        if self.view is not None:
            self.view.stop()  # server already closed the noVNC path
        self.accept()

    def _on_confirm_failed(self, exc: ApiError) -> None:
        self._set_busy(False)
        QMessageBox.critical(
            self,
            "Ошибка",
            f"Не удалось подтвердить вход: {exc.message}\n\n"
            "Если сессия браузера потеряна — нажмите «Перезапустить VNC».",
        )

    def _restart_vnc(self) -> None:
        self._set_busy(True, "Перезапуск VNC…")
        account_id = self.account["id"]
        run_in_thread(
            lambda: self.api.vnc_start(account_id),
            self._on_restarted,
            self._on_restart_failed,
        )

    def _on_restarted(self, vnc: dict) -> None:
        self._set_busy(False)
        if self.view is not None and vnc.get("vnc_url"):
            self.view.load(vnc["vnc_url"])

    def _on_restart_failed(self, exc: ApiError) -> None:
        self._set_busy(False)
        QMessageBox.critical(self, "Ошибка", f"Не удалось запустить VNC: {exc.message}")

    # ------------------------------------------------------------ lifecycle

    def reject(self) -> None:
        if self._busy:
            return  # do not close mid-request
        if self.view is not None:
            self.view.stop()
        account_id = self.account["id"]
        # Best effort: close the noVNC path, the server browser stays alive so
        # the user can continue later via «Войти (VNC)».
        run_in_thread(lambda: self.api.vnc_stop(account_id))
        super().reject()

    def closeEvent(self, event) -> None:  # window "X" button
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)
