// API department: runs (live tree of agents), the key pool, roles, skills.
// Server data always goes through textContent (h), never innerHTML.

import { api } from "../api.js";
import { modelMenu } from "../models.js";
import { confirmDialog, emptyState, errorText, fmtDate, h, icon, markdown, modal, toast } from "../ui.js";

const ROLE_RU = { Orchestrator: "Оркестратор", Architect: "Архитектор", Coder: "Кодер", Reviewer: "Ревьюер",
  Tester: "Тестер", Integrator: "Интегратор" };
const RUN_STATUS = { queued: ["в очереди", "idle"], running: ["идёт", "info"], done: ["готово", "ok"],
  failed: ["ошибка", "danger"], stopped: ["остановлен", "warn"] };
const AGENT_STATUS = { working: ["работает", "info"], done: ["готово", "ok"], failed: ["ошибка", "danger"],
  stopped: ["остановлен", "warn"] };
const KEY_STATUS = { available: ["доступен", "ok"], limited: ["в лимите", "warn"], invalid: ["не принят", "danger"],
  disabled: ["выключен", "idle"] };
const TABS = [["runs", "Запуски"], ["keys", "Котёл ключей"], ["roles", "Роли"], ["skills", "Скиллы"]];
const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];

const badge = ([text, cls]) => h("span", { class: `badge ${cls}` }, text);
const roleName = (r) => ROLE_RU[r] || r;
const kTokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));
const mmss = (s) => (s >= 3600 ? `${Math.round(s / 60)} мин` : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);

export async function render(main, { params }) {
  const [tab = "runs", id] = params;
  const timers = [];
  const every = (fn, ms) => { const t = setInterval(fn, ms); timers.push(t); return t; };
  const cleanup = () => timers.forEach(clearInterval);
  const tabs = h("nav", { class: "subtabs", role: "tablist" }, TABS.map(([t, label]) =>
    h("a", { href: `#/department/${t}`, role: "tab", "aria-selected": String(t === tab) }, label)));
  const body = h("div", { class: "dept" });
  main.append(h("div", { class: "page-head" }, h("h1", { class: "m-0" }, "API-отдел")), tabs, body);
  try {
    if (tab === "keys") await keysView(body, every);
    else if (tab === "roles") await rolesView(body);
    else if (tab === "skills") await skillsView(body);
    else if (id) await runView(body, Number(id), every);
    else await runsView(body, every);
  } catch (err) {
    body.replaceChildren(h("div", { class: "banner" }, errorText(err)));
  }
  return cleanup;
}

// ---------------------------------------------------------------- runs

async function runsView(body, every) {
  const prompt = h("textarea", { rows: 4, placeholder: "Что сделать? Например: «Сделай PWA Pomodoro-таймер с историей и тестами»",
    "data-testid": "run-prompt" });
  const lead = h("select", {}, Object.keys(ROLE_RU).map((r) => h("option", { value: r, selected: r === "Orchestrator" }, roleName(r))));
  const startBtn = h("button", { class: "primary", "data-testid": "run-start", onclick: async () => {
    if (!prompt.value.trim()) return toast("Опишите задачу", { error: true });
    startBtn.disabled = true;
    try {
      const run = await api.createRun({ prompt: prompt.value.trim(), lead_role: lead.value });
      location.hash = `#/department/runs/${run.id}`;
    } catch (err) {
      toast(errorText(err), { error: true });
    } finally {
      startBtn.disabled = false;
    }
  } }, "Запустить отдел");
  const list = h("div", { class: "grid", "data-testid": "runs" });
  body.append(h("div", { class: "card" }, h("label", {}, "Задача для отдела"), prompt,
    h("div", { class: "actions" }, h("span", { class: "inline-field" }, "Ведущий:", lead), startBtn)), h("h2", {}, "Запуски"), list);
  const load = async () => {
    const runs = await api.listRuns();
    if (!runs.length) return list.replaceChildren(emptyState("Пока пусто", "Опишите задачу выше: ведущий агент разобьёт её и раздаст субагентам."));
    list.replaceChildren(...runs.map((r) => h("a", { class: "card run-card", href: `#/department/runs/${r.id}` },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, r.title || `Запуск #${r.id}`), badge(RUN_STATUS[r.status] || [r.status, "idle"])),
      h("div", { class: "meta" }, h("span", {}, fmtDate(r.created_at)), h("span", {}, `${roleName(r.lead_role)} ведёт`),
        h("span", {}, `${kTokens(r.tokens_in + r.tokens_out)} токенов`)))));
  };
  await load();
  every(() => load().catch(() => {}), 5000);
}

