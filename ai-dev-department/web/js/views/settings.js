// «Настройки»: everything technical in one place - keys, models and roles, skills,
// browser accounts, GitHub, profile. Each tab reuses the screen it always had.

import { h } from "../ui.js";
import { keysView, rolesView, skillsView } from "./department.js";
import * as accountsView from "./accounts.js";
import * as githubView from "./github.js";
import * as profileView from "./profile.js";

const TABS = [
  ["keys", "Ключи API", "Общий котёл: агенты сами берут свободный ключ, ключ в лимите проверяется раз в 5 минут."],
  ["models", "Модели и роли", "Самая сильная рабочая модель ведёт, остальные роли — по убыванию. Любую роль можно задать вручную."],
  ["skills", "Скиллы", "Папки со SKILL.md: агент видит описание и сам открывает инструкции, когда задача подходит."],
  ["accounts", "Браузерные аккаунты", "Второй движок: агенты через веб-чат сервиса в браузере на сервере."],
  ["github", "GitHub", "Токен для коммитов и PR от имени сервера."],
  ["profile", "Профиль", ""],
];

export async function render(main, { params, me, setTopbar }) {
  const tab = TABS.find((t) => t[0] === params[0]) || TABS[0];
  const timers = [];
  const every = (fn, ms) => { const t = setInterval(fn, ms); timers.push(t); return t; };
  setTopbar("Настройки");
  const nav = h("nav", { class: "set-nav", "aria-label": "Разделы настроек" }, TABS.map(([id, label]) =>
    h("a", { href: `#/settings/${id}`, "aria-current": id === tab[0] ? "page" : null, "data-testid": `set-${id}` }, label)));
  const body = h("section", { class: "set-body" });
  main.append(h("div", { class: "settings" }, nav, body));
  const content = h("div", { class: "dept" });
  if (["keys", "models", "skills"].includes(tab[0])) {
    body.append(h("h1", {}, tab[1]), content);
    if (tab[0] === "keys") await keysView(content, every);
    else if (tab[0] === "models") await rolesView(content);
    else await skillsView(content);
  } else {
    const view = { accounts: accountsView, github: githubView, profile: profileView }[tab[0]];
    const cleanup = await view.render(body, { me, params: [] });
    if (typeof cleanup === "function") timers.push({ cleanup });
  }
  return () => timers.forEach((t) => (t.cleanup ? t.cleanup() : clearInterval(t)));
}
