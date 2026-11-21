// App bootstrap: session restore, hash router, workspace shell.

import { api, auth, refreshSession } from "./api.js";
import { h, icon } from "./ui.js";
import { startAttention, stopAttention } from "./attention.js";
import * as loginView from "./views/login.js";
import * as accountsView from "./views/accounts.js";
import * as githubView from "./views/github.js";
import * as projectsView from "./views/projects.js";
import * as profileView from "./views/profile.js";

const ROUTES = [
  { path: "accounts", label: "Аккаунты", icon: "users", view: accountsView },
  { path: "github", label: "GitHub", icon: "github", view: githubView },
  { path: "projects", label: "Проекты", icon: "folder", view: projectsView },
  { path: "profile", label: "Профиль", icon: "user", view: profileView },
];
const DEFAULT_ROUTE = "accounts";

const root = document.getElementById("app");
const state = { me: null, cleanup: null };

function currentRoute() {
  const [path, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const route = ROUTES.find((r) => r.path === path);
  return { route, params: rest };
}

function renderLogin() {
  state.me = null;
  root.replaceChildren();
  loginView.render(root, { onLoggedIn: start });
}

function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Разделы" },
    ROUTES.map((r) => h("a", { href: `#/${r.path}`, dataset: { route: r.path } }, icon(r.icon), h("span", {}, r.label))));
  const sidebar = h("aside", { class: "sidebar" },
    h("div", { class: "brand" }, h("img", { src: "icon.svg", alt: "" }), "AI Dev Department"),
    nav);
  const main = h("main", { class: "main", id: "main" });
  const bar = h("div", { class: "attention-bar", hidden: true, role: "alert" });
  root.replaceChildren(h("div", { class: "shell" }, sidebar, bar, main));
  startAttention(bar);
}

async function route() {
  if (!auth.loggedIn) return renderLogin();
  const { route: r, params } = currentRoute();
  if (!r) {
    location.replace(`#/${DEFAULT_ROUTE}`);
    return;
  }
  if (!document.getElementById("main")) renderShell();
  document.querySelectorAll(".nav a").forEach((a) => {
    if (a.dataset.route === r.path) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (typeof state.cleanup === "function") state.cleanup();
  state.cleanup = null;
  const main = document.getElementById("main");
  main.replaceChildren();
  document.title = `${r.label} — AI Dev Department`;
  state.cleanup = await r.view.render(main, { me: state.me, params });
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
  if (typeof state.cleanup === "function") state.cleanup();
  state.cleanup = null;
  renderLogin();
});
window.addEventListener("hashchange", route);

(async () => {
  if ("serviceWorker" in navigator && location.protocol !== "file:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  if (await refreshSession()) await start();
  else renderLogin();
})();
