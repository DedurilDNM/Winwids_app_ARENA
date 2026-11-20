// «Профиль»: account info, install as app, logout.

import { auth } from "../api.js";
import { fmtDate, h } from "../ui.js";

let deferredInstall = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstall = e;
});

export async function render(main, { me }) {
  const installBtn = h("button", {
    hidden: !deferredInstall,
    onclick: async () => {
      if (!deferredInstall) return;
      deferredInstall.prompt();
      await deferredInstall.userChoice;
      deferredInstall = null;
      installBtn.hidden = true;
    },
  }, "Установить приложение");

  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "Профиль")),
    h("section", { class: "card" },
      h("dl", { class: "kv" },
        h("dt", {}, "Email"), h("dd", { "data-testid": "me-email" }, me?.email || "—"),
        h("dt", {}, "Роль"), h("dd", {}, me?.is_admin ? "Администратор" : "Пользователь"),
        h("dt", {}, "С нами с"), h("dd", {}, fmtDate(me?.created_at))),
      h("div", { class: "actions" },
        installBtn,
        h("button", { class: "danger", onclick: () => auth.logout() }, "Выйти"))),
    h("p", { class: "muted small mt-16" },
      "На телефоне: меню браузера → «Добавить на главный экран», чтобы открыть как приложение."));
}