function agentRows(agents) {
  const depth = (a) => (a.id.match(/\./g) || []).length;
  return agents.map((a) => h("div", { class: "agent", style: `--d:${depth(a)}`, "data-testid": "agent" },
    h("span", { class: "agent-glyph" }, roleName(a.role).slice(0, 1)),
    h("div", { class: "agent-main" },
      h("div", {}, h("b", {}, roleName(a.role)), a.description ? h("span", { class: "muted" }, ` · ${a.description}`) : null),
      h("div", { class: "muted small agent-last" }, a.last || (a.status === "working" ? "думает…" : ""))),
    h("div", { class: "agent-side" }, badge(AGENT_STATUS[a.status] || [a.status, "idle"]),
      h("div", { class: "muted small" }, `${a.model || ""}${a.tokens ? ` · ${kTokens(a.tokens)}` : ""}`))));
}

const EVENT_TEXT = {
  tool: (d) => [d.tool, d.arg || ""],
  text: (d) => ["пишет", d.text || ""],
  file: (d) => ["файл", `${d.path} (${d.action})`],
  skill: (d) => ["скилл", d.name],
  todos: (d) => ["план", `${(d.todos || []).length} пунктов`],
  agent_start: (d) => ["старт", `${roleName(d.role)}${d.description ? ` · ${d.description}` : ""} · ${d.model}`],
  agent_done: (d) => ["итог", d.status === "done" ? `готово, ${d.turns} ход.` : `${d.status}${d.error ? `: ${d.error}` : ""}`],
  key_rotation: (d) => ["ключ", `••••${d.from_tail} в лимите → ••••${d.to_tail}`],
  model_switch: (d) => ["модель", `${d.from} → ${d.to} (${d.reason})`],
  auto_models: (d) => ["модели", d.status === "started" ? "роли не настроены: подбираю самые сильные доступные модели…"
    : (d.assignments || []).map((a) => `${roleName(a.role)}: ${a.model}`).join(" · ")],
  keys_waiting: (d) => ["ключ", `все ключи ${d.provider} заняты или в лимите — жду свободный`],
};

