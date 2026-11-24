// The chat: write a task, the department works on it right here - the lead's text, its
// plan, every step as a line (open it for the diff or the output), sub-agents as blocks,
// the files that changed. Follow-up messages continue in the same workspace.
// Server data always goes through textContent (h / markdown), never innerHTML.

import { api } from "../api.js";
import { confirmDialog, errorText, h, markdown, toast } from "../ui.js";

const ROLE_RU = { Orchestrator: "Оркестратор", Architect: "Архитектор", Coder: "Кодер", Reviewer: "Ревьюер",
  Tester: "Тестер", Integrator: "Интегратор" };
const MODES = [["department", "Отдел"], ["single", "Один агент"], ["plan", "План"]];
const SUGGESTIONS = [
  ["PWA-приложение", "Pomodoro-таймер с историей, тёмной темой и тестами"],
  ["Найти и починить баг", "опишите, что ломается, и приложите шаги"],
  ["Telegram-бот", "напоминания по расписанию, данные в SQLite"],
  ["Ревью кода", "безопасность, тесты и узкие места проекта"],
];
const roleName = (r) => ROLE_RU[r] || r || "";
const kTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));
const pref = {
  get(k, d) { try { return localStorage.getItem(`aidd.chat.${k}`) || d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`aidd.chat.${k}`, v); } catch { /* private mode */ } },
};

// ------------------------------------------------------------------ files shown in the chat

const KIND_RU = { html: "Прототип", image: "Изображение", pdf: "PDF", sheet: "Таблица", document: "Документ Word",
  slides: "Презентация", text: "Текст", file: "Файл" };
