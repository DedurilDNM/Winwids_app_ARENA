"""«Аккаунты» tab: list of the user's service accounts + manual VNC login.

Add flow (button «Добавить аккаунт»):
    1. POST /api/accounts                   -> account (status pending_login)
    2. POST /api/accounts/{id}/vnc/start    -> noVNC URL (session is reused if
                                               create already started it)
    3. modal VncLoginDialog                 -> user logs in by hand
    4. «Я вошёл / Готово» -> POST /api/accounts/{id}/confirm-login -> refresh
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient, ApiError
from app.widgets.api_worker import run_in_thread
from app.windows.vnc_view import VncLoginDialog

# Mirrors server/app/schemas.py (RoleName, AccountStatus, MAX_ACCOUNTS_PER_USER).
ROLES = ["Coder", "Architect", "Reviewer", "Tester", "Integrator", "Orchestrator"]
MAX_ACCOUNTS = 10
STATUS_LABELS = {
    "pending_login": ("Ожидает входа", "#c98a00"),
    "active": ("Активен", "#2e8b57"),
    "relogin_needed": ("Нужен повторный вход", "#c0392b"),
    "disabled": ("Отключён", "#7f8c8d"),
}
# Statuses for which «Войти (VNC)» makes sense.
LOGIN_STATUSES = {"pending_login", "relogin_needed", "active"}

COLUMNS = ["ID", "Название", "Роль", "Статус", "Репозитории", "Заметка", "Проверка", "Создан"]


class AccountDialog(QDialog):
    """Create-account form. The service password is NOT asked here — the
    user types it into the remote browser over VNC."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить аккаунт")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Например: coder-1")
        self.name.setMaxLength(120)
        self.role = QComboBox()
        self.role.addItems(ROLES)
        self.repos = QLineEdit()
        self.repos.setPlaceholderText("owner/repo, owner/other (необязательно)")
        self.note = QLineEdit()
        self.note.setMaxLength(255)
        self.note.setPlaceholderText("Заметка (необязательно)")
        form.addRow("Название:", self.name)
        form.addRow("Роль по умолчанию:", self.role)
        form.addRow("Репозитории GitHub:", self.repos)
        form.addRow("Заметка:", self.note)
        layout.addLayout(form)

        info = QLabel(
            "После создания откроется окно VNC с браузером сервера — "
            "войдите в сервис там вручную."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888;")
        layout.addWidget(info)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать и войти")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Внимание", "Укажите название аккаунта.")
            return
        bad = [r for r in self.repo_list() if "/" not in r]
        if bad:
            QMessageBox.warning(
                self, "Внимание", f"Репозиторий должен быть в формате owner/name: {', '.join(bad)}"
            )
            return
        self.accept()

    def repo_list(self) -> list[str]:
        raw = self.repos.text().replace(";", ",").replace(" ", ",")
        return [r.strip() for r in raw.split(",") if r.strip()]

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(),
            "default_role": self.role.currentText(),
            "github_repos": self.repo_list(),
            "service_note": self.note.text().strip(),
        }


