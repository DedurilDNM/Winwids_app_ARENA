// E2E: a task stops the project (BLOCKED once) -> «Продолжить с места
// остановки» keeps the finished tasks and completes the project.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
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
const ids = {};
for (const [name, role] of [["boss", "Orchestrator"], ["arch", "Architect"], ["dev", "Coder"]]) {
  const acc = await call("POST", "/api/accounts", { name, default_role: role }, T);
  await call("POST", `/api/accounts/${acc.id}/confirm-login`, null, T);
  ids[name] = acc.id;
}
const project = await call("POST", "/api/projects", {
  name: "Resume", description: "", task: "Site BLOCK_ONCE", github_repo: "octo/website", max_rounds: 0, ci_enabled: false,
  orchestrator_account_id: ids.boss,
  assignments: [{ account_id: ids.arch, role: "Architect" }, { account_id: ids.dev, role: "Coder" }],
}, T);

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(e.message));
await page.goto(`${BASE}/app/#/projects/${project.id}`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=live].on");
await page.click("button:has-text('Запустить')");
await page.waitForSelector("[data-testid=resume]", { timeout: 90000 });
assert.match(await page.textContent("[data-testid=result]"), /BLOCKED: need the logo/);
const archDone = await page.locator(".task:has-text('Архитектор') [data-testid=task-status]").textContent();
assert.match(archDone, /Готово/);
console.log("✓ the coder stopped the project; the architect's task is done; «Продолжить» offered");

await page.click("[data-testid=resume]");
await page.waitForSelector("[data-testid=project-status]:has-text('Завершён')", { timeout: 90000 });
assert.equal(await page.locator(".task").count(), 2, "no tasks recreated");
assert.equal(await page.locator("[data-testid=task-status]:has-text('Готово')").count(), 2);
await page.waitForSelector("[data-testid=feed] li:has-text('продолжен с места остановки')");
console.log("✓ resumed: same 2 tasks, both done, project completed");
await browser.close();
assert.deepEqual(problems, []);
console.log("ALL RESUME E2E CHECKS PASSED");
