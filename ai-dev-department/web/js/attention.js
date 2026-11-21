// "A human is needed": captcha / pop-up in an agent's browser. Polled on
// every page; shown as a banner + a system notification (if allowed).

import { api, auth } from "./api.js";
import { openBrowser } from "./browser.js";
import { h, toast } from "./ui.js";

const TEXT = {
  captcha: "сервис показал капчу — пройдите её в браузере агента",
  dialog: "всплывающее окно мешает отправке — закройте его в браузере агента",
};
const seen = new Set();
let timer = null;

export function attentionText(kind) {
  return TEXT[kind] || kind;
}

async function notify(item) {
  const title = `Нужна помощь: ${item.account_name}`;
  const body = attentionText(item.kind);
  toast(`⚠ ${title}: ${body}`, { error: true, timeout: 8000 });
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const reg = await navigator.serviceWorker?.getRegistration();
    if (reg) await reg.showNotification(title, { body, tag: `attention-${item.id}`, icon: "icon.svg", renotify: true });
    else new Notification(title, { body, tag: `attention-${item.id}` });
  } catch {
    /* notifications are best effort */
  }
  if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
}

function render(bar, items) {
  if (!items.length) {
    bar.hidden = true;
    bar.replaceChildren();
    return;
  }
  bar.hidden = false;
  bar.replaceChildren(...items.map((item) => h("div", { class: "attention-item", "data-testid": "attention" },
    h("span", { class: "attention-text" }, h("strong", {}, `⚠ ${item.account_name}: `), attentionText(item.kind)),
    h("div", { class: "actions m-0" },
      "Notification" in window && Notification.permission === "default"
        ? h("button", { onclick: () => Notification.requestPermission() }, "Включить уведомления") : null,
      h("button", { class: "primary", onclick: () => openBrowser({ id: item.account_id, name: item.account_name }, { mode: "view" }) },
        "Открыть браузер")))));
}

export function startAttention(bar) {
  stopAttention();
  const tick = async () => {
    if (!auth.loggedIn) return;
    try {
      const items = await api.attention();
      for (const item of items) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          notify(item);
        }
      }
      render(bar, items);
    } catch {
      /* offline: keep the last state */
    }
  };
  tick();
  timer = setInterval(tick, 8000);
}

export function stopAttention() {
  clearInterval(timer);
  timer = null;
}
