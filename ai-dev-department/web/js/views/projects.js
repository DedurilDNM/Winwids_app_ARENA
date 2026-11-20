// «Проекты»: list, 3-step wizard (task → team → launch) and the live project
// screen (tasks board + event feed over WebSocket).
//
// Routes: #/projects · #/projects/new · #/projects/<id>

import { accessTokenForWs, api } from "../api.js";
import { openBrowser } from "../browser.js";
import { confirmDialog, emptyState, errorText, fmtDate, h, icon, markdown, toast } from "../ui.js";
import { ROLES, ROLE_LABELS, statusBadge } from "./accounts.js";

export const PROJECT_STATUS = {
  draft: ["Черновик", "idle"],
  running: ["Выполняется", "info"],
  completed: ["Завершён", "ok"],
  failed: ["Остановлен / ошибка", "danger"],
};
const TASK_STATUS = {
  pending: ["Ждёт", "idle"],
  running: ["В работе", "info"],
  done: ["Готово", "ok"],
  blocked: ["Заблокировано", "warn"],
  failed: ["Ошибка", "danger"],
};
const AGENT_ROLES = ROLES.filter((r) => r !== "Orchestrator");

function badge(map, status, testid) {
  const [label, cls] = map[status] || [status, "idle"];
  return h("span", { class: `badge ${cls}`, "data-testid": testid }, label);
}
export const projectBadge = (status) => badge(PROJECT_STATUS, status, "project-status");

function repoLink(repo, path = "", label = null) {
  return h("a", { href: `https://github.com/${repo}${path}`, target: "_blank", rel: "noopener noreferrer" }, label || repo);
}

export async function render(main, { params }) {
  if (params[0] === "new") return renderWizard(main);
  if (params[0] && /^\d+$/.test(params[0])) return renderProject(main, Number(params[0]));
  return renderList(main);
}

// ------------------------------------------------------------------ list

async function renderList(main) {
  const list = h("div", { class: "grid", "data-testid": "projects" });
  main.append(
    h("div", { class: "page-head" },
      h("h1", {}, "Проекты"),
      h("a", { class: "btn primary", href: "#/projects/new" }, icon("plus"), "Новый проект")),
    list);
  try {
    const projects = await api.listProjects();
    list.replaceChildren(...(projects.length
      ? projects.map((p) => h("a", { class: "card card-link", href: `#/projects/${p.id}`, dataset: { projectId: p.id } },
          h("div", { class: "card-head" }, h("div", { class: "card-title" }, p.name), projectBadge(p.status)),
          h("p", { class: "small muted clamp" }, p.task),
          h("div", { class: "meta" }, h("span", {}, p.github_repo), h("span", {}, fmtDate(p.created_at)),
            p.pr_url ? h("span", { class: "ok-text" }, "PR открыт") : null)))
      : [emptyState("Пока нет проектов",
          "Проект — это задача для команды агентов: оркестратор делит её на подзадачи, агенты пишут код, сервер коммитит его в GitHub.",
          h("a", { class: "btn primary", href: "#/projects/new" }, icon("plus"), "Создать проект"))]));
  } catch (err) {
    toast(`Не удалось загрузить проекты: ${errorText(err)}`, { error: true });
  }
}

// ------------------------------------------------------------------ wizard