class AccountsTab(QWidget):
    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self._accounts: list[dict] = []
        self._loading = False

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить")
        self.add_btn = QPushButton("Добавить аккаунт")
        self.add_btn.setStyleSheet("font-weight: bold;")
        self.counter = QLabel("")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.add_btn)
        top.addStretch(1)
        top.addWidget(self.counter)
        layout.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._login_selected)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.login_btn = QPushButton("Войти (VNC)")
        self.login_btn.setToolTip("Открыть браузер сервера для (повторного) входа в аккаунт")
        self.toggle_btn = QPushButton("Отключить")
        self.delete_btn = QPushButton("Удалить")
        bottom.addWidget(self.login_btn)
        bottom.addWidget(self.toggle_btn)
        bottom.addWidget(self.delete_btn)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.refresh_btn.clicked.connect(self.load_accounts)
        self.add_btn.clicked.connect(self._add_account)
        self.login_btn.clicked.connect(self._login_selected)
        self.toggle_btn.clicked.connect(self._toggle_selected)
        self.delete_btn.clicked.connect(self._delete_selected)

        self._update_buttons()
        self.load_accounts()

    # ------------------------------------------------------------------ data

    def load_accounts(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.setEnabled(False)
        run_in_thread(self.api.list_accounts, self._on_loaded, self._on_load_error)

    def _on_loaded(self, accounts: list[dict]) -> None:
        self._loading = False
        self.refresh_btn.setEnabled(True)
        selected = self._selected_id()
        self._accounts = accounts
        self._populate(accounts)
        if selected is not None:
            self._select_id(selected)
        self._update_buttons()

    def _on_load_error(self, exc: ApiError) -> None:
        self._loading = False
        self.refresh_btn.setEnabled(True)
        QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить аккаунты: {exc.message}")

    def _populate(self, accounts: list[dict]) -> None:
        self.table.setRowCount(0)
        for acc in accounts:
            row = self.table.rowCount()
            self.table.insertRow(row)
            label, color = STATUS_LABELS.get(acc.get("status", ""), (acc.get("status", ""), None))
            status_item = QTableWidgetItem(label)
            if color:
                status_item.setForeground(QColor(color))
            values = [
                QTableWidgetItem(str(acc["id"])),
                QTableWidgetItem(acc.get("name", "")),
                QTableWidgetItem(acc.get("default_role", "")),
                status_item,
                QTableWidgetItem(", ".join(acc.get("github_repos") or [])),
                QTableWidgetItem(acc.get("service_note", "")),
                QTableWidgetItem(_fmt_dt(acc.get("last_health_check"))),
                QTableWidgetItem(_fmt_dt(acc.get("created_at"))[:10]),
            ]
            values[0].setData(Qt.ItemDataRole.UserRole, acc["id"])
            for col, item in enumerate(values):
                self.table.setItem(row, col, item)

        self.counter.setText(f"Аккаунтов: {len(accounts)} / {MAX_ACCOUNTS}")
        self.add_btn.setEnabled(len(accounts) < MAX_ACCOUNTS)
        self.add_btn.setToolTip(
            "" if len(accounts) < MAX_ACCOUNTS else f"Достигнут лимит: {MAX_ACCOUNTS} аккаунтов"
        )

    def _selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_account(self) -> dict | None:
        aid = self._selected_id()
        return next((a for a in self._accounts if a["id"] == aid), None)

    def _select_id(self, account_id: int) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == account_id:
                self.table.selectRow(row)
                return

    def _update_buttons(self) -> None:
        acc = self._selected_account()
        has = acc is not None
        self.login_btn.setEnabled(has and acc.get("status") in LOGIN_STATUSES)
        self.delete_btn.setEnabled(has)
        self.toggle_btn.setEnabled(has)
        self.toggle_btn.setText(
            "Включить" if has and acc.get("status") == "disabled" else "Отключить"
        )

    def _set_busy(self, busy: bool) -> None:
        for btn in (self.add_btn, self.login_btn, self.toggle_btn, self.delete_btn):
            btn.setEnabled(not busy)
        if not busy:
            self.add_btn.setEnabled(len(self._accounts) < MAX_ACCOUNTS)
            self._update_buttons()

    # --------------------------------------------------------------- actions

    def _add_account(self) -> None:
        dlg = AccountDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()

        def create_and_start() -> tuple[dict, dict | None, ApiError | None]:
            account = self.api.create_account(**values)
            try:
                vnc = self.api.vnc_start(account["id"])
            except ApiError as exc:
                # Account exists already; report VNC failure separately.
                return account, None, exc
            return account, vnc, None

        self._set_busy(True)
        run_in_thread(create_and_start, self._on_created, self._on_action_error("создать аккаунт"))

    def _on_created(self, result: tuple[dict, dict | None, ApiError | None]) -> None:
        self._set_busy(False)
        account, vnc, vnc_error = result
        if vnc is None:
            QMessageBox.warning(
                self,
                "VNC недоступен",
                f"Аккаунт «{account['name']}» создан, но VNC-сессию запустить не удалось:\n"
                f"{vnc_error.message if vnc_error else 'неизвестная ошибка'}\n\n"
                "Повторите вход позже кнопкой «Войти (VNC)».",
            )
            self.load_accounts()
            return
        self._open_login_dialog(account, vnc)

    def _login_selected(self) -> None:
        acc = self._selected_account()
        if acc is None:
            QMessageBox.information(self, "Внимание", "Выберите аккаунт в списке.")
            return
        if acc.get("status") not in LOGIN_STATUSES:
            QMessageBox.information(
                self, "Внимание", "Аккаунт отключён. Сначала включите его."
            )
            return
        self._set_busy(True)
        run_in_thread(
            lambda: self.api.vnc_start(acc["id"]),
            lambda vnc: (self._set_busy(False), self._open_login_dialog(acc, vnc)),
            self._on_action_error("запустить VNC"),
        )

    def _open_login_dialog(self, account: dict, vnc: dict) -> None:
        dlg = VncLoginDialog(self.api, account, vnc, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            QMessageBox.information(
                self, "Готово", f"Вход в аккаунт «{account['name']}» подтверждён, сессия сохранена."
            )
        self.load_accounts()

    def _toggle_selected(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        # Re-enabling goes through pending_login: "active" is set by confirm-login only.
        new_status = "pending_login" if acc.get("status") == "disabled" else "disabled"
        self._set_busy(True)
        run_in_thread(
            lambda: self.api.update_account(acc["id"], status=new_status),
            lambda _res: (self._set_busy(False), self.load_accounts()),
            self._on_action_error("изменить статус аккаунта"),
        )

    def _delete_selected(self) -> None:
        acc = self._selected_account()
        if acc is None:
            return
        reply = QMessageBox.question(
            self,
            "Удаление",
            f"Удалить аккаунт «{acc['name']}»?\n"
            "Сохранённая сессия и браузер на сервере будут уничтожены, "
            "аккаунт будет отвязан от проектов.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        run_in_thread(
            lambda: self.api.delete_account(acc["id"]),
            lambda _res: (self._set_busy(False), self.load_accounts()),
            self._on_action_error("удалить аккаунт"),
        )

    def _on_action_error(self, what: str):
        def handler(exc: ApiError) -> None:
            self._set_busy(False)
            QMessageBox.critical(self, "Ошибка", f"Не удалось {what}: {exc.message}")
            self.load_accounts()

        return handler


def _fmt_dt(value) -> str:
    if not value:
        return "—"
    return str(value).replace("T", " ")[:19]