async function runView(body, runId, every) {
  const head = h("div", { class: "card run-head" });
  const tree = h("div", { class: "card tree", "data-testid": "agents" });
  const todos = h("div", { class: "card", hidden: true });
  const feed = h("div", { class: "card feed", "data-testid": "feed" });
  const result = h("div", { class: "card", hidden: true, "data-testid": "result" });
  const files = h("div", { class: "card", hidden: true });
  body.append(h("a", { href: "#/department/runs", class: "small" }, "← все запуски"), head,
    h("div", { class: "run-grid" }, h("div", {}, h("h2", {}, "Агенты"), tree, todos), h("div", {}, h("h2", {}, "Шаги"), feed)),
    result, files);
  let after = 0;
  let final = false;
  const agentsById = {};
  const addEvents = (events) => {
    for (const e of events) {
      after = Math.max(after, e.id);
      const fmt = EVENT_TEXT[e.type];
      if (!fmt || e.type === "tool_done") continue;
      const [what, text] = fmt(e.data || {});
      const role = agentsById[e.agent_id]?.role || "";
      feed.prepend(h("div", { class: `ev ev-${e.type}` }, h("span", { class: "ev-time" }, fmtDate(e.at).slice(-5)),
        h("span", { class: "ev-who" }, roleName(role)), h("span", { class: "ev-what" }, what), h("span", { class: "ev-text" }, text)));
    }
    while (feed.children.length > 400) feed.lastChild.remove();
  };
  const tick = async () => {
    const run = await api.getRun(runId);
    run.agents.forEach((a) => { agentsById[a.id] = a; });
    const stopBtn = run.live ? h("button", { onclick: async () => { await api.stopRun(runId); toast("Останавливаю…"); } }, "Остановить") : null;
    head.replaceChildren(h("div", { class: "card-head" }, h("div", { class: "card-title" }, run.title), badge(RUN_STATUS[run.status] || [run.status, "idle"])),
      h("div", { class: "meta" }, h("span", {}, fmtDate(run.created_at)), h("span", {}, `${run.agents.length} агент.`),
        h("span", {}, `${kTokens(run.tokens_in + run.tokens_out)} токенов`)),
      h("div", { class: "actions" }, stopBtn,
        h("button", { onclick: async () => {
          const blob = await api.runArchive(runId);
          const a = h("a", { href: URL.createObjectURL(blob), download: `run-${runId}.zip` });
          document.body.append(a); a.click(); a.remove();
        } }, "Скачать файлы (.zip)")));
    tree.replaceChildren(...(run.agents.length ? agentRows(run.agents) : [h("p", { class: "muted" }, "Запускается…")]));
    todos.hidden = !run.todos.length;
    todos.replaceChildren(h("b", {}, "План ведущего"), h("ul", { class: "todos" }, run.todos.map((t) =>
      h("li", { class: t.status }, t.status === "completed" ? "✓ " : t.status === "in_progress" ? "▸ " : "○ ", t.content))));
    addEvents(await api.runEvents(runId, after));
    if (run.result || run.error) {
      result.hidden = false;
      result.replaceChildren(h("h2", { class: "m-0" }, run.error ? "Ошибка" : "Итог"), run.error ? h("p", { class: "danger-text" }, run.error) : markdown(run.result));
    }
    if (!run.live && ["done", "failed", "stopped"].includes(run.status) && !final) {
      final = true;
      const list = await api.runFiles(runId);
      files.hidden = !list.length;
      files.replaceChildren(h("h2", { class: "m-0" }, `Файлы (${list.length})`), h("ul", { class: "files" }, list.slice(0, 300).map((f) =>
        h("li", {}, h("a", { href: "#", onclick: async (e) => {
          e.preventDefault();
          const text = await api.runFile(runId, f.path);
          modal(f.path, (close) => h("div", {}, h("pre", { class: "file-view" }, text), h("div", { class: "modal-actions" }, h("button", { onclick: () => close() }, "Закрыть"))));
        } }, f.path), h("span", { class: "muted small" }, ` ${f.size} Б`)))));
    }
  };
  await tick();
  every(() => { if (!final) tick().catch(() => {}); }, 1500);
}

// ---------------------------------------------------------------- key pool

