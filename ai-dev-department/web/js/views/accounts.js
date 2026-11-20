// «Аккаунты»: the user's service accounts (agents) and their roles.

import { api } from "../api.js";
import { emptyState, errorText, fmtDate, h, icon, toast } from "../ui.js";

export const ROLES = ["Orchestrator", "Architect", "Coder", "Reviewer", "Tester", "Integrator"];
export const ROLE_LABELS = {
  Orchestrator: "Оркестратор", Architect: "Архитектор", Coder: "Разработчик",
  Reviewer: "Ревьюер", Tester: "Тестировщик", Integrator: "Интегратор",
};
export const MAX_ACCOUNTS = 10;
export const STATUS = {
  pending_login: ["Ждёт входа", "warn"],
  active: ["Активен", "ok"],
  relogin_needed: ["Нужен повторный вход", "danger"],
  disabled: ["Отключён", "idle"],
};

export function statusBadge(status) {
  const [label, cls] = STATUS[status] || [status, "idle"];
  return h("span", { class: `badge ${cls}` }, label);
}

function accountCard(acc) {
  return h("article", { class: "card", dataset: { accountId: acc.id } },
    h("div", { class: "card-head" },
      h("div", { class: "card-title" }, acc.name),
      statusBadge(acc.status)),
    h("div", { class: "meta" },
      h("span", {}, ROLE_LABELS[acc.default_role] || acc.default_role),
      h("span", {}, acc.github_repos?.length ? acc.github_repos.join(", ") : "GitHub не задан"),
      h("span", {}, `Проверка: ${fmtDate(acc.last_health_check)}`)),
    acc.service_note ? h("p", { class: "small muted mt-8" }, acc.service_note) : null);
}

export async function render(main) {
  const list = h("div", { class: "grid" });
  const counter = h("span", { class: "muted small" });
  const refreshBtn = h("button", { onclick: load, title: "Обновить" }, icon("refresh"), "Обновить");

  main.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Аккаунты"), counter),
      h("div", { class: "actions m-0" }, refreshBtn)),
    list);

  async function load() {
    refreshBtn.disabled = true;
    try {
      const accounts = await api.listAccounts();
      counter.textContent = `${accounts.length} из ${MAX_ACCOUNTS}`;
      list.replaceChildren(...(accounts.length
        ? accounts.map(accountCard)
        : [emptyState("Пока нет аккаунтов", "Добавьте аккаунт сервиса — вход выполняется во встроенном браузере.")]));
    } catch (err) {
      toast(`Не удалось загрузить аккаунты: ${errorText(err)}`, { error: true });
    } finally {
      refreshBtn.disabled = false;
    }
  }
  await load();
}
