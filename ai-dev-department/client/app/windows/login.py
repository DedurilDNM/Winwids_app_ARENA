"""Login window: login or register, then open the main window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.api_client import ApiClient, ApiError
from app.auth_store import AuthStore


class LoginWindow(QDialog):
    def __init__(self, api: ApiClient, auth: AuthStore, parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.auth = auth
        self.setWindowTitle("AI Dev Department — вход")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.email = QLineEdit()
        self.email.setPlaceholderText("email@example.com")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пароль (минимум 8 символов)")
        self.invite_code = QLineEdit()
        self.invite_code.setPlaceholderText("Инвайт-код (только при регистрации)")
        self.remember = QCheckBox("Запомнить меня")
        self.remember.setChecked(True)
        form.addRow("Email:", self.email)
        form.addRow("Пароль:", self.password)
        form.addRow("Инвайт-код:", self.invite_code)
        form.addRow("", self.remember)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.login_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.login_btn.setText("Войти")
        self.register_btn = QPushButton("Зарегистрироваться")
        buttons.addButton(self.register_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(buttons)

        self.login_btn.clicked.connect(self._do_login)
        self.register_btn.clicked.connect(self._do_register)

    def _set_busy(self, busy: bool) -> None:
        self.login_btn.setEnabled(not busy)
        self.register_btn.setEnabled(not busy)
        self.email.setEnabled(not busy)
        self.password.setEnabled(not busy)

    def _do_login(self) -> None:
        self._set_busy(True)
        try:
            self.api.login(self.email.text().strip(), self.password.text())
            self.accept()
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка входа",
                f"Не удалось войти: {exc.message}",
            )
        finally:
            self._set_busy(False)

    def _do_register(self) -> None:
        if not self.invite_code.text().strip():
            QMessageBox.warning(
                self, "Инвайт-код",
                "Для регистрации нужен инвайт-код. Попросите администратора выдать его.",
            )
            return
        self._set_busy(True)
        try:
            self.api.register(
                self.email.text().strip(),
                self.password.text(),
                self.invite_code.text().strip(),
            )
            QMessageBox.information(
                self, "Готово",
                "Регистрация прошла успешно. Теперь вы можете войти.",
            )
        except ApiError as exc:
            QMessageBox.critical(
                self, "Ошибка регистрации",
                f"Не удалось зарегистрироваться: {exc.message}",
            )
        finally:
            self._set_busy(False)
