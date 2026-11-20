// E2E stage 4: project wizard → start → live tasks/events → PR, on desktop;
// then the project screen on a phone. Server: e2e_server.py with
// E2E_FAKE_GITHUB=1 E2E_FAKE_AGENTS=1 (real VNC stack, Chromium as browser).
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const SHOTS = process.env.SHOTS || "";
const problems = [];

// ---- setup through the API: GitHub token + three logged-in accounts
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
await call("PUT", "/api/github", { token: "ghp_test_token_1234567890" }, T);
for (const [name, role] of [["boss", "Orchestrator"], ["dev", "Coder"], ["qa", "Reviewer"]]) {
  const acc = await call("POST", "/api/accounts", { name, default_role: role }, T);
  await call("POST", `/api/accounts/${acc.id}/confirm-login`, null, T);
}
console.log("✓ setup: GitHub connected, 3 active accounts");

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1366, height: 900 } })).newPage();
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && m.location().url.startsWith(BASE) && problems.push(`console: ${m.text()}`));
await page.goto(`${BASE}/app/#/projects`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.click("a:has-text('Новый проект')");

// step 1
await page.fill("input[name=name]", "Лендинг");
await page.fill("input[name=repo]", "octo/missing");
await page.click("button:has-text('Проверить')");
await page.waitForSelector("[data-testid=wizard-repo-check] .badge.danger");
await page.fill("input[name=repo]", "https://github.com/octo/website");
await page.fill("textarea[name=task]", "Сделать лендинг кофейни");
await page.click("button:has-text('Далее: команда')");
// step 2
await page.waitForSelector("select[name=orchestrator]");
assert.equal(await page.locator("select[name=orchestrator] option:checked").textContent(), "boss");
await page.click("button:has-text('Далее')");
await page.waitForSelector(".form-error:has-text('хотя бы одного')");
for (const name of ["dev", "qa"]) await page.check(`.team-row:has-text('${name}') input[type=checkbox]`);
assert.equal(await page.textContent(".form-error"), "", "error cleared after selecting");
await page.selectOption(".team-row:has-text('qa') select", "Tester");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s4-wizard-team.png` });
await page.click("button:has-text('Далее')");
// step 3
await page.waitForSelector("dd:has-text('qa — Тестировщик')");
await page.click("button:has-text('Создать и запустить')");
console.log("✓ wizard: repo check, orchestrator preselected, agents + roles, create & start");

await page.waitForSelector("[data-testid=live].on");
await page.waitForSelector("[data-testid=feed] li:has-text('начата')");
await page.waitForSelector("[data-testid=task-status]:has-text('В работе')");
console.log("✓ live: WebSocket online, task started events, a task «В работе»");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s4-project-running.png` });

await page.waitForSelector("[data-testid=project-status]:has-text('Завершён')", { timeout: 60000 });
assert.equal(await page.locator("[data-testid=task-status]:has-text('Готово')").count(), 2);
assert.ok((await page.textContent("[data-testid=result]")).includes("Лендинг готов"));
assert.ok(!(await page.textContent("[data-testid=result]")).includes("##"), "markdown rendered");
assert.equal(await page.locator(".task .card-title:has-text('Тестировщик')").count(), 1, "project role (Tester) wins over default (Reviewer)");
assert.match(await page.getAttribute("[data-testid=pr-link]", "href"), /github\.com\/octo\/website\/pull\/\d+/);
assert.ok(await page.locator(".task a:has-text('коммит')").count() === 2);
await page.waitForSelector("[data-testid=feed] li:has-text('Проект завершён')");
console.log("✓ completed: 2 tasks done with commit links, result + PR link, feed shows completion");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s4-project-done.png`, fullPage: true });
const projectUrl = page.url();

// ---- phone: list + project screen
const m = await (await browser.newContext({ ...devices["Pixel 7"] })).newPage();
m.on("pageerror", (e) => problems.push(`phone pageerror: ${e.message}`));
await m.goto(`${BASE}/app/#/projects`);
await m.fill("input[name=email]", "admin@local");
await m.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await m.click("button[type=submit]");
await m.click("[data-project-id]");
await m.waitForSelector("[data-testid=project-status]:has-text('Завершён')");
assert.equal(await m.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
if (SHOTS) await m.screenshot({ path: `${SHOTS}/s4-project-phone.png`, fullPage: true });
console.log("✓ phone: project screen fits, no horizontal scroll");

// ---- re-run is blocked for completed, delete works
await page.goto(projectUrl);
await page.click("button:has-text('Удалить')");
await page.click(".modal button:has-text('Удалить')");
await page.waitForSelector(".empty:has-text('Пока нет проектов')");
console.log("✓ delete project");

await browser.close();
assert.deepEqual(problems, [], problems.join("\n"));
console.log("ALL STAGE 4 E2E CHECKS PASSED");
