// E2E «big department»: wizard with review rounds, Architect then 2 Coders
// in parallel (scripted agents), areas and dependencies on the cards, agent
// messages panel, a review round with a fix task, completion.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
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
await call("PUT", "/api/github", { token: "ghp_test_token_1234567890" }, T);
for (const [name, role] of [["boss", "Orchestrator"], ["arch", "Architect"], ["c1", "Coder"], ["c2", "Coder"]]) {
  const acc = await call("POST", "/api/accounts", { name, default_role: role }, T);
  await call("POST", `/api/accounts/${acc.id}/confirm-login`, null, T);
}

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1366, height: 1000 } })).newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(e.message));
await page.goto(`${BASE}/app/#/projects/new`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.fill("input[name=name]", "Отдел");
await page.fill("input[name=repo]", "octo/website");
await page.fill("textarea[name=task]", "Сайт из двух модулей");
await page.click("button:has-text('Далее: команда')");
await page.waitForSelector("select[name=orchestrator]");
for (const n of ["arch", "c1", "c2"]) await page.check(`.team-row:has-text('${n}') input[type=checkbox]`);
await page.click("button:has-text('Далее')");
await page.selectOption("select[name=max_rounds]", "1");
await page.click("button:has-text('Создать и запустить')");
console.log("✓ wizard with review rounds");

await page.waitForSelector("[data-testid=scope] code:has-text('src/m3/')", { timeout: 30000 });
assert.ok((await page.textContent(".task:has-text('Build module 3')")).includes("после #"), "dependency shown");
await page.waitForSelector("[data-testid=parallel]:has-text('2')", { timeout: 60000 });
console.log("✓ 2 coders work in parallel after the architect; areas and dependencies on the cards");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s7-parallel.png` });
await page.waitForSelector("[data-testid=messages] li:has-text('follow docs/CONTRACTS.md')");
console.log("✓ «Переписка агентов» shows the architect's message");
await page.waitForSelector(".round-title:has-text('Доработки — круг 1')", { timeout: 90000 });
console.log("✓ orchestrator review created a fix round");
await page.waitForSelector("[data-testid=project-status]:has-text('Завершён')", { timeout: 120000 });
assert.ok(await page.locator("[data-testid=messages] li:has-text('Good, one fix needed')").count() === 1);
assert.equal(await page.locator("[data-testid=task-status]:has-text('Готово')").count(), 4);
assert.ok(!/\bnull\b|undefined/.test(await page.textContent("main")), "no null/undefined rendered");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s7-done.png`, fullPage: true });
console.log("✓ completed: 4 tasks done, orchestrator answer in the messages");

const m = await (await browser.newContext({ ...devices["Pixel 7"] })).newPage();
await m.goto(page.url());
await m.fill("input[name=email]", "admin@local");
await m.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await m.click("button[type=submit]");
await m.waitForSelector(".round-title");
assert.equal(await m.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
if (SHOTS) await m.screenshot({ path: `${SHOTS}/s7-phone.png`, fullPage: true });
console.log("✓ phone layout fits");
await browser.close();
assert.deepEqual(problems, []);
console.log("ALL DEPARTMENT E2E CHECKS PASSED");