async function renderWizard(main) {
  let accounts = [];
  let github = { connected: false };
  try {
    [accounts, github] = await Promise.all([api.listAccounts(), api.githubStatus()]);
  } catch (err) {
    toast(errorText(err), { error: true });
  }
  const draft = { name: "", task: "", description: "", github_repo: "", orchestrator_account_id: null, agents: new Map() };
  let step = 0;
  let repoOk = false;

  const stepper = h("ol", { class: "stepper" });
  const body = h("div", {});
  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "Новый проект"), h("a", { class: "btn", href: "#/projects" }, "Отмена")),
    stepper, body);

  if (!github.connected) {
    body.before(h("div", { class: "banner warn-banner" }, "GitHub не подключён — проект нельзя будет запустить. ",
      h("a", { href: "#/github" }, "Подключить")));
  }

  const STEPS = ["Задача", "Команда", "Запуск"];
  function show(i) {
    step = i;
    stepper.replaceChildren(...STEPS.map((label, n) =>
      h("li", { class: n === i ? "current" : n < i ? "done" : "" }, h("span", {}, String(n + 1)), label)));
    body.replaceChildren([stepTask, stepTeam, stepLaunch][i]());
    body.querySelector("input, textarea, select")?.focus();
  }

  // ---- step 1: task + repo
  function stepTask() {
    const name = h("input", { name: "name", maxLength: 200, value: draft.name, placeholder: "Например: Лендинг для кофейни" });
    const repo = h("input", { name: "repo", value: draft.github_repo, placeholder: "owner/repo" });
    const task = h("textarea", { name: "task", rows: 7, maxLength: 20000, placeholder: "Опишите, что нужно сделать. Чем конкретнее — тем лучше результат." });
    task.value = draft.task;
    const description = h("textarea", { name: "description", rows: 3, maxLength: 10000, placeholder: "Контекст, ограничения, стек (необязательно)" });
    description.value = draft.description;
    const repoState = h("div", { class: "small mt-8", "data-testid": "wizard-repo-check" });
    const checkBtn = h("button", { type: "button", disabled: !github.connected }, "Проверить");
    const error = h("div", { class: "form-error", role: "alert" });

    async function check() {
      const value = repo.value.trim();
      if (!value) return false;
      checkBtn.disabled = true;
      repoState.textContent = "Проверяю…";
      try {
        const r = await api.githubRepoCheck(value);
        repoOk = r.ok;
        draft.github_repo = r.repo;
        repo.value = r.repo;
        repoState.replaceChildren(h("span", { class: `badge ${r.ok ? "ok" : "danger"}` }, r.ok ? "Сервер может коммитить" : r.message));
      } catch (err) {
        repoOk = false;
        repoState.replaceChildren(h("span", { class: "badge danger" }, errorText(err)));
      } finally {
        checkBtn.disabled = !github.connected;
      }
      return repoOk;
    }
    checkBtn.addEventListener("click", check);
    repo.addEventListener("input", () => { repoOk = false; repoState.textContent = ""; });

    const form = h("form", { class: "card" },
      h("label", { class: "field" }, "Название проекта", name),
      h("label", { class: "field" }, "GitHub-репозиторий", h("div", { class: "row" }, repo, checkBtn), repoState),
      h("label", { class: "field" }, "Задача", task),
      h("label", { class: "field" }, "Описание", description),
      error,
      h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Далее: команда")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      Object.assign(draft, { name: name.value.trim(), task: task.value.trim(), description: description.value.trim(), github_repo: repo.value.trim() });
      if (!draft.name || !draft.task || !draft.github_repo) return (error.textContent = "Заполните название, репозиторий и задачу");
      if (!/^[\w.-]+\/[\w.-]+$/.test(draft.github_repo.replace(/^https:\/\/github\.com\//, "").replace(/\.git$/, ""))) {
        return (error.textContent = "Репозиторий в формате owner/repo");
      }
      if (github.connected && !repoOk && !(await check())) return (error.textContent = "Репозиторий не прошёл проверку");
      show(1);
    });
    return form;
  }

  // ---- step 2: orchestrator + agents with roles
  function stepTeam() {
    if (!accounts.length) {
      return emptyState("Нет аккаунтов", "Сначала добавьте аккаунты и войдите в них.", h("a", { class: "btn primary", href: "#/accounts" }, "К аккаунтам"));
    }
    if (draft.orchestrator_account_id === null) {
      const pick = accounts.find((a) => a.default_role === "Orchestrator" && a.status === "active") || accounts.find((a) => a.status === "active") || accounts[0];
      draft.orchestrator_account_id = pick.id;
    }
    const error = h("div", { class: "form-error", role: "alert" });
    const orch = h("select", { name: "orchestrator" },
      accounts.map((a) => h("option", { value: a.id, selected: a.id === draft.orchestrator_account_id }, `${a.name}${a.status === "active" ? "" : " (не активен)"}`)));
    const rows = h("div", { class: "team" });

    function renderRows() {
      draft.orchestrator_account_id = Number(orch.value);
      draft.agents.delete(draft.orchestrator_account_id);
      rows.replaceChildren(...accounts.filter((a) => a.id !== draft.orchestrator_account_id).map((a) => {
        const checked = draft.agents.has(a.id);
        const box = h("input", { type: "checkbox", checked, "aria-label": `Добавить ${a.name}` });
        const role = h("select", { "aria-label": `Роль ${a.name}`, disabled: !checked },
          AGENT_ROLES.map((r) => h("option", { value: r, selected: r === (draft.agents.get(a.id) || (AGENT_ROLES.includes(a.default_role) ? a.default_role : "Coder")) }, ROLE_LABELS[r])));
        box.addEventListener("change", () => {
          error.textContent = "";
          if (box.checked) draft.agents.set(a.id, role.value);
          else draft.agents.delete(a.id);
          role.disabled = !box.checked;
        });
        role.addEventListener("change", () => draft.agents.set(a.id, role.value));
        return h("div", { class: `team-row${checked ? " on" : ""}`, dataset: { accountId: a.id } },
          h("label", { class: "team-name" }, box, h("span", {}, a.name)), statusBadge(a.status), role);
      }));
    }
    orch.addEventListener("change", renderRows);
    renderRows();

    const form = h("form", { class: "card" },
      h("label", { class: "field" }, "Оркестратор — делит задачу и собирает итог", orch),
      h("div", { class: "field" }, h("span", {}, "Агенты и их роли в этом проекте"), rows),
      h("p", { class: "small muted" }, "Неактивные аккаунты можно выбрать, но перед запуском в них нужно войти."),
      error,
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => show(0) }, "Назад"),
        h("button", { type: "submit", class: "primary" }, "Далее")));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!draft.agents.size) return (error.textContent = "Выберите хотя бы одного агента");
      show(2);
    });
    return form;
  }

  // ---- step 3: review + create (+ start)
  function stepLaunch() {
    const byId = new Map(accounts.map((a) => [a.id, a]));
    const team = [byId.get(draft.orchestrator_account_id), ...[...draft.agents.keys()].map((id) => byId.get(id))];
    const inactive = team.filter((a) => a && a.status !== "active");
    const error = h("div", { class: "form-error", role: "alert" });
    const createBtn = h("button", { type: "button" }, "Сохранить черновик");
    const startBtn = h("button", { type: "button", class: "primary", disabled: Boolean(inactive.length) || !github.connected }, "Создать и запустить");

    async function create(start) {
      createBtn.disabled = startBtn.disabled = true;
      try {
        const project = await api.createProject({
          name: draft.name,
          description: draft.description,
          task: draft.task,
          github_repo: draft.github_repo,
          orchestrator_account_id: draft.orchestrator_account_id,
          assignments: [...draft.agents].map(([account_id, role]) => ({ account_id, role })),
        });
        if (start) await api.startProject(project.id);
        toast(start ? "Проект запущен" : "Черновик сохранён");
        location.hash = `#/projects/${project.id}`;
      } catch (err) {
        error.textContent = errorText(err);
        createBtn.disabled = false;
        startBtn.disabled = Boolean(inactive.length) || !github.connected;
      }
    }
    createBtn.addEventListener("click", () => create(false));
    startBtn.addEventListener("click", () => create(true));

    return h("div", { class: "card" },
      h("dl", { class: "kv" },
        h("dt", {}, "Проект"), h("dd", {}, draft.name),
        h("dt", {}, "Репозиторий"), h("dd", {}, draft.github_repo),
        h("dt", {}, "Задача"), h("dd", { class: "pre" }, draft.task),
        h("dt", {}, "Оркестратор"), h("dd", {}, byId.get(draft.orchestrator_account_id)?.name),
        h("dt", {}, "Агенты"), h("dd", {}, [...draft.agents].map(([id, role]) => `${byId.get(id)?.name} — ${ROLE_LABELS[role]}`).join(", "))),
      inactive.length
        ? h("div", { class: "banner warn-banner mt-16" }, `Нужно войти в аккаунты: ${inactive.map((a) => a.name).join(", ")}. `, h("a", { href: "#/accounts" }, "К аккаунтам"))
        : h("p", { class: "small muted mt-16" }, `Сервер создаст ветку ai/project-<id>, агенты по очереди выполнят подзадачи, в конце откроется Pull Request.`),
      error,
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => show(1) }, "Назад"), createBtn, startBtn));
  }

  show(0);
}

