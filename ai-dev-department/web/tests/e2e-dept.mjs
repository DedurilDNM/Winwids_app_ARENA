// E2E: the API department through the UI, against a fake OpenAI-compatible aggregator
// (web/tests/fake_llm.py on :9200) added as a «custom» provider. Two keys go into the
// pool through the UI - one always rate-limited - the roles get the fake model, a run
// is started from the UI and finishes: lead + parallel Coder and Tester, a key
// rotation in the feed, the file in the workspace.
//
//   python3 web/tests/fake_llm.py 9200 &   FRESH=1 web/tests/e2e-server.sh
//   NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node web/tests/e2e-dept.mjs
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const LLM = process.env.LLM || "http://127.0.0.1:9200/v1";
const SHOTS = process.env.SHOTS || "";
async function call(method, path, body, token) {
  const res = await fetch(`${BASE}${path}`, {
    method, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${text}`);
  return text ? JSON.parse(text) : null;
}
const { access_token: T } = await call("POST", "/api/auth/login", { email: "admin@local", password: process.env.ADMIN_PASSWORD });

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1366, height: 900 } })).newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`${BASE}/app/#/department/keys`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=key-summary]");

// two keys through the UI: the «limited» one first, so the first call must rotate
for (const [secret, label] of [["sk-limited-1111", "Лимитный"], ["sk-good-2222", "Рабочий"]]) {
  await page.getByRole("button", { name: "Ключ в котёл" }).first().click();
  await page.selectOption("[data-testid=key-provider]", "custom");
  await page.fill("[data-testid=key-secret]", secret);
  await page.fill("input[placeholder^='Название']", label);
  await page.fill("input[placeholder^='https://']", LLM);
  await page.getByRole("button", { name: "Проверить и добавить" }).click();
  await page.waitForSelector(`text=${label}`);
}
assert.equal(await page.locator("[data-testid=key]").count(), 2);

// roles: Orchestrator, Coder, Tester on the fake model (models come from the provider)
await page.goto(`${BASE}/app/#/department/roles`);
await page.waitForSelector("[data-testid=role]");
for (const role of ["Оркестратор", "Кодер", "Тестер"]) {
  const card = page.locator("[data-testid=role]", { hasText: role }).first();
  await card.locator("select").first().selectOption("custom");
  await card.locator("input[list]").fill("fake-model");
  await card.getByRole("button", { name: "Сохранить" }).click();
  await page.waitForSelector(`text=${role}: сохранено`);
}

// the run
await page.goto(`${BASE}/app/#/department/runs`);
await page.fill("[data-testid=run-prompt]", "Сделай hello.py и проверь запуск");
await page.click("[data-testid=run-start]");
await page.waitForSelector("[data-testid=agents] [data-testid=agent]");
await page.waitForSelector("[data-testid=result]:not([hidden])", { timeout: 60000 });
await page.waitForSelector("text=hello.py", { timeout: 10000 });
if (SHOTS) await page.screenshot({ path: `${SHOTS}/dept-run.png`, fullPage: true });

const runs = await call("GET", "/api/runs", null, T);
const run = await call("GET", `/api/runs/${runs[0].id}`, null, T);
assert.equal(run.status, "done", JSON.stringify(run));
assert.deepEqual(run.agents.map((a) => a.role).sort(), ["Coder", "Orchestrator", "Tester"]);
const events = await call("GET", `/api/runs/${run.id}/events`, null, T);
assert.ok(events.some((e) => e.type === "key_rotation"), "a key rotation is in the feed");
const ev = (type, agent) => events.find((e) => e.type === type && e.agent_id === agent).id;
assert.ok(ev("agent_start", "lead.2") < ev("agent_done", "lead.1"), "the Tester started before the Coder finished");
assert.ok(await page.locator(".ev-key_rotation").count() >= 1);
assert.match(await page.locator("[data-testid=result]").innerText(), /hello\.py/);
const keys = (await call("GET", "/api/keys", null, T)).keys;
assert.equal(keys.find((k) => k.tail === "1111").status, "limited");

// the key pool and the run on a phone
const phone = await (await browser.newContext({ ...devices["iPhone 13"] })).newPage();
await phone.goto(`${BASE}/app/`);
await phone.fill("input[name=email]", "admin@local");
await phone.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await phone.click("button[type=submit]");
await phone.goto(`${BASE}/app/#/department/runs/${run.id}`);
await phone.waitForSelector("[data-testid=agent]");
assert.ok(!(await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)), "no sideways scroll");
if (SHOTS) await phone.screenshot({ path: `${SHOTS}/dept-run-phone.png` });
await phone.goto(`${BASE}/app/#/department/keys`);
await phone.waitForSelector("[data-testid=key]");
if (SHOTS) await phone.screenshot({ path: `${SHOTS}/dept-keys-phone.png`, fullPage: true });

assert.deepEqual(errors, []);
await browser.close();
console.log("e2e-dept: OK");
