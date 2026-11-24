// Mock data + a tiny live simulation shared by both interface prototypes.
// Nothing here talks to the server: it only shows how the screens behave.
(function () {
  const now = () => Date.now();
  const PROVIDERS = {
    openrouter: { name: "OpenRouter", short: "OR", color: "#7c5cff" },
    anthropic: { name: "Anthropic", short: "AN", color: "#d97757" },
    groq: { name: "Groq", short: "GQ", color: "#f55036" },
    deepseek: { name: "DeepSeek", short: "DS", color: "#4d6bfe" },
  };
  const keys = [
    { id: 1, provider: "openrouter", label: "Основной", tail: "a91f", status: "available", busy: 2, used: 1840, nextCheck: 0 },
    { id: 2, provider: "openrouter", label: "Запасной", tail: "07c2", status: "available", busy: 1, used: 920, nextCheck: 0 },
    { id: 3, provider: "openrouter", label: "Друг", tail: "5e10", status: "limited", busy: 0, used: 3110, reason: "429 · лимит запросов", nextCheck: now() + 192e3 },
    { id: 4, provider: "openrouter", label: "Бесплатный", tail: "c4d8", status: "available", busy: 0, used: 44, nextCheck: 0 },
    { id: 5, provider: "anthropic", label: "Рабочий", tail: "Q7mA", status: "available", busy: 1, used: 610, nextCheck: 0 },
    { id: 6, provider: "anthropic", label: "Личный", tail: "x2Lp", status: "limited", busy: 0, used: 2200, reason: "402 · кредиты", nextCheck: now() + 61e3 },
    { id: 7, provider: "groq", label: "Groq 1", tail: "9kTz", status: "available", busy: 0, used: 330, nextCheck: 0 },
    { id: 8, provider: "deepseek", label: "DeepSeek", tail: "f00d", status: "invalid", busy: 0, used: 0, reason: "401 · ключ отозван", nextCheck: now() + 3300e3 },
  ];
  const skills = [
    { name: "code-review", desc: "Проверка изменений: ошибки, безопасность, читаемость", source: "встроенный", roles: ["Ревьюер"] },
    { name: "write-tests", desc: "Тесты к изменённому коду: pytest, vitest, playwright", source: "встроенный", roles: ["Тестер", "Кодер"] },
    { name: "frontend-design", desc: "Интерфейсы: сетка, типографика, состояния, адаптив", source: "ваш", roles: ["Кодер"] },
    { name: "git-workflow", desc: "Ветки, коммиты, описание PR", source: "встроенный", roles: ["Интегратор"] },
    { name: "api-design", desc: "REST-контракты, ошибки, версии", source: "ваш", roles: ["Архитектор"] },
  ];
  const TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "TodoWrite", "Skill", "Task"];
  const roles = [
    { role: "Оркестратор", glyph: "◆", hue: 265, provider: "anthropic", model: "claude-opus-5-5", effort: "high", skills: [], tools: ["Read", "Glob", "Grep", "TodoWrite", "Task"] },
    { role: "Архитектор", glyph: "▲", hue: 200, provider: "openrouter", model: "google/gemini-4-pro", effort: "high", skills: ["api-design"], tools: ["Read", "Glob", "Grep", "Write", "WebFetch"] },
    { role: "Кодер", glyph: "●", hue: 150, provider: "openrouter", model: "anthropic/claude-sonnet-5-5", effort: "medium", skills: ["frontend-design", "write-tests"], tools: ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"] },
    { role: "Ревьюер", glyph: "■", hue: 35, provider: "deepseek", model: "deepseek-chat", effort: "medium", skills: ["code-review"], tools: ["Read", "Glob", "Grep", "Skill"] },
    { role: "Тестер", glyph: "✚", hue: 330, provider: "groq", model: "qwen-3-coder", effort: "low", skills: ["write-tests"], tools: ["Read", "Write", "Bash", "Skill"] },
  ];
  // the run: a lead agent and its sub-agents (a tree), steps arrive over time
  const run = {
    title: "Pomodoro-таймер: PWA с историей",
    started: now() - 4 * 60e3,
    agents: [
      { id: "lead", parent: null, role: "Оркестратор", status: "working", tokens: 18400, task: "План и распределение" },
      { id: "arch", parent: "lead", role: "Архитектор", status: "done", tokens: 9100, task: "Структура и API хранения" },
      { id: "c1", parent: "lead", role: "Кодер", status: "working", tokens: 22800, task: "Таймер и состояние" },
      { id: "c2", parent: "lead", role: "Кодер", status: "working", tokens: 15300, task: "Экран истории" },
      { id: "t1", parent: "lead", role: "Тестер", status: "waiting", tokens: 0, task: "Тесты таймера" },
      { id: "rv", parent: "lead", role: "Ревьюер", status: "queued", tokens: 0, task: "Ревью перед PR" },
    ],
    steps: [
      { agent: "lead", kind: "text", text: "Разбиваю задачу: архитектура → два кодера параллельно → тесты → ревью" },
      { agent: "lead", kind: "tool", tool: "TodoWrite", arg: "5 пунктов", ok: true },
      { agent: "lead", kind: "tool", tool: "Task", arg: "Архитектор · структура", ok: true },
      { agent: "arch", kind: "tool", tool: "Skill", arg: "api-design", ok: true },
      { agent: "arch", kind: "tool", tool: "Write", arg: "docs/ARCHITECTURE.md", ok: true },
      { agent: "lead", kind: "tool", tool: "Task", arg: "Кодер ×2 параллельно", ok: true },
      { agent: "c1", kind: "tool", tool: "Read", arg: "docs/ARCHITECTURE.md", ok: true },
      { agent: "c2", kind: "tool", tool: "Glob", arg: "src/**/*.ts", ok: true },
      { agent: "c1", kind: "tool", tool: "Write", arg: "src/timer.ts", ok: true },
    ],
  };
  const SCRIPT = [
    { agent: "c2", kind: "tool", tool: "Write", arg: "src/history.ts" },
    { agent: "c1", kind: "tool", tool: "Edit", arg: "src/timer.ts · +18 −4" },
    { agent: "c1", kind: "tool", tool: "Bash", arg: "npm run build", ok: true },
    { agent: "c2", kind: "tool", tool: "Skill", arg: "frontend-design" },
    { agent: "c2", kind: "tool", tool: "Edit", arg: "src/history.ts · +40" },
    { agent: "c1", kind: "status", status: "done" },
    { agent: "t1", kind: "status", status: "working" },
    { agent: "t1", kind: "tool", tool: "Write", arg: "tests/timer.test.ts" },
    { agent: "t1", kind: "tool", tool: "Bash", arg: "npx vitest run", ok: false, note: "1 тест упал" },
    { agent: "t1", kind: "text", text: "Таймер не сбрасывается после паузы — сообщаю кодеру" },
    { agent: "c2", kind: "status", status: "done" },
    { agent: "c1", kind: "status", status: "working" },
    { agent: "c1", kind: "tool", tool: "Edit", arg: "src/timer.ts · reset()" },
    { agent: "t1", kind: "tool", tool: "Bash", arg: "npx vitest run", ok: true, note: "12/12" },
    { agent: "c1", kind: "status", status: "done" },
    { agent: "t1", kind: "status", status: "done" },
    { agent: "rv", kind: "status", status: "working" },
    { agent: "rv", kind: "tool", tool: "Skill", arg: "code-review" },
    { agent: "rv", kind: "tool", tool: "Grep", arg: "localStorage" },
    { agent: "rv", kind: "text", text: "Замечаний нет, можно в PR" },
    { agent: "rv", kind: "status", status: "done" },
    { agent: "lead", kind: "text", text: "Готово: 6 файлов, тесты 12/12, ветка aidd/pomodoro" },
    { agent: "lead", kind: "status", status: "done" },
  ];

  const listeners = [];
  const emit = (ev) => listeners.forEach((fn) => fn(ev));
  let i = 0;
  function tick() {
    // a key hits a rate limit now and then: the pool rotates to a free key
    if (i === 3) {
      const k = keys.find((x) => x.id === 2);
      k.status = "limited"; k.busy = 0; k.reason = "429 · лимит запросов"; k.nextCheck = now() + 300e3;
      emit({ type: "rotation", from: k, to: keys.find((x) => x.id === 4) });
      keys.find((x) => x.id === 4).busy += 1;
    }
    if (i === 12) {
      const k = keys.find((x) => x.id === 3);
      k.status = "available"; k.reason = ""; k.nextCheck = 0;
      emit({ type: "recovered", key: k });
    }
    const step = SCRIPT[i % SCRIPT.length];
    if (i >= SCRIPT.length) return;  // the run ended
    i += 1;
    const agent = run.agents.find((a) => a.id === step.agent);
    if (step.kind === "status") agent.status = step.status;
    else {
      agent.tokens += 600 + Math.round(Math.random() * 2400);
      if (agent.status === "queued" || agent.status === "waiting") agent.status = "working";
      run.steps.push({ ...step, ok: step.ok !== false });
    }
    keys.forEach((k) => { if (k.status === "available") k.used += Math.round(Math.random() * 6); });
    emit({ type: "step", step, agent });
  }
  window.AIDD_MOCK = {
    PROVIDERS, TOOLS, keys, skills, roles, run,
    on(fn) { listeners.push(fn); },
    start(ms) { setInterval(tick, ms || 1800); },
    roleOf(name) { return roles.find((r) => r.role === name); },
    fmtLeft(ts) {
      const s = Math.max(0, Math.round((ts - now()) / 1000));
      return s >= 3600 ? `${Math.round(s / 60)} мин` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    },
    elapsed() { const s = Math.round((now() - run.started) / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; },
  };
})();
