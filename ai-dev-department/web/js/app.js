// App bootstrap: session restore, hash router, the shell (chats on the left, the page on the right).

import { api, auth, refreshSession } from "./api.js";
import { h, icon } from "./ui.js";
import { startAttention, stopAttention } from "./attention.js";
import * as loginView from "./views/login.js";
import * as chatView from "./views/chat.js";
import * as settingsView from "./views/settings.js";
import * as projectsView from "./views/projects.js";
import * as departmentView from "./views/department.js";

const ROUTES = [
  { path: "chat", label: "Чат", view: chatView, chat: true },
  { path: "settings", label: "Настройки", view: settingsView },
  { path: "projects", label: "Проекты (браузер)", view: projectsView },
  { path: "department", label: "Запуски", view: departmentView },
];
const DEFAULT_ROUTE = "chat/new";

const root = document.getElementById("app");
const state = { me: null, cleanup: null, chats: [], chatTimer: null };

function currentRoute() {
  const [path, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const route = ROUTES.find((r) => r.path === path);
  return { route, params: rest };
}

function renderLogin() {
  state.me = null;
  clearInterval(state.chatTimer);
  root.replaceChildren();
  loginView.render(root, { onLoggedIn: start });
}

const svg = (d, size = 18) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("width", size); s.setAttribute("height", size);
  s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.8");
  s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", d); s.append(p);
  return s;
};
const ICON = {
  folder: "M3 7h6l2 2h10v10H3z",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2v3M12 19v3M4.9 4.9 7 7M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1",
  menu: "M3 6h18M3 12h18M3 18h18",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
};

export function setTopbar(title, ...actions) {
  const bar = document.getElementById("topbar");
  if (!bar) return;
  bar.querySelector(".tb-title").textContent = title || "";
  bar.querySelector(".tb-actions").replaceChildren(...actions.filter(Boolean));
}

function renderShell() {
  const chats = h("div", { class: "side-chats", "data-testid": "chat-list" });
  const side = h("aside", { class: "side", id: "side" },
    h("div", { class: "side-brand" }, "AIDD"),
    h("a", { class: "side-item side-new", href: "#/chat/new", "data-testid": "new-chat", onclick: closeSide },
      h("span", { class: "plus" }, "+"), "Новый чат"),
    h("a", { class: "side-item", href: "#/projects", onclick: closeSide }, svg(ICON.folder), "Проекты (браузер)"),
    h("div", { class: "side-label" }, "Недавние"),
    chats,
    h("div", { class: "side-me" },
      h("span", { class: "av" }, (state.me?.email || "?").slice(0, 1).toUpperCase()),
      h("span", { class: "me-name" }, state.me?.email || ""),
      h("a", { class: "icon-btn", href: "#/settings/keys", title: "Настройки", "data-testid": "settings", onclick: closeSide }, svg(ICON.gear)),
      h("button", { class: "icon-btn", title: "Выйти", onclick: () => auth.logout() }, svg(ICON.logout))));
  const topbar = h("div", { class: "topbar", id: "topbar" },
    h("button", { class: "icon-btn menu-btn", "aria-label": "Чаты", onclick: () => side.classList.toggle("open") }, svg(ICON.menu)),
    h("div", { class: "tb-title" }), h("div", { class: "tb-actions" }));
  const main = h("main", { class: "main", id: "main" });
  const bar = h("div", { class: "attention-bar", hidden: true, role: "alert" });
  const scrim = h("div", { class: "side-scrim", onclick: closeSide });
  root.replaceChildren(h("div", { class: "shell" }, side, scrim, h("div", { class: "content" }, bar, topbar, main)));
  startAttention(bar);
  loadChats();
  clearInterval(state.chatTimer);
  state.chatTimer = setInterval(loadChats, 5000);
}

function closeSide() { document.getElementById("side")?.classList.remove("open"); }

export async function loadChats() {
  const box = document.querySelector(".side-chats");
  if (!box || !auth.loggedIn) return;
  try { state.chats = await api.listChats(); } catch { return; }
  const current = location.hash.match(/^#\/chat\/(\d+)/)?.[1];
  box.replaceChildren(...(state.chats.length ? state.chats.map((c) => h("a", {
    class: `side-chat${String(c.id) === current ? " on" : ""}`, href: `#/chat/${c.id}`, onclick: closeSide, title: c.title,
  }, c.status === "running" ? h("span", { class: "live" }) : null, c.title))
    : [h("div", { class: "side-empty" }, "Здесь появятся ваши чаты")]));
}

async function route() {
  if (!auth.loggedIn) return renderLogin();
  const alias = { accounts: "settings/accounts", github: "settings/github", profile: "settings/profile" }[
    location.hash.replace(/^#\/?/, "").split("/")[0]];
  if (alias) {
    location.replace(`#/${alias}`);
    return;
  }
  const { route: r, params } = currentRoute();
  if (!r) {
    location.replace(`#/${DEFAULT_ROUTE}`);
    return;
  }
  if (!document.getElementById("main")) renderShell();
  if (typeof state.cleanup === "function") state.cleanup();
  state.cleanup = null;
  const main = document.getElementById("main");
  main.className = `main${r.chat ? " chat-mode" : ""}`;
  main.replaceChildren();
  setTopbar(r.label);
  document.title = `${r.label} — AIDD`;
  loadChats();
  state.cleanup = await r.view.render(main, { me: state.me, params, setTopbar, refreshChats: loadChats });
}

async function start() {
  try {
    state.me = await api.me();
  } catch {
    return renderLogin();
  }
  root.replaceChildren();
  if (!currentRoute().route) location.replace(`#/${DEFAULT_ROUTE}`);
  await route();
}

auth.onLogout(() => {
  stopAttention();
  clearInterval(state.chatTimer);
  if (typeof state.cleanup === "function") state.cleanup();
  state.cleanup = null;
  renderLogin();
});
window.addEventListener("hashchange", route);
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && auth.loggedIn) { e.preventDefault(); location.hash = "#/chat/new"; }
});

(async () => {
  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  if (await refreshSession()) await start();
  else renderLogin();
})();

export { icon };