// ------------------------------------------------------------------ project screen

async function renderProject(main, projectId) {
  let project;
  let accounts = [];
  try {
    [project, accounts] = await Promise.all([api.getProject(projectId), api.listAccounts()]);
  } catch (err) {
    main.append(emptyState("Проект не найден", errorText(err), h("a", { class: "btn", href: "#/projects" }, "К проектам")));
    return;
  }
  const byId = new Map(accounts.map((a) => [a.id, a]));
  let tasks = project.tasks || [];

  const head = h("div", {});
  const actions = h("div", { class: "actions m-0" });
  const board = h("div", { class: "tasks", "data-testid": "tasks" });
  const result = h("section", { class: "card", hidden: true });
  const feed = h("ol", { class: "feed", "data-testid": "feed" });
  const live = h("span", { class: "live", "data-testid": "live" }, "офлайн");

  main.append(
    h("div", { class: "page-head" }, head, actions),
    h("div", { class: "project-grid" },
      h("div", { class: "stack" },
        result,
        h("section", {}, h("h2", {}, "Задачи"), board)),
      h("section", { class: "card feed-card" }, h("div", { class: "card-head" }, h("h2", { class: "m-0" }, "События"), live), feed)));

  function renderHead() {
    head.replaceChildren(
      h("a", { href: "#/projects", class: "small" }, "← Проекты"),
      h("h1", {}, project.name),
      h("div", { class: "meta mt-8" }, projectBadge(project.status), repoLink(project.github_repo),
        project.work_branch ? repoLink(project.github_repo, `/tree/${project.work_branch}`, `ветка ${project.work_branch}`) : null,
        project.pr_url ? h("a", { href: project.pr_url, target: "_blank", rel: "noopener noreferrer", class: "ok-text", "data-testid": "pr-link" }, "Pull Request") : null));
    const running = project.status === "running";
    actions.replaceChildren(...[
      running ? h("button", { class: "danger", onclick: stop }, "Остановить")
        : project.status !== "completed" ? h("button", { class: "primary", onclick: start }, project.status === "draft" ? "Запустить" : "Запустить заново") : null,
      !running ? h("button", { class: "danger", onclick: remove }, "Удалить") : null,
    ].filter(Boolean));
    if (project.result) {
      result.hidden = false;
      result.replaceChildren(h("h2", {}, project.status === "completed" ? "Итог" : "Причина остановки"), h("div", { "data-testid": "result" }, markdown(project.result)));
    } else {
      result.hidden = true;
    }
  }

  function renderTasks() {
    if (!tasks.length) {
      board.replaceChildren(h("p", { class: "muted small" }, project.status === "running"
        ? "Оркестратор составляет план…" : "Задачи появятся после запуска: их составит оркестратор."));
      return;
    }
    board.replaceChildren(...tasks.map((t) => {
      const agent = byId.get(t.assigned_account_id);
      return h("article", { class: `card task task-${t.status}`, dataset: { taskId: t.id } },
        h("div", { class: "card-head" },
          h("div", {}, h("div", { class: "card-title" }, `#${t.id} · ${ROLE_LABELS[t.role] || t.role}`),
            h("div", { class: "small muted" }, agent ? agent.name : "агент удалён")),
          badge(TASK_STATUS, t.status, "task-status")),
        h("p", { class: "small pre" }, t.task),
        t.summary ? h("p", { class: "small task-summary" }, t.summary) : null,
        h("div", { class: "actions" }, ...[
          t.commit_sha ? repoLink(project.github_repo, `/commit/${t.commit_sha}`, `коммит ${t.commit_sha.slice(0, 7)}`) : null,
          agent && t.status === "running" ? h("button", { onclick: () => openBrowser(agent, { mode: "view" }) }, "Смотреть агента") : null,
        ].filter(Boolean)));
    }));
  }

  function addEvent(text, cls = "") {
    feed.prepend(h("li", { class: cls }, h("time", {}, new Date().toLocaleTimeString("ru-RU")), " ", text));
    while (feed.children.length > 200) feed.lastChild.remove();
  }

  async function reload() {
    try {
      project = await api.getProject(projectId);
      tasks = project.tasks || [];
      renderHead();
      renderTasks();
    } catch (err) {
      toast(errorText(err), { error: true });
    }
  }

  async function start() {
    try {
      await api.startProject(projectId);
      addEvent("Проект запущен", "info");
      await reload();
    } catch (err) {
      toast(`Не удалось запустить: ${errorText(err)}`, { error: true, timeout: 7000 });
    }
  }
  async function stop() {
    if (!(await confirmDialog("Остановить проект?", "Текущая задача будет прервана после ответа агента.", "Остановить"))) return;
    try {
      await api.stopProject(projectId);
      await reload();
    } catch (err) {
      toast(errorText(err), { error: true });
    }
  }
  async function remove() {
    if (!(await confirmDialog("Удалить проект?", "Задачи и история проекта будут удалены. Ветка и PR в GitHub останутся.", "Удалить"))) return;
    try {
      await api.deleteProject(projectId);
      location.hash = "#/projects";
    } catch (err) {
      toast(errorText(err), { error: true });
    }
  }

  // ---- live updates
  const EVENT_TEXT = {
    project_started: () => ["Проект запущен", "info"],
    task_started: (e) => [`Задача #${e.task_id} начата`, "info"],
    task_status_changed: (e) => [`Задача #${e.task_id}: ${(TASK_STATUS[e.status] || [e.status])[0]}`, e.status === "done" ? "ok" : e.status === "failed" || e.status === "blocked" ? "danger" : ""],
    project_completed: () => ["Проект завершён", "ok"],
    project_failed: (e) => [`Проект остановлен: ${e.message}`, "danger"],
    session_lost: (e) => [`Аккаунт #${e.account_id}: сессия потеряна — нужен повторный вход`, "danger"],
    session_relogged: (e) => [`Аккаунт #${e.account_id}: сессия восстановлена`, "ok"],
  };
  let ws = null;
  let closed = false;
  let retry = 0;
  let reloadTimer = null;

  async function connect(forceRefresh = false) {
    if (closed) return;
    const token = await accessTokenForWs({ forceRefresh });
    if (!token || closed) return;
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/projects/${projectId}`);
    ws.addEventListener("open", () => ws.send(JSON.stringify({ type: "auth", token })));
    ws.addEventListener("message", (msg) => {
      let e;
      try { e = JSON.parse(msg.data); } catch { return; }
      if (e.type === "snapshot") {
        retry = 0;
        live.textContent = "онлайн";
        live.classList.add("on");
        if (e.project) Object.assign(project, e.project);
        tasks = e.tasks || [];
        renderHead();
        renderTasks();
        return;
      }
      const describe = EVENT_TEXT[e.type];
      if (describe) addEvent(...describe(e));
      // Events carry ids only: refetch the full state (debounced).
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(reload, 300);
    });
    ws.addEventListener("close", (ev) => {
      live.textContent = "офлайн";
      live.classList.remove("on");
      if (closed) return;
      retry += 1;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(retry, 5));
      setTimeout(() => connect(ev.code === 4401), delay);
    });
  }

  renderHead();
  renderTasks();
  connect();
  return () => {
    closed = true;
    clearTimeout(reloadTimer);
    ws?.close();
  };
}