export async function keysView(body, every) {
  const providers = await api.providers();
  const summary = h("div", { class: "stats", "data-testid": "key-summary" });
  const list = h("div", { class: "grid", "data-testid": "keys" });
  const addBtn = h("button", { class: "primary", onclick: () => addKeyDialog(providers, load) }, icon("plus"), "Ключ в котёл");
  body.append(h("p", { class: "muted" }, "Ключи из общего котла раздаются агентам сами: каждый запрос берёт самый свободный ключ. Ключ упёрся в лимит — запрос уходит на другой, а этот проверяется раз в 5 минут и возвращается сам."),
    h("div", { class: "actions" }, addBtn), summary, list);
  let fetchedAt = 0;
  let keys = [];
  const paint = () => {
    const passed = (Date.now() - fetchedAt) / 1000;
    list.replaceChildren(...(keys.length ? keys.map((k) => h("div", { class: "card key-card", "data-testid": "key" },
      h("div", { class: "card-head" }, h("div", {}, h("div", { class: "card-title" }, k.label), h("div", { class: "muted small" }, `${k.provider_name} · ••••${k.tail}`)),
        badge(KEY_STATUS[k.status] || [k.status, "idle"])),
      k.reason ? h("div", { class: "small warn-text" }, k.reason) : null,
      h("div", { class: "meta" }, h("span", {}, `в работе: ${k.busy}`), h("span", {}, `запросов: ${k.requests}`),
        h("span", {}, `токенов: ${kTokens(k.tokens_in + k.tokens_out)}`),
        k.next_check_in != null ? h("span", {}, `проверка через ${mmss(Math.max(0, k.next_check_in - passed))}`) : null),
      h("div", { class: "actions" },
        h("button", { onclick: async () => { const r = await api.checkKey(k.id); toast(r.outcome === "ok" ? "Ключ доступен" : `Проверка: ${r.key.reason || r.outcome}`); load(); } }, "Проверить"),
        h("button", { onclick: async () => { await api.updateKey(k.id, { disabled: k.status !== "disabled" }); load(); } }, k.status === "disabled" ? "Включить" : "Выключить"),
        h("button", { class: "danger", onclick: async () => { if (await confirmDialog("Удалить ключ?", `${k.label} ••••${k.tail} уйдёт из котла.`, "Удалить")) { await api.deleteKey(k.id); load(); } } }, "Удалить"))))
      : [emptyState("Котёл пуст", "Добавьте ключ OpenRouter, Anthropic или другого агрегатора.", addBtn.cloneNode(true))]));
  };
  async function load() {
    const data = await api.listKeys();
    keys = data.keys;
    fetchedAt = Date.now();
    const s = data.summary;
    summary.replaceChildren(...[["Доступно", s.available, "ok"], ["В лимите", s.limited, "warn"], ["Не приняты", s.invalid, "danger"], ["Запросов в работе", s.busy, "info"]]
      .map(([t, v, c]) => h("div", { class: "card stat" }, h("div", { class: "muted small" }, t), h("div", { class: `stat-v ${c}` }, String(v)))));
    paint();
  }
  await load();
  every(() => load().catch(() => {}), 5000);
  every(paint, 1000);
}

function addKeyDialog(providers, done) {
  return modal("Ключ в котёл", (close) => {
    const prov = h("select", { "data-testid": "key-provider" }, providers.map((p) => h("option", { value: p.id }, p.name)));
    const key = h("input", { type: "password", placeholder: "Ключ API", autocomplete: "off", "data-testid": "key-secret" });
    const label = h("input", { placeholder: "Название (необязательно)" });
    const base = h("input", { placeholder: "https://…/v1 (адрес OpenAI-совместимого сервиса)", hidden: true });
    const err = h("div", { class: "form-error" });
    const sync = () => {
      const p = providers.find((x) => x.id === prov.value);
      base.hidden = !p?.needs_base_url;
      key.placeholder = p?.key_hint ? `Ключ API (${p.key_hint})` : "Ключ API";
    };
    prov.addEventListener("change", sync);
    sync();
    const ok = h("button", { class: "primary", onclick: async () => {
      ok.disabled = true; err.textContent = "Проверяю ключ…";
      try {
        const k = await api.addKey({ provider: prov.value, key: key.value.trim(), label: label.value.trim(), base_url: base.hidden ? null : base.value.trim() });
        toast(k.status === "available" ? "Ключ проверен и добавлен" : `Ключ добавлен, сейчас ${KEY_STATUS[k.status][0]}`);
        close(true); done();
      } catch (e) {
        err.textContent = errorText(e);
      } finally { ok.disabled = false; }
    } }, "Проверить и добавить");
    return h("div", {}, h("label", {}, "Агрегатор"), prov, h("label", {}, "Ключ"), key, label, base, err,
      h("p", { class: "muted small" }, "Ключ хранится зашифрованным; виден только по последним 4 символам."),
      h("div", { class: "modal-actions" }, h("button", { onclick: () => close() }, "Отмена"), ok));
  });
}

// ---------------------------------------------------------------- roles

