"""Main window: list of projects + create project."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient, ApiError
from app.auth_store import AuthStore
from app.windows.project_detail import ProjectDetailWindow


class LoadProjectsThread(QThread):
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, api: ApiClient) -> None:
        super().__init__()
        self.api = api

    def run(self) -> None:
        try:
            self.finished.emit(self.api.list_projects())
        except ApiError as exc:
            self.error.emit(str(exc.detail or exc.status_code))


class MainWindow(QWidget):
    def __init__(self, api: ApiClient, auth: AuthStore, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.auth = auth
        self.setWindowTitle("AI Dev Department — проекты")
        self.resize(900, 560)

        layout = QVBoxLayout(self)

        # Top bar: refresh + create + logout
        top = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить")
        self.create_btn = QPushButton("Создать проект")
        self.logout_btn = QPushButton("Выйти")
        top.addWidget(self.refresh_btn)
        top.addWidget(self.create_btn)
        top.addStretch(1)
        top.addWidget(self.logout_btn)
        layout.addLayout(top)

        # Search
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию…")
        layout.addWidget(self.search)

        # Table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Название", "Статус", "Репозиторий", "Создан"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._open_selected)
        layout.addWidget(self.table)

        # Bottom actions
        bottom = QHBoxLayout()
        self.open_btn = QPushButton("Открыть проект")
        self.start_btn = QPushButton("Запустить")
        self.stop_btn = QPushButton("Остановить")
        self.delete_btn = QPushButton("Удалить")
        bottom.addWidget(self.open_btn)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.delete_btn)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.refresh_btn.clicked.connect(self.load_projects)
        self.create_btn.clicked.connect(self._create_project)
        self.open_btn.clicked.connect(self._open_selected)
        self.start_btn.clicked.connect(lambda: self._action_project("start"))
        self.stop_btn.clicked.connect(lambda: self._action_project("stop"))
        self.delete_btn.clicked.connect(self._delete_selected)
        self.logout_btn.clicked.connect(self._logout)
        self.search.textChanged.connect(self._filter)

        self._projects: list[dict] = []
        self._detail_window: ProjectDetailWindow | None = None

        self.load_projects()

    # ---------------------------------------------------------------- data

    def load_projects(self) -> None:
        self.refresh_btn.setEnabled(False)
        self._thread = LoadProjectsThread(self.api)
        self._thread.finished.connect(self._on_loaded)
        self._thread.error.connect(self._on_error)
        self._thread.finished.connect(lambda: self.refresh_btn.setEnabled(True))
        self._thread.start()

    def _on_loaded(self, projects: list[dict]) -> None:
        self._projects = projects
        self._populate_table(projects)

    def _on_error(self, message: str) -> None:
        QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить проекты: {message}")
        self.refresh_btn.setEnabled(True)

    def _populate_table(self, projects: list[dict]) -> None:
        self.table.setRowCount(0)
        for p in projects:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(p["id"])))
            self.table.setItem(row, 1, QTableWidgetItem(p["name"]))
            self.table.setItem(row, 2, QTableWidgetItem(p["status"]))
            self.table.setItem(row, 3, QTableWidgetItem(p.get("github_repo", "")))
            self.table.setItem(row, 4, QTableWidgetItem(str(p.get("created_at", ""))[:10]))

    def _filter(self, text: str) -> None:
        text = text.lower()
        for row in range(self.table.rowCount()):
            match = text in (
                (self.table.item(row, 1).text() if self.table.item(row, 1) else "")
                .lower()
            )
            self.table.setRowHidden(row, not match)

    def _selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return int(item.text()) if item else None

    # ---------------------------------------------------------------- actions

    def _open_selected(self) -> None:
        pid = self._selected_id()
        if pid is None:
            QMessageBox.information(self, "Внимание", "Выберите проект в списке.")
            return
        self._open_project(pid)

    def _open_project(self, pid: int) -> None:
        try:
            project = self.api.get_project(pid)
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось загрузить проект: {exc.detail or exc.status_code}"
            )
            return
        self._detail_window = ProjectDetailWindow(self.api, project, self)
        self._detail_window.show()

    def _create_project(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout

        dlg = QDialog(self)
        dlg.setWindowTitle("Создать проект")
        dlg.setMinimumWidth(450)
        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        name = QLineEdit()
        description = QLineEdit()
        task = QLineEdit()
        repo = QLineEdit()
        repo.setPlaceholderText("owner/name")
        form.addRow("Название:", name)
        form.addRow("Описание:", description)
        form.addRow("Задача:", task)
        form.addRow("Репозиторий (GitHub):", repo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(buttons)

        def do_create() -> None:
            if not name.text().strip() or not task.text().strip() or not repo.text().strip():
                QMessageBox.warning(dlg, "Внимание", "Заполните название, задачу и репозиторий.")
                return
            try:
                self.api.create_project(
                    name=name.text().strip(),
                    description=description.text().strip(),
                    task=task.text().strip(),
                    github_repo=repo.text().strip(),
                )
                dlg.accept()
                self.load_projects()
            except ApiError as exc:
                QMessageBox.critical(
                    dlg, "Ошибка",
                    f"Не удалось создать проект: {exc.detail or exc.status_code}",
                )

        buttons.accepted.connect(do_create)
        buttons.rejected.connect(dlg.reject)
        dlg.exec()

    def _action_project(self, action: str) -> None:
        pid = self._selected_id()
        if pid is None:
            QMessageBox.information(self, "Внимание", "Выберите проект в списке.")
            return
        try:
            if action == "start":
                self.api.start_project(pid)
            else:
                self.api.stop_project(pid)
            self.load_projects()
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось {action}ить проект: {exc.detail or exc.status_code}"
            )

    def _delete_selected(self) -> None:
        pid = self._selected_id()
        if pid is None:
            QMessageBox.information(self, "Внимание", "Выберите проект в списке.")
            return
        reply = QMessageBox.question(
            self, "Удаление",
            "Удалить выбранный проект? Это действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.delete_project(pid)
            self.load_projects()
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось удалить проект: {exc.detail or exc.status_code}"
            )

    def _logout(self) -> None:
        self.auth.clear()
        self.close()