const sizeText = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(n / 1024))} КБ`);

async function downloadFile(chatId, path) {
  const blob = await api.chatFileBlob(chatId, path);
  const a = h("a", { href: URL.createObjectURL(blob), download: path.split("/").pop() });
  document.body.append(a); a.click(); a.remove();
}

/** A live page from a signed address with its own sandbox: no same-origin access, so it can never
 *  touch the app or the session; files next to it (css, js, images) load by relative paths. */
function liveFrame(url, cls) {
  return h("iframe", { class: cls, src: url, sandbox: "allow-scripts allow-forms allow-modals allow-popups", referrerpolicy: "no-referrer" });
}

function attachmentCard(chatId, d) {
  const name = d.path.split("/").pop();
  const view = h("div", { class: "att-view" });
  const full = h("button", { class: "small-btn", hidden: true }, "На весь экран");
  const card = h("div", { class: "att", "data-testid": "attachment" },
    h("div", { class: "att-h" }, h("div", { class: "att-name" }, h("b", {}, name), h("span", {}, `${KIND_RU[d.kind] || "Файл"} · ${sizeText(d.size || 0)}`)),
      h("span", { class: "sp" }), full, h("button", { class: "small-btn", onclick: () => downloadFile(chatId, d.path) }, "Скачать")),
    d.caption ? h("div", { class: "att-cap" }, d.caption) : null, view);
  (async () => {
    try {
      if (d.kind === "html") {
        const url = await api.chatLink(chatId, d.path);
        view.append(liveFrame(url, "att-frame"));
        full.hidden = false;
        full.onclick = () => {
          const close = h("button", { class: "small-btn", onclick: () => box.remove() }, "Закрыть");
          const box = h("div", { class: "att-full" }, h("div", { class: "att-full-h" }, h("b", {}, name), h("span", { class: "sp" }), close), liveFrame(url, "att-full-frame"));
          document.body.append(box);
        };
      } else if (d.kind === "image") {
        view.append(h("img", { class: "att-img", alt: name, src: await api.chatLink(chatId, d.path) }));
      } else if (d.kind === "pdf") {
        view.append(h("iframe", { class: "att-frame", src: await api.chatLink(chatId, d.path) }));
      } else if (d.kind === "sheet") {
        const data = await api.chatPreview(chatId, d.path);
        const sheet = data.sheets[0];
        if (sheet) {
          view.append(h("div", { class: "att-table" }, data.sheets.length > 1 ? h("div", { class: "faint pad-s" }, `Лист «${sheet.name}» (из ${data.sheets.length})`) : null,
            h("table", {}, sheet.rows.map((r, i) => h("tr", {}, r.map((c) => h(i ? "td" : "th", {}, c)))))));
        }
      } else if (d.kind === "text") {
        const text = await (await api.chatFileBlob(chatId, d.path)).text();
        view.append(h("pre", { class: "out att-text" }, text.slice(0, 6000)));
      }
    } catch { view.append(h("div", { class: "faint pad-s" }, "Просмотр недоступен — файл можно скачать.")); }
  })();
  return card;
}

// ------------------------------------------------------------------ one turn

class Turn {
  constructor(turn, chat) {
    this.id = turn.id;
    this.chat = chat;
    this.done = false;
    this.agents = {};
    this.steps = {};
    this.plan = null;
    this.leadSteps = null;
    this.lastLeadText = null;
    this.body = h("div", { class: "turn-body" });
    this.thinking = h("div", { class: "thinking" }, "Работаю над задачей…");
    const [text, attached] = String(turn.prompt).split("\n\nПриложенные файлы (в папке проекта):");
    const names = (attached || "").split("\n").map((l) => l.replace(/^- /, "").trim()).filter(Boolean);
    this.el = h("div", { class: "turn", "data-testid": "turn" },
      h("div", { class: "user-msg" }, h("div", {}, text,
        names.length ? h("div", { class: "user-files" }, names.map((n) => h("span", { class: "chip-file" }, n))) : null)), this.body);
    this.body.append(this.thinking);
  }

  put(node) { // keep the «working» line last
    this.body.insertBefore(node, this.thinking.isConnected ? this.thinking : null);
    if (!node.classList.contains("steps")) this.leadSteps = null;
    return node;
  }

  note(text, kind = "") { this.put(h("div", { class: `note ${kind}` }, text)); }

  stepsFor(agentId) {
    const a = this.agents[agentId];
    if (a) return a.steps;
    if (!this.leadSteps) this.leadSteps = this.put(h("div", { class: "steps" }));
    return this.leadSteps;
  }

  apply(e) {
    const d = e.data || {};
    const a = this.agents[e.agent_id];
    if (e.type === "agent_start") {
      if (e.agent_id === "lead") { this.leadModel = d.model; return; }
      const steps = h("div", { class: "steps" });
      const status = h("span", { class: "st run" }, "● работает");
      const head = h("div", { class: "who", onclick: () => block.classList.toggle("open") },
        h("b", {}, roleName(d.role)), h("span", { class: "task" }, d.description || ""),
        h("span", { class: "m" }, d.model || ""), status);
      const block = this.put(h("div", { class: "c-agent open", "data-testid": "agent" }, head, steps));
      this.agents[e.agent_id] = { el: block, steps, status, count: 0 };
    } else if (e.type === "tool") {
      if (d.tool === "Task" || d.tool === "TodoWrite") return; // shown as agent blocks / the plan card
      const box = this.stepsFor(e.agent_id);
      const right = h("span", { class: "r" });
      const mark = h("span", { class: "s" }, "●");
      const step = h("div", { class: "step run", "data-testid": "step" }, mark, h("span", { class: "n" }, d.tool),
        h("span", { class: "a" }, d.arg || ""), right);
      box.append(step);
      this.steps[`${e.agent_id}|${d.id}`] = { step, right, mark };
      if (a) a.count += 1;
    } else if (e.type === "tool_done") {
      const s = this.steps[`${e.agent_id}|${d.id}`];
      if (!s) return;
      s.step.classList.remove("run");
      s.step.classList.add(d.ok ? "ok" : "fail");
      s.mark.textContent = d.ok ? "✓" : "✗";
      if (d.added || d.deleted) s.right.replaceChildren(h("span", { class: "add" }, `+${d.added || 0}`), " ", d.deleted ? h("span", { class: "del" }, `−${d.deleted}`) : "");
      else if (!d.ok && d.note) s.right.textContent = d.note.split("\n")[0].slice(0, 60);
      else if (d.lines) s.right.textContent = `${d.lines} строк`;
      const detail = d.diff ? h("div", { class: "out", hidden: true }, d.diff.map((l) =>
        h("div", { class: l.startsWith("+") ? "l-a" : l.startsWith("-") ? "l-d" : "" }, l)), d.more ? h("div", { class: "faint" }, `… ещё ${d.more} строк`) : null)
        : d.output ? h("div", { class: "out", hidden: true }, d.output) : null;
      if (detail) {
        s.step.after(detail);
        s.step.classList.add("openable");
        s.step.addEventListener("click", () => { detail.hidden = !detail.hidden; });
      }
    } else if (e.type === "text") {
      if (a) a.steps.append(h("div", { class: "agent-text" }, d.text));
      else this.lastLeadText = this.put(h("div", { class: "prose" }, markdown(d.text)));
    } else if (e.type === "todos" && e.agent_id === "lead") {
      const todos = d.todos || [];
      const done = todos.filter((t) => t.status === "completed").length;
      const card = h("div", { class: "plan", "data-testid": "plan" },
        h("div", { class: "t" }, h("span", {}, "План"), h("span", {}, `${done} из ${todos.length}`)),
        h("ul", {}, todos.map((t) => h("li", { class: t.status === "completed" ? "done" : t.status === "in_progress" ? "now" : "" }, t.content))));
      if (this.plan) this.plan.replaceWith(card); else this.put(card);
      this.plan = card;
    } else if (e.type === "agent_done") {
      if (!a) return;
      const ok = d.status === "done";
      a.status.className = `st ${ok ? "ok" : "fail"}`;
      a.status.textContent = ok ? `✓ ${a.count} ${a.count === 1 ? "шаг" : a.count < 5 ? "шага" : "шагов"}` : d.status === "stopped" ? "остановлен" : "✗ ошибка";
      if (ok) a.el.classList.remove("open");
      if (d.summary && ok) a.el.append(h("div", { class: "agent-sum" }, d.summary.split("\n")[0].slice(0, 220)));
    } else if (e.type === "repo") {
      const text = { cloning: `Клонирую ${d.repo}…`, ready: `Работаю в ${d.repo}, ветка ${d.branch} (от ${d.base})`,
        committed: `Коммит ${d.sha} в рабочую ветку · файлов: ${d.files}` }[d.status];
      if (text) this.note(text, "repo-note");
    } else if (e.type === "attachment") {
      this.put(attachmentCard(this.chat, d));
    } else if (e.type === "key_rotation") {
      this.note(`Ключ ••••${d.from_tail} упёрся в лимит — запросы перешли на ••••${d.to_tail}`);
    } else if (e.type === "model_switch") {
      this.note(`Модель ${d.from} недоступна (${d.reason}) — работаю на ${d.to}`);
    } else if (e.type === "keys_waiting") {
      this.note(`Все ключи ${d.provider} заняты или в лимите — жду свободный`);
    } else if (e.type === "auto_models") {
      this.note(d.status === "started" ? "Роли ещё не настроены — подбираю самые сильные доступные модели…"
        : `Модели: ${(d.assignments || []).map((x) => `${roleName(x.role)} — ${x.model}`).join(", ")}`);
    }
  }

  async finish(turn, openChanges) {
    if (this.done) return;
    this.done = true;
    this.thinking.remove();
    if (turn.result) {
      const full = markdown(turn.result);
      if (this.lastLeadText) this.lastLeadText.replaceChildren(full);
      else this.put(h("div", { class: "prose" }, full));
    }
    if (turn.error) this.note(`Ход прерван: ${turn.error}`, "bad");
    else if (turn.status === "stopped") this.note("Остановлено", "bad");
    try {
      const ch = await api.chatChanges(this.chat, this.id);
      if (ch.files.length) {
        this.put(h("div", { class: "changes-card", "data-testid": "changes-card" },
          h("div", { class: "h" }, h("span", {}, `Изменено файлов: ${ch.files.length}`), h("span", { class: "add" }, `+${ch.added}`),
            ch.deleted ? h("span", { class: "del" }, `−${ch.deleted}`) : null, h("span", { class: "sp" }),
            h("button", { class: "small-btn", onclick: () => openChanges(this.id) }, "Открыть изменения")),
          ch.files.slice(0, 12).map((f) => h("div", { class: "f", onclick: () => openChanges(this.id, f.path) },
            h("span", {}, f.path), h("span", { class: "add" }, f.binary ? "файл" : `+${f.added}`),
            f.deleted ? h("span", { class: "del" }, `−${f.deleted}`) : h("span", {})))));
      }
    } catch { /* no changes to show */ }
  }
}

// ------------------------------------------------------------------ the view

export async function render(main, { params, setTopbar, refreshChats }) {
  const chatId = params[0] && params[0] !== "new" ? Number(params[0]) : null;
  const timers = [];
  let alive = true;
  let mode = pref.get("mode", "department");
  let model = pref.get("model", "auto");
  let running = false;
  let lastEvent = 0;
  const turns = new Map();

  const thread = h("div", { class: "thread", "data-testid": "thread" });
  const scroll = h("div", { class: "chat-scroll" }, thread);
  const input = h("textarea", { rows: 2, placeholder: chatId ? "Ответить или добавить задачу…" : "Опишите задачу…", "data-testid": "chat-input" });
  let files = [];
  const chips = h("div", { class: "chips-files", hidden: true });
  const picker = h("input", { type: "file", multiple: true, hidden: true, "data-testid": "attach-input", onchange: () => {
    files = [...files, ...picker.files].slice(0, 10);
    picker.value = "";
    paintFiles();
  } });
  const attach = h("button", { class: "attach", title: "Прикрепить файлы", "aria-label": "Прикрепить файлы", onclick: () => picker.click() }, "+");
  function paintFiles() {
    chips.hidden = !files.length;
    chips.replaceChildren(...files.map((f, i) => h("span", { class: "chip-file" }, f.name, h("button", { "aria-label": "Убрать", onclick: () => { files.splice(i, 1); paintFiles(); } }, "×"))));
  }
  const sendBtn = h("button", { class: "go", "aria-label": "Отправить", "data-testid": "send", onclick: () => (running ? stop() : send()) });
  const modeSeg = h("div", { class: "seg" });
  const modelBtn = h("button", { class: "model-btn", onclick: (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; } });
  const pop = h("div", { class: "popover", hidden: true });
  let repo = chatId ? null : pref.get("repo", "") || null;
  const repoBtn = h("button", { class: "model-btn repo-btn", hidden: Boolean(chatId), "data-testid": "repo-btn",
    onclick: (e) => { e.stopPropagation(); repoPop.hidden = !repoPop.hidden; if (!repoPop.hidden) loadRepos(); } });
  const repoPop = h("div", { class: "popover left", hidden: true, "data-testid": "repo-pop" });
  function paintRepo() { repoBtn.replaceChildren("⎇ ", repo || "Без репозитория"); }
  function pickRepo(value) { repo = value; pref.set("repo", value || ""); repoPop.hidden = true; paintRepo(); }
  async function loadRepos() {
    repoPop.replaceChildren(h("div", { class: "it" }, h("small", {}, "Загружаю…")));
    try {
      const repos = await api.githubRepos();
      repoPop.replaceChildren(h("div", { class: "it", onclick: () => pickRepo(null) }, h("b", {}, "Без репозитория"), h("small", {}, "файлы только в чате")),
        ...repos.filter((r) => r.can_push).slice(0, 40).map((r) => h("div", { class: "it", onclick: () => pickRepo(r.full_name) },
          h("b", {}, r.full_name), h("small", {}, `${r.private ? "приватный" : "публичный"} · ${r.default_branch}`))));
    } catch (err) {
      repoPop.replaceChildren(h("div", { class: "it", onclick: () => pickRepo(null) }, h("b", {}, "Без репозитория")),
        h("a", { class: "it", href: "#/settings/github" }, h("small", {}, err.status === 409 ? "Подключить GitHub…" : errorText(err))));
    }
  }
  const composer = h("div", { class: "composer-wrap" }, h("div", { class: "composer" }, chips, input,
    h("div", { class: "row" }, attach, picker, modeSeg, h("div", { class: "pop-host" }, repoBtn, repoPop), h("span", { class: "sp" }),
      h("div", { class: "pop-host" }, modelBtn, pop), sendBtn)));
  const panel = h("aside", { class: "changes-panel", hidden: true, "data-testid": "changes-panel" });
  main.append(h("div", { class: "chat-wrap" }, h("div", { class: "chat-col" }, scroll, composer), panel));
  document.addEventListener("click", closePop);
  function closePop() { pop.hidden = true; repoPop.hidden = true; }

  function paintControls() {
    modeSeg.replaceChildren(...MODES.map(([id, label]) => h("button", { class: id === mode ? "on" : "", onclick: () => { mode = id; pref.set("mode", id); paintControls(); } }, label)));
    modelBtn.replaceChildren(model === "auto" ? "Авто" : model.split(":").slice(1).join(":"), " ▾");
    sendBtn.replaceChildren(running ? h("span", { class: "stop-sq" }) : "↑");
    sendBtn.classList.toggle("stop", running);
    sendBtn.setAttribute("aria-label", running ? "Остановить" : "Отправить");
  }
  async function loadModels() {
    let roles = [];
    try { roles = await api.listRoles(); } catch { /* not set up yet */ }
    const options = [...new Set(roles.filter((r) => r.provider && r.model).map((r) => `${r.provider}:${r.model}`))];
    pop.replaceChildren(
      h("div", { class: "it", onclick: () => pick("auto") }, h("b", {}, "Авто"), h("small", {}, "модели ролей; ведёт самая сильная")),
      ...options.map((o) => h("div", { class: "it", onclick: () => pick(o) }, h("b", {}, o.split(":").slice(1).join(":")), h("small", {}, o.split(":")[0]))),
      h("a", { class: "it", href: "#/settings/models" }, h("small", {}, "Модели и роли…")));
  }
  function pick(value) { model = value; pref.set("model", value); pop.hidden = true; paintControls(); }

  const autosize = () => { input.style.height = "auto"; input.style.height = `${Math.min(240, input.scrollHeight)}px`; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!running) send(); } });

  const nearBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 160;
  const toBottom = () => { scroll.scrollTop = scroll.scrollHeight; };

  async function send() {
    const prompt = input.value.trim();
    if (!prompt) return;
    sendBtn.disabled = true;
    try {
      const body = { prompt, mode, model: model === "auto" ? null : model };
      if (!chatId) {
        if (repo) body.repo = repo;
        const chat = await api.createChat(body, files);
        input.value = "";
        files = [];
        refreshChats();
        location.hash = `#/chat/${chat.id}`;
        return;
      }
      await api.sendChatMessage(chatId, body, files);
      input.value = "";
      files = [];
      paintFiles();
      autosize();
      await refresh(true);
      toBottom();
    } catch (err) {
      toast(errorText(err), { error: true });
    } finally {
      sendBtn.disabled = false;
    }
  }
  async function stop() {
    try { await api.stopChat(chatId); toast("Останавливаю…"); } catch (err) { toast(errorText(err), { error: true }); }
  }

  // ---- changes panel
  async function openChanges(runId, path) {
    panel.hidden = false;
    const list = h("div", { class: "ch-files" });
    const view = h("div", { class: "ch-diff" });
    panel.replaceChildren(h("div", { class: "ch-head" }, h("b", {}, runId ? "Изменения хода" : "Все изменения чата"), h("span", { class: "sp" }),
      h("button", { class: "small-btn", onclick: download }, "Скачать .zip"),
      h("button", { class: "icon-x", "aria-label": "Закрыть", onclick: () => { panel.hidden = true; } }, "✕")), list, view);
    const ch = await api.chatChanges(chatId, runId);
    if (!ch.files.length) { view.replaceChildren(h("p", { class: "muted pad" }, "Файлы не менялись.")); return; }
    const show = async (f) => {
      list.querySelectorAll(".row").forEach((r) => r.classList.toggle("on", r.dataset.path === f));
      const text = await api.chatDiff(chatId, f, runId);
      view.replaceChildren(h("div", { class: "diff" }, text.split("\n").filter((l) => !/^(diff --git|index |--- |\+\+\+ |new file mode|similarity|rename )/.test(l))
        .map((l) => h("div", { class: l.startsWith("+") ? "l-a" : l.startsWith("-") ? "l-d" : l.startsWith("@@") ? "l-h" : "" }, l || " "))));
    };
    list.replaceChildren(...ch.files.map((f) => h("div", { class: "row", dataset: { path: f.path }, onclick: () => show(f.path) },
      h("span", {}, f.path), h("span", { class: "add" }, f.binary ? "файл" : `+${f.added}`), f.deleted ? h("span", { class: "del" }, `−${f.deleted}`) : h("span", {}))));
    show(path || ch.files[0].path);
  }
  async function download() {
    const blob = await api.chatArchive(chatId);
    const a = h("a", { href: URL.createObjectURL(blob), download: `chat-${chatId}.zip` });
    document.body.append(a); a.click(); a.remove();
  }

  // ---- loading and live updates
  async function applyEvents(events) {
    for (const e of events) {
      lastEvent = Math.max(lastEvent, e.id);
      turns.get(e.run_id)?.apply(e);
    }
  }
  async function refresh(force) {
    if (!alive || !chatId) return;
    const stick = nearBottom();
    const chat = await api.getChat(chatId);
    let total = 0;
    for (const t of chat.turns) {
      total += t.tokens;
      if (!turns.has(t.id)) {
        const view = new Turn(t, chatId);
        turns.set(t.id, view);
        thread.append(view.el);
      }
    }
    for (;;) { // every event so far (in pages)
      const events = await api.chatEvents(chatId, lastEvent);
      await applyEvents(events);
      if (events.length < 1000) break;
    }
    for (const t of chat.turns) {
      if (!t.live && ["done", "failed", "stopped"].includes(t.status)) await turns.get(t.id).finish(t, openChanges);
    }
    const wasRunning = running;
    running = chat.status === "running";
    paintControls();
    const changes = running || force || wasRunning !== running || !headerChanges ? await api.chatChanges(chatId).catch(() => null) : null;
    if (changes) headerChanges = changes;
    const prBtn = chat.repo ? h("button", { class: "small-btn", "data-testid": "pr-btn", disabled: running || !chat.ahead,
      title: chat.ahead ? `Отправить ветку ${chat.branch} и ${chat.pr_url ? "обновить" : "открыть"} pull request` : "Пока нет коммитов в рабочей ветке",
      onclick: async (e) => {
        e.target.disabled = true;
        try { const res = await api.openChatPr(chatId); toast("Pull request готов"); window.open(res.pr_url, "_blank", "noopener"); refresh(true); } catch (err) { toast(errorText(err), { error: true }); e.target.disabled = false; }
      } }, chat.pr_url ? "Обновить PR" : "Создать PR") : null;
    setTopbar(chat.title,
      h("span", { class: "tb-meta" }, running ? h("span", { class: "live" }) : null, `${kTok(total)} токенов`),
      chat.repo ? h("span", { class: "tb-meta repo-chip", title: `ветка ${chat.branch}` }, `⎇ ${chat.repo}`) : null,
      chat.pr_url ? h("a", { class: "small-btn", href: chat.pr_url, target: "_blank", rel: "noopener" }, "PR ↗") : null,
      prBtn,
      headerChanges?.files.length ? h("button", { class: "small-btn", "data-testid": "changes-btn", onclick: () => openChanges(null) },
        "Изменения ", h("span", { class: "add" }, `+${headerChanges.added}`), headerChanges.deleted ? h("span", { class: "del" }, ` −${headerChanges.deleted}`) : "") : null,
      h("button", { class: "icon-x", title: "Удалить чат", onclick: remove }, "🗑"));
    if (wasRunning && !running) refreshChats();
    if (stick) toBottom();
  }
  let headerChanges = null;
  async function remove() {
    if (!(await confirmDialog("Удалить чат?", "Переписка и файлы этого чата будут удалены.", "Удалить"))) return;
    await api.deleteChat(chatId);
    refreshChats();
    location.hash = "#/chat/new";
  }

  paintControls();
  paintRepo();
  loadModels();
  if (!chatId) {
    setTopbar("Новый чат");
    thread.append(h("div", { class: "empty-chat" }, h("h1", {}, "Что сделаем?"),
      h("p", {}, "Опишите задачу — отдел агентов разберёт её, сделает и покажет всё здесь."),
      h("div", { class: "sugg" }, SUGGESTIONS.map(([a, b]) => h("button", { onclick: () => { input.value = `${a}: ${b}`; autosize(); input.focus(); } }, h("b", {}, a), h("span", {}, b))))));
    input.focus();
  } else {
    try {
      await refresh(true);
    } catch (err) {
      thread.replaceChildren(h("div", { class: "empty-chat" }, h("h1", {}, "Чат не найден"), h("p", {}, errorText(err))));
      return () => {};
    }
    toBottom();
    const tick = async () => {
      if (!alive) return;
      try { await refresh(false); } catch { /* the next tick tries again */ }
      timers.push(setTimeout(tick, running ? 1000 : 6000));
    };
    timers.push(setTimeout(tick, 1000));
  }
  return () => { alive = false; timers.forEach(clearTimeout); document.removeEventListener("click", closePop); };
}
