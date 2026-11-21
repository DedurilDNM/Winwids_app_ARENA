// «Проекты»: list, 3-step wizard (task → team → launch) and the live project
// screen (tasks board + event feed over WebSocket).
//
// Routes: #/projects · #/projects/new · #/projects/<id>

import { accessTokenForWs, api } from "../api.js";
import { openBrowser } from "../browser.js";
import { confirmDialog, emptyState, errorText, fmtDate, h, icon, markdown, toast } from "../ui.js";
import { ROLES, ROLE_LABELS, statusBadge } from "./accounts.js";
import { attentionText } from "../attention.js";

export const PROJECT_STATUS = {
  draft: ["Черновик", "idle"],
  running: ["Выполняется", "info"],
  completed: ["Завершён", "ok"],
  failed: ["Остановлен / ошибка", "danger"],
};
const TASK_STATUS = {
  pending: ["Ждёт", "idle"],
  running: ["В работе", "info"],
  paused: ["Пауза: нужен человек", "warn"],
  done: ["Готово", "ok"],
  blocked: ["Заблокировано", "warn"],
  failed: ["Ошибка", "danger"],
};
const AGENT_ROLES = ROLES.filter((r) => r !== "Orchestrator");
const CI_STATUS = {
  passed: ["CI: проходит", "ok"],
  failed: ["CI: упал", "danger"],
  running: ["CI: проверяется…", "info"],
  no_workflow: ["CI: ещё не настроен", "idle"],
  no_access: ["CI: нет права Actions: Read", "warn"],
  not_started: ["CI: не запустился", "warn"],
  timeout: ["CI: слишком долго", "warn"],
};

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
  const draft = { name: "", task: "", description: "", github_repo: "", orchestrator_account_id: null, agents: new Map(), max_rounds: 3, ci_enabled: true };
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
    const rounds = h("select", { name: "max_rounds", "aria-label": "Круги проверки" },
      [[0, "без проверки"], [1, "1 круг доработок"], [3, "до 3 кругов (рекомендуется)"], [5, "до 5 кругов"]].map(([v, label]) =>
        h("option", { value: v, selected: v === draft.max_rounds }, label)));
    rounds.addEventListener("change", () => { draft.max_rounds = Number(rounds.value); });
    const ci = h("input", { type: "checkbox", name: "ci_enabled", checked: draft.ci_enabled });
    ci.addEventListener("change", () => { draft.ci_enabled = ci.checked; });

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
          max_rounds: draft.max_rounds,
          ci_enabled: draft.ci_enabled,
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
        h("dt", {}, "Агенты"), h("dd", {}, [...draft.agents].map(([id, role]) => `${byId.get(id)?.name} — ${ROLE_LABELS[role]}`).join(", ")),
        h("dt", {}, "Проверки"), h("dd", {}, rounds),
        h("dt", {}, "Сборка"), h("dd", {}, h("label", { class: "checkbox m-0" }, ci,
          "Проверять через GitHub Actions: агенты добавят CI, ошибки сборки и тестов вернутся на доработку"))),
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
  const brief = h("details", { class: "card brief", open: true, "data-testid": "brief" });
  const feed = h("ol", { class: "feed", "data-testid": "feed" });
  const chat = h("ol", { class: "feed chat", "data-testid": "messages" });
  let messages = project.messages || [];
  const live = h("span", { class: "live", "data-testid": "live" }, "офлайн");

  main.append(
    h("div", { class: "page-head" }, head, actions),
    h("div", { class: "project-grid" },
      h("div", { class: "stack" },
        brief,
        result,
        h("section", {}, h("h2", {}, "Задачи"), board)),
      h("div", { class: "stack feed-card" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "m-0" }, "События"), live), feed),
        h("section", { class: "card" }, h("h2", {}, "Переписка агентов"), chat))));

  function ciBadge() {
    if (!project.ci_status) return null;
    const [label, cls] = CI_STATUS[project.ci_status] || [`CI: ${project.ci_status}`, "idle"];
    const b = h("span", { class: `badge ${cls}`, "data-testid": "ci" }, label);
    return project.ci_url ? h("a", { href: project.ci_url, target: "_blank", rel: "noopener noreferrer", class: "badge-link" }, b) : b;
  }

  function renderHead() {
    head.replaceChildren(
      h("a", { href: "#/projects", class: "small" }, "← Проекты"),
      h("h1", {}, project.name),
      h("div", { class: "meta mt-8" }, projectBadge(project.status), repoLink(project.github_repo),
        project.work_branch ? repoLink(project.github_repo, `/tree/${project.work_branch}`, `ветка ${project.work_branch}`) : null,
        project.pr_url ? h("a", { href: project.pr_url, target: "_blank", rel: "noopener noreferrer", class: "ok-text", "data-testid": "pr-link" }, "Pull Request") : null,
        ciBadge()));
    const running = project.status === "running";
    const canResume = project.status === "failed" && tasks.length > 0;
    actions.replaceChildren(...[
      running ? h("button", { class: "danger", onclick: stop }, "Остановить") : null,
      canResume ? h("button", { class: "primary", onclick: resume, "data-testid": "resume" }, "Продолжить с места остановки") : null,
      !running && project.status !== "completed"
        ? h("button", { class: canResume ? "" : "primary", onclick: () => start(canResume) },
            project.status === "draft" ? "Запустить" : "Запустить заново")
        : null,
      !running ? h("button", { class: "danger", onclick: remove }, "Удалить") : null,
    ].filter(Boolean));
    if (project.result) {
      result.hidden = false;
      result.replaceChildren(h("h2", {}, project.status === "completed" ? "Итог" : "Причина остановки"), h("div", { "data-testid": "result" }, markdown(project.result)));
    } else {
      result.hidden = true;
    }
  }

  function taskCard(t) {
    const agent = byId.get(t.assigned_account_id);
    const deps = (t.depends_on || []).map((d) => `#${d}`).join(", ");
    return h("article", { class: `card task task-${t.status}`, dataset: { taskId: t.id } },
      h("div", { class: "card-head" },
        h("div", {}, h("div", { class: "card-title" }, `#${t.id} · ${ROLE_LABELS[t.role] || t.role}`),
          h("div", { class: "small muted" }, agent ? agent.name : "агент удалён", deps ? ` · после ${deps}` : "",
            t.attempts ? ` · повтор ${t.attempts}` : "")),
        badge(TASK_STATUS, t.status, "task-status")),
      (t.scope || []).length ? h("div", { class: "chips", "data-testid": "scope" }, t.scope.map((x) => h("code", {}, x))) : null,
      h("p", { class: "small pre" }, t.task),
      t.summary ? h("p", { class: "small task-summary" }, t.summary) : null,
      h("div", { class: "actions" }, ...[
        t.commit_sha ? repoLink(project.github_repo, `/commit/${t.commit_sha}`, `коммит ${t.commit_sha.slice(0, 7)}`) : null,
        agent && (t.status === "running" || t.status === "paused")
          ? h("button", { class: t.status === "paused" ? "primary" : "", onclick: () => openBrowser(agent, { mode: "view" }) },
              t.status === "paused" ? "Открыть браузер и помочь" : "Смотреть агента")
          : null,
      ].filter(Boolean)));
  }

  function renderBrief() {
    brief.replaceChildren(...[
      h("summary", {}, h("h2", { class: "m-0 inline" }, "Задача проекта")),
      h("div", { class: "pre brief-text", "data-testid": "brief-task" }, project.task || "—"),
      project.description ? h("h3", { class: "round-title" }, "Описание") : null,
      project.description ? h("div", { class: "pre brief-text" }, project.description) : null,
    ].filter(Boolean));
  }

  function renderTasks() {
    if (!tasks.length) {
      board.replaceChildren(h("p", { class: "muted small" }, project.status === "running"
        ? "Оркестратор составляет план…" : "Задачи появятся после запуска: их составит оркестратор."));
      return;
    }
    const rounds = new Map();
    for (const t of tasks) {
      const r = t.round || 0;
      if (!rounds.has(r)) rounds.set(r, []);
      rounds.get(r).push(t);
    }
    const working = tasks.filter((t) => t.status === "running" || t.status === "paused").length;
    board.replaceChildren(...[
      working > 1 ? h("p", { class: "small ok-text", "data-testid": "parallel" }, `Сейчас параллельно работают агентов: ${working}`) : null,
      ...[...rounds].sort(([a], [b]) => a - b).map(([r, list]) => h("div", { class: "round" },
        h("h3", { class: "round-title" }, r === 0 ? "План" : `Доработки — круг ${r}`),
        h("div", { class: "tasks" }, list.map(taskCard)))),
    ].filter(Boolean));
  }

  function renderMessages() {
    if (!messages.length) {
      chat.replaceChildren(h("li", { class: "muted" }, "Агенты ещё ничего не писали друг другу"));
      return;
    }
    const KIND = { question: "вопрос", answer: "ответ", message: "" };
    chat.replaceChildren(...messages.slice(-100).reverse().map((m) => h("li", { class: `msg-${m.kind}` },
      h("strong", {}, m.from_name), " → ", h("strong", {}, m.to),
      KIND[m.kind] ? h("span", { class: "muted" }, ` (${KIND[m.kind]})`) : null,
      h("div", { class: "pre" }, m.text))));
  }

  function addEvent(text, cls = "") {
    feed.prepend(h("li", { class: cls }, h("time", {}, new Date().toLocaleTimeString("ru-RU")), " ", text));
    while (feed.children.length > 200) feed.lastChild.remove();
  }

  async function reload() {
    try {
      project = await api.getProject(projectId);
      tasks = project.tasks || [];
      messages = project.messages || [];
      renderHead();
      renderTasks();
      renderMessages();
    } catch (err) {
      toast(errorText(err), { error: true });
    }
  }

  async function resume() {
    try {
      await api.resumeProject(projectId);
      await reload();
    } catch (err) {
      toast(`Не удалось продолжить: ${errorText(err)}`, { error: true, timeout: 7000 });
    }
  }

  async function start(confirmFirst = false) {
    if (confirmFirst && !(await confirmDialog("Запустить с нуля?",
      "Оркестратор составит новый план, все задачи начнутся заново. Чтобы сохранить сделанное, нажмите «Продолжить с места остановки».",
      "Запустить заново"))) return;
    try {
      await api.startProject(projectId);  // the server sends «project_started» to the feed
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
    attention_needed: (e) => [`⚠ ${e.account_name || "Агент"}: ${attentionText(e.kind)}`, "danger"],
    attention_resolved: () => ["Готово, агент продолжает работу", "ok"],
    agent_message: (e) => [`✉ ${e.from} → ${e.to}: ${e.text.slice(0, 120)}`, "info"],
    review_round: (e) => [`Оркестратор отправил на доработку: ${e.message.match(/\d+ fix/) ? e.message.match(/(\d+) fix/)[1] + " задач(и)" : ""}`, "info"],
    review_done: () => ["Оркестратор принял работу", "ok"],
    task_retry: (e) => [`Задача #${e.task_id}: временный сбой, повторяю автоматически`, "info"],
    project_resumed: () => ["Проект продолжен с места остановки", "ok"],
    ci_waiting: () => ["Ждём проверку сборки (GitHub Actions)…", "info"],
    ci_result: (e) => [(CI_STATUS[e.ci_status] || [`CI: ${e.ci_status}`])[0], e.ci_status === "passed" ? "ok" : e.ci_status === "failed" ? "danger" : ""],
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

  renderBrief();
  renderHead();
  renderTasks();
  renderMessages();
  connect();
  return () => {
    closed = true;
    clearTimeout(reloadTimer);
    ws?.close();
  };
}
