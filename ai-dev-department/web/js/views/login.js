// Login / registration screen.

import { api } from "../api.js";
import { errorText, h } from "../ui.js";

export function render(root, { onLoggedIn }) {
  let mode = "login";

  const email = h("input", { type: "email", name: "email", autocomplete: "username", required: true, placeholder: "you@example.com" });
  const password = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true, minLength: 1 });
  const invite = h("input", { type: "text", name: "invite", autocomplete: "off", placeholder: "Код от администратора" });
  const remember = h("input", { type: "checkbox", name: "remember", checked: true });
  const inviteField = h("label", { class: "field", hidden: true }, "Инвайт-код", invite);
  const error = h("div", { class: "form-error", role: "alert" });
  const submit = h("button", { type: "submit", class: "primary w-full" }, "Войти");

  const tabLogin = h("button", { type: "button", role: "tab" }, "Вход");
  const tabRegister = h("button", { type: "button", role: "tab" }, "Регистрация");

  function setMode(next) {
    mode = next;
    tabLogin.setAttribute("aria-selected", String(mode === "login"));
    tabRegister.setAttribute("aria-selected", String(mode === "register"));
    inviteField.hidden = mode !== "register";
    invite.required = mode === "register";
    password.minLength = mode === "register" ? 8 : 1;
    password.autocomplete = mode === "register" ? "new-password" : "current-password";
    password.placeholder = mode === "register" ? "Минимум 8 символов" : "";
    submit.textContent = mode === "register" ? "Зарегистрироваться" : "Войти";
    error.textContent = "";
  }
  tabLogin.addEventListener("click", () => setMode("login"));
  tabRegister.addEventListener("click", () => setMode("register"));

  const form = h("form", { novalidate: false },
    h("label", { class: "field" }, "Email", email),
    h("label", { class: "field" }, "Пароль", password),
    inviteField,
    h("label", { class: "checkbox" }, remember, "Запомнить меня на этом устройстве"),
    error,
    submit);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    try {
      if (mode === "register") {
        await api.register(email.value.trim(), password.value, invite.value.trim(), remember.checked);
      } else {
        await api.login(email.value.trim(), password.value, remember.checked);
      }
      password.value = "";
      await onLoggedIn();
    } catch (err) {
      error.textContent = err.status === 401 ? "Неверный email или пароль" : errorText(err);
    } finally {
      submit.disabled = false;
    }
  });

  setMode("login");
  root.append(h("div", { class: "auth" },
    h("div", { class: "auth-card" },
      h("div", { class: "auth-logo" }, h("img", { src: "icon.svg", alt: "" }),
        h("div", {}, h("h1", {}, "AI Dev Department"), h("div", { class: "muted small" }, "Рабочая среда команды ИИ-агентов"))),
      h("div", { class: "tabs", role: "tablist" }, tabLogin, tabRegister),
      form)));
  email.focus();
}