export async function rolesView(body) {
  const [roles, providers, skills] = await Promise.all([api.listRoles(), api.providers(), api.listSkills()]);
  const TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "TodoWrite", "Skill", "SendFile", "Screenshot", "Task"];
  const grid = h("div", { class: "grid roles-grid" });
  const autoBox = h("div", { class: "card", hidden: true, "data-testid": "auto-result" });
  const autoBtn = h("button", { class: "primary", "data-testid": "auto-roles", onclick: () => runAuto() }, "Подобрать модели автоматически");
  const engineBox = h("div", { class: "card engine-card", "data-testid": "engine" });
  async function paintEngine(e) {
    const seg = h("div", { class: "seg" }, [["auto", "Авто"], ["claude_code", "Claude Code"], ["native", "Встроенный"]].map(([id, label]) =>
      h("button", { class: e.engine === id ? "on" : "", onclick: async () => { try { paintEngine(await api.setEngine(id)); } catch (err) { toast(errorText(err), { error: true }); } } }, label)));
    const now = e.active === "claude_code" ? `Сейчас работает: Claude Code ${e.version || ""}` : "Сейчас работают встроенные агенты";
    const why = !e.installed ? "Claude Code не установлен на сервере (см. инструкцию в документации)."
      : "";
    engineBox.replaceChildren(h("div", { class: "card-head" }, h("div", { class: "card-title" }, "Движок"), seg),
      h("p", { class: "small" }, now), why ? h("p", { class: "muted small" }, why) : null,
      h("p", { class: "muted small" }, "Claude Code — те же инструкции, инструменты, суб-агенты и скиллы, что у Claude. Ключи берутся из котла, модели — из ролей ниже; с любым агрегатором (для OpenAI-совместимых прокси переводит формат). Лучше всего — с моделями Claude."));
  }
  try { await paintEngine(await api.getEngine()); } catch { engineBox.hidden = true; }
  body.append(engineBox, h("p", { class: "muted" }, "Автоподбор смотрит все модели агрегаторов, на которые есть ключи, оценивает силу (цена ответа, размер, класс модели), проверяет, что модель отвечает и умеет вызывать инструменты, и ставит самую сильную ведущей, остальные роли — по убыванию. Роли, выбранные вами, автоподбор не трогает; «Сбросить» вернёт роль автоподбору."),
    h("div", { class: "actions" }, autoBtn), autoBox, grid);
  async function runAuto() {
    autoBtn.disabled = true;
    autoBox.hidden = false;
    autoBox.replaceChildren(h("p", {}, "Смотрю модели и проверяю лучшие…"));
    try {
      let job = await api.autoRoles(true);
      while (job.running) {
        autoBox.replaceChildren(h("p", {}, `Проверено моделей: ${job.checked}, рабочих: ${job.working}…`));
        await new Promise((r) => setTimeout(r, 2000));
        job = await api.autoRolesStatus();
      }
      if (job.error) throw new Error(job.error);
      const res = job.result;
      if (!res.assignments.length) {
        autoBox.replaceChildren(h("p", { class: "danger-text" }, res.candidates ? "Ни одна проверенная модель не смогла вызвать инструмент." : "Нет моделей: добавьте ключ в котёл."));
        return;
      }
      toast("Модели назначены");
      autoBox.replaceChildren(h("b", {}, `Рейтинг (из ${res.candidates} моделей)`), h("table", { class: "rank" },
        h("tr", {}, h("th", {}, "Модель"), h("th", {}, "Агрегатор"), h("th", {}, "Сила"), h("th", {}, "$/1M"), h("th", {}, "Проверка")),
        res.ranking.slice(0, 15).map((m) => h("tr", {}, h("td", { class: "mono" }, m.model), h("td", {}, m.provider), h("td", {}, String(m.score)),
          h("td", {}, m.price_out != null ? m.price_out.toFixed(2) : "—"),
          h("td", {}, m.works === true ? "✓" : m.works === false ? `✗ ${m.check || ""}` : "—")))));
      await reloadRoles();
    } catch (e) {
      autoBox.replaceChildren(h("p", { class: "danger-text" }, errorText(e)));
    } finally {
      autoBtn.disabled = false;
    }
  }
  async function reloadRoles() {
    grid.replaceChildren();
    await paintRoles(await api.listRoles());
  }
  await paintRoles(roles);
  async function paintRoles(roles) {
  for (const r of roles) {
    const prov = h("select", {}, providers.map((p) => h("option", { value: p.id, selected: p.id === r.provider }, p.name)));
    const model = h("input", { value: r.model || "", placeholder: "нажмите, чтобы выбрать из списка", autocomplete: "off", "data-testid": "role-model" });
    // a list of every connected aggregator's models; picking one sets the aggregator too
    const drop = h("div", { class: "popover models below", hidden: true });
    const openDrop = (e) => {
      e?.stopPropagation();
      if (!drop.hidden) return;
      document.querySelectorAll(".popover.below").forEach((p) => { p.hidden = true; });
      drop.replaceChildren(modelMenu({
        onPick: (provider, name) => { prov.value = provider; model.value = name; drop.hidden = true; save.click(); },
        typed: { sub: `как есть, у агрегатора ${prov.selectedOptions[0]?.textContent || ""}`, onclick: (name) => { model.value = name; drop.hidden = true; save.click(); } },
      }));
      drop.hidden = false;
    };
    model.addEventListener("focus", openDrop);
    model.addEventListener("click", openDrop);
    document.addEventListener("click", () => { drop.hidden = true; });
    const effort = h("select", {}, EFFORTS.map((e) => h("option", { value: e, selected: (r.effort || "") === e }, e || "по умолчанию")));
    const toolBoxes = TOOLS.map((t) => h("label", { class: "chip-check" }, h("input", { type: "checkbox", value: t, checked: (r.tools || []).includes(t) }), t));
    const allSkills = r.skills === null || r.skills === undefined;
    const skillBoxes = skills.map((s) => h("label", { class: "chip-check", title: s.description }, h("input", { type: "checkbox", value: s.name, checked: allSkills || r.skills.includes(s.name) }), s.name));
    const instr = h("textarea", { rows: 3, placeholder: "Свои инструкции роли (необязательно)" }, r.instructions || "");
    const save = h("button", { class: "primary", onclick: async () => {
      const chosenSkills = skillBoxes.filter((l) => l.firstChild.checked).map((l) => l.firstChild.value);
      try {
        await api.saveRole(r.role, { provider: prov.value, model: model.value.trim(), effort: effort.value || null,
          tools: toolBoxes.filter((l) => l.firstChild.checked).map((l) => l.firstChild.value),
          skills: chosenSkills.length === skills.length ? null : chosenSkills, instructions: instr.value });
        toast(`${roleName(r.role)}: сохранено`);
        reloadRoles();
      } catch (e) { toast(errorText(e), { error: true }); }
    } }, "Сохранить");
    const reset = h("button", { onclick: async () => { await api.resetRole(r.role); toast("Настройки роли сброшены"); reloadRoles(); } }, "Сбросить");
    grid.append(h("div", { class: "card role-card", "data-testid": "role" },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, roleName(r.role)),
        h("span", {}, r.auto ? badge(["авто", "info"]) : null, " ", badge(r.ready ? ["готова", "ok"] : ["не настроена", "warn"]))),
      r.fallbacks?.length ? h("div", { class: "muted small" }, "Запасные: ", r.fallbacks.map((f) => `${f.model} (${f.provider})`).join(", ")) : null,
      h("div", { class: "two" }, h("div", {}, h("label", {}, "Агрегатор"), prov), h("div", {}, h("label", {}, "Усилие"), effort)),
      h("label", {}, "Модель"), h("div", { class: "pop-host" }, model, drop),
      h("label", {}, "Инструменты"), h("div", { class: "chips" }, toolBoxes),
      skills.length ? [h("label", {}, "Скиллы"), h("div", { class: "chips" }, skillBoxes)] : null,
      instr, h("div", { class: "actions" }, save, reset)));
  }
  }
}

