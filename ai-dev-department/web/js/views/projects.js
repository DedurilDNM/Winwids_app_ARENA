// «Проекты»: list of projects (wizard + live view arrive in stage 4).

import { api } from "../api.js";
import { emptyState, errorText, fmtDate, h, toast } from "../ui.js";

export const PROJECT_STATUS = {
  draft: ["Черновик", "idle"],
  queued: ["В очереди", "info"],
  running: ["Выполняется", "info"],
  completed: ["Завершён", "ok"],
  failed: ["Ошибка", "danger"],
  stopped: ["Остановлен", "idle"],
};

export function projectBadge(status) {
  const [label, cls] = PROJECT_STATUS[status] || [status, "idle"];
  return h("span", { class: `badge ${cls}` }, label);
}

export async function render(main) {
  const list = h("div", { class: "grid" });
  main.append(h("div", { class: "page-head" }, h("h1", {}, "Проекты")), list);
  try {
    const projects = await api.listProjects();
    list.replaceChildren(...(projects.length
      ? projects.map((p) => h("article", { class: "card", dataset: { projectId: p.id } },
          h("div", { class: "card-head" }, h("div", { class: "card-title" }, p.name), projectBadge(p.status)),
          h("div", { class: "meta" }, h("span", {}, p.github_repo), h("span", {}, fmtDate(p.created_at)))))
      : [emptyState("Пока нет проектов", "Проект — это задача для команды агентов в одном GitHub-репозитории.")]));
  } catch (err) {
    toast(`Не удалось загрузить проекты: ${errorText(err)}`, { error: true });
  }
}
