// Full-screen embedded browser: the server-side browser of an account,
// streamed through noVNC in an iframe. The user logs into the service by hand
// there; the password never reaches this app or the API.

import { api } from "./api.js";
import { errorText, h, icon, toast } from "./ui.js";

const NOVNC_DEFAULTS = { autoconnect: "true", resize: "scale", reconnect: "true" };

/** Build the noVNC page URL from what the server returned (absolute or relative). */
export function novncPageUrl(vncUrl) {
  const url = new URL(vncUrl, location.origin);
  for (const [k, v] of Object.entries(NOVNC_DEFAULTS)) {
    if (!url.searchParams.has(k)) url.searchParams.set(k, v);
  }
  return url.toString();
}

/**
 * Open the browser overlay for `account`.
 * mode "login": shows «Готово, я вошёл» which calls confirm-login.
 * mode "view":  watch/control a live agent session; «Сохранить сессию» is secondary.
 * Resolves with the updated account after confirm-login, or null when closed.
 */
export function openBrowser(account, { mode = "login" } = {}) {
  return new Promise((resolve) => {
    let busy = false;
    let closed = false;

    const status = h("span", { class: "muted small browser-status" }, "Запуск браузера на сервере…");
    const frameBox = h("div", { class: "browser-frame" }, h("div", { class: "spinner" }));
    const doneBtn = h("button", { class: `browser-done${mode === "login" ? " primary" : ""}`, disabled: true },
      mode === "login" ? "Готово, я вошёл" : "Сохранить сессию");
    const reconnectBtn = h("button", { disabled: true, title: "Переподключить", "aria-label": "Переподключить" },
      icon("refresh"), h("span", { class: "hide-sm" }, "Переподключить"));
    const externalLink = h("a", { class: "btn", target: "_blank", rel: "noopener noreferrer", hidden: true, title: "Открыть во вкладке" },
      icon("external"), h("span", { class: "hide-sm" }, "Во вкладке"));
    const closeBtn = h("button", { class: "ghost browser-close", "aria-label": "Закрыть", title: "Закрыть" }, icon("close"));

    const overlay = h("div", { class: "browser-overlay", role: "dialog", "aria-modal": "true", "aria-label": `Браузер: ${account.name}` },
      h("header", { class: "browser-bar" },
        h("div", { class: "browser-title" },
          h("strong", {}, account.name),
          status),
        h("div", { class: "browser-actions" }, reconnectBtn, externalLink),
        doneBtn,
        closeBtn),
      h("div", { class: "rotate-hint" }, "Поверните телефон горизонтально — экран браузера станет крупнее"),
      mode === "login"
        ? h("div", { class: "browser-hint" },
            "Войдите в сервис в окне ниже. Логин и пароль вводятся прямо в браузере сервера и не сохраняются в приложении. Затем нажмите «Готово, я вошёл».")
        : null,
      frameBox);

    function showFrame(vncUrl) {
      const src = novncPageUrl(vncUrl);
      const iframe = h("iframe", {
        src,
        title: `Браузер аккаунта ${account.name}`,
        allow: "clipboard-read; clipboard-write; fullscreen",
        referrerpolicy: "no-referrer",
      });
      iframe.addEventListener("load", () => {
        status.textContent = mode === "login" ? "Подключено — выполните вход" : "Подключено";
      });
      frameBox.replaceChildren(iframe);
      externalLink.href = src;
      externalLink.hidden = false;
    }

    function showMessage(text) {
      frameBox.replaceChildren(h("div", { class: "browser-message" }, text));
    }

    async function start() {
      reconnectBtn.disabled = true;
      doneBtn.disabled = true;
      status.textContent = "Запуск браузера на сервере…";
      try {
        const vnc = await api.vncStart(account.id);
        if (closed) return;
        if (vnc.headless || !vnc.vnc_url) {
          status.textContent = "Сервер в headless-режиме";
          showMessage("VNC на сервере отключён (VNC_HEADLESS=true). Ручной вход невозможен; «Готово» сохранит текущую сессию браузера.");
        } else {
          showFrame(vnc.vnc_url);
        }
        doneBtn.disabled = false;
      } catch (err) {
        if (closed) return;
        status.textContent = "Ошибка запуска";
        showMessage(`Не удалось запустить браузер: ${errorText(err)}`);
      } finally {
        reconnectBtn.disabled = false;
      }
    }

    function close(result) {
      if (busy || closed) return;
      closed = true;
      overlay.remove();
      document.body.classList.remove("no-scroll");
      document.removeEventListener("keydown", onKey);
      if (!result) api.vncStop(account.id).catch(() => {}); // browser on the server stays alive
      resolve(result || null);
    }
    const onKey = (e) => e.key === "Escape" && !overlay.contains(document.activeElement?.closest("iframe")) && close(null);

    doneBtn.addEventListener("click", async () => {
      busy = true;
      doneBtn.disabled = true;
      reconnectBtn.disabled = true;
      status.textContent = "Сохраняю сессию…";
      try {
        const updated = await api.confirmLogin(account.id);
        busy = false;
        toast(`Вход в «${account.name}» сохранён`);
        close(updated);
      } catch (err) {
        busy = false;
        doneBtn.disabled = false;
        reconnectBtn.disabled = false;
        status.textContent = "Не сохранено";
        toast(`Не удалось сохранить вход: ${errorText(err)}`, { error: true, timeout: 7000 });
      }
    });
    reconnectBtn.addEventListener("click", start);
    closeBtn.addEventListener("click", () => close(null));
    document.addEventListener("keydown", onKey);

    document.body.classList.add("no-scroll");
    document.body.append(overlay);
    start();
  });
}