// ---------------------------------------------------------------- skills

export async function skillsView(body) {
  const list = h("div", { class: "grid" });
  const file = h("input", { type: "file", accept: ".md,.zip", hidden: true, onchange: async () => {
    if (!file.files[0]) return;
    try { const s = await api.addSkillFile(file.files[0]); toast(`Скилл ${s.name} добавлен`); load(); } catch (e) { toast(errorText(e), { error: true }); }
    file.value = "";
  } });
  const paste = h("button", { onclick: () => modal("Новый скилл (SKILL.md)", (close) => {
    const ta = h("textarea", { rows: 12 }, "---\nname: my-skill\ndescription: Когда агенту использовать этот скилл.\n---\n\nИнструкции…\n");
    const err = h("div", { class: "form-error" });
    return h("div", {}, ta, err, h("div", { class: "modal-actions" }, h("button", { onclick: () => close() }, "Отмена"),
      h("button", { class: "primary", onclick: async () => {
        try { await api.addSkillText(ta.value); close(true); load(); } catch (e) { err.textContent = errorText(e); }
      } }, "Добавить")));
  }) }, "Написать SKILL.md");
  const fromGit = h("button", { "data-testid": "skill-github", onclick: () => modal("Скилл по ссылке GitHub", (close) => {
    const url = h("input", { placeholder: "https://github.com/<владелец>/<репозиторий>", "data-testid": "skill-url" });
    const err = h("div", { class: "form-error" });
    return h("div", {}, h("p", { class: "muted small" }, "Любая ссылка: на репозиторий, папку или сам SKILL.md. Найду все скиллы (папки со SKILL.md) и скачаю их целиком со скриптами и данными. Проверьте лицензию в репозитории."),
      url, err, h("div", { class: "modal-actions" }, h("button", { onclick: () => close() }, "Отмена"),
        h("button", { class: "primary", onclick: async () => {
          err.textContent = "Скачиваю…";
          try {
            const res = await api.importSkill(url.value.trim());
            const names = (res.installed || []).map((x) => x.name);
            toast(names.length > 1 ? `Добавлено скиллов: ${names.length} (${names.join(", ")})` : `Скилл ${names[0]} добавлен`);
            close(true); load();
          } catch (e) { err.textContent = errorText(e); }
        } }, "Добавить")));
  }) }, "По ссылке GitHub");
  const official = h("button", { "data-testid": "skill-official", onclick: async () => {
    official.disabled = true;
    official.textContent = "Скачиваю…";
    try {
      const res = await api.importSkill("https://github.com/anthropics/skills");
      toast(`Официальные скиллы Anthropic: ${(res.installed || []).length}`);
      load();
    } catch (e) { toast(errorText(e), { error: true }); }
    official.disabled = false;
    official.textContent = "Официальные скиллы Anthropic";
  } }, "Официальные скиллы Anthropic");
  body.append(h("p", { class: "muted" }, "Как в Claude Code: агент видит имя и описание скилла и сам открывает инструкции, когда задача подходит."),
    h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => file.click() }, icon("plus"), "Загрузить .zip или SKILL.md"), paste, fromGit, official, file),
    h("p", { class: "muted small" }, "Официальные — набор Anthropic (github.com/anthropics/skills): документы Word/Excel/PowerPoint/PDF, frontend-design, canvas-design, webapp-testing и другие. Лицензия у части скиллов (документы) — source-available от Anthropic: проверьте условия в репозитории."), list);
  async function load() {
    const skills = await api.listSkills();
    list.replaceChildren(...skills.map((s) => h("div", { class: "card", "data-testid": "skill" },
      h("div", { class: "card-head" }, h("div", { class: "card-title mono" }, s.name), badge(s.source === "user" ? ["ваш", "info"] : ["встроенный", "idle"])),
      h("p", { class: "muted small" }, s.description),
      h("div", { class: "actions" },
        h("button", { onclick: async () => { const full = await api.getSkill(s.name); modal(s.name, (close) => h("div", {}, markdown(full.body), h("div", { class: "modal-actions" }, h("button", { onclick: () => close() }, "Закрыть")))); } }, "Открыть"),
        s.source === "user" ? h("button", { class: "danger", onclick: async () => { if (await confirmDialog("Удалить скилл?", s.name, "Удалить")) { await api.deleteSkill(s.name); load(); } } }, "Удалить") : null))));
  }
  await load();
}
