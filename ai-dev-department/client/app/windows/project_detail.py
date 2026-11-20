"""Project detail window: timeline of tasks + start/stop."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.api_client import ApiClient, ApiError


class ProjectDetailWindow(QWidget):
    def __init__(self, api: ApiClient, project: dict, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.project = project
        self.setWindowTitle(f"Проект: {project['name']}")
        self.resize(850, 600)

        layout = QVBoxLayout(self)

        # Header info
        info = QVBoxLayout()
        title = QLabel(f"<b>{project['name']}</b>")
        title.setStyleSheet("font-size: 18px;")
        info.addWidget(title)
        info.addWidget(QLabel(f"<b>Задача:</b> {project.get('task', '-')}"))
        info.addWidget(QLabel(f"<b>Репозиторий:</b> {project.get('github_repo', '-')}"))
        info.addWidget(QLabel(f"<b>Статус:</b> <span id='status'>{project.get('status', '-')}</span>"))
        self.status_label = info.itemAt(info.count() - 1).widget()
        info.addWidget(QLabel(f"<b>Результат:</b> {project.get('result') or '—'}"))
        layout.addLayout(info)

        # Actions
        actions = QHBoxLayout()
        self.start_btn = QPushButton("Запустить")
        self.stop_btn = QPushButton("Остановить")
        self.refresh_btn = QPushButton("Обновить")
        actions.addWidget(self.start_btn)
        actions.addWidget(self.stop_btn)
        actions.addWidget(self.refresh_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._update_buttons(project.get("status"))

        self.start_btn.clicked.connect(lambda: self._action("start"))
        self.stop_btn.clicked.connect(lambda: self._action("stop"))
        self.refresh_btn.clicked.connect(self.reload)

        # Tasks table
        layout.addWidget(QLabel("<b>Задачи (таймлайн):</b>"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Роль", "Задача", "Исполнитель", "Статус", "Ветка"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        self.reload()

    # ---------------------------------------------------------------- data

    def reload(self) -> None:
        self.refresh_btn.setEnabled(False)
        try:
            project = self.api.get_project(self.project["id"])
            self.project = project
            self.status_label.setText(f"<b>{project.get('status', '-')}</b>")
            self._update_buttons(project.get("status"))
            self._populate_tasks(project.get("tasks", []))
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось загрузить проект: {exc.message}"
            )
        finally:
            self.refresh_btn.setEnabled(True)

    def _populate_tasks(self, tasks: list[dict]) -> None:
        self.table.setRowCount(0)
        for t in tasks:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(t["id"])))
            self.table.setItem(row, 1, QTableWidgetItem(t.get("role", "")))
            self.table.setItem(row, 2, QTableWidgetItem(t.get("task", "")))
            self.table.setItem(row, 3, QTableWidgetItem(str(t.get("assigned_account_id") or "-")))
            self.table.setItem(row, 4, QTableWidgetItem(t.get("status", "")))
            self.table.setItem(row, 5, QTableWidgetItem(t.get("branch") or "-"))

    def _update_buttons(self, status: str) -> None:
        running = status == "running"
        self.start_btn.setEnabled(status in ("draft", "failed"))
        self.stop_btn.setEnabled(running)

    # ---------------------------------------------------------------- actions

    def _action(self, action: str) -> None:
        try:
            if action == "start":
                self.api.start_project(self.project["id"])
            else:
                self.api.stop_project(self.project["id"])
            self.reload()
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось {'запустить' if action == 'start' else 'остановить'} проект: {exc.message}"
            )
