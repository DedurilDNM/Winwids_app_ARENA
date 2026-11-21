// E2E: a full project through the REAL generic adapter and real account
// browsers (Xvfb/VNC), against the fake service in Agent mode with a
// ProseMirror-like editor and a captcha on the first send. Asserts the
// «нужна помощь» banner appears and clears, and the project completes with a PR.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

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
const ids = {};
for (const [name, role] of [["boss", "Orchestrator"], ["dev", "Coder"]]) {
  const acc = await call("POST", "/api/accounts", { name, default_role: role }, T);
  await call("POST", `/api/accounts/${acc.id}/confirm-login`, null, T);
  ids[name] = acc.id;
}
const project = await call("POST", "/api/projects", {
  name: "Real adapter run", description: "", task: "Make two parts", github_repo: "octo/website",
  orchestrator_account_id: ids.boss, assignments: [{ account_id: ids.dev, role: "Coder" }],
}, T);
console.log("✓ setup: 2 logged-in accounts, project created");

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(e.message));
await page.goto(`${BASE}/app/#/projects/${project.id}`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=live].on");
assert.equal(await page.locator(".attention-bar").isVisible(), false, "no banner before anything happens");
await page.click("button:has-text('Запустить')");

await page.waitForSelector("[data-testid=attention]:has-text('капчу')", { timeout: 60000 });
await page.waitForSelector("[data-testid=feed] li:has-text('капчу')");
console.log("✓ captcha after the first send -> «нужна помощь» banner + feed event");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s6-attention.png` });
await page.waitForSelector(".attention-bar", { state: "hidden", timeout: 60000 });
await page.waitForSelector("[data-testid=feed] li:has-text('Проверка пройдена')");
console.log("✓ captcha cleared -> banner gone, agent continues");

await page.waitForSelector("[data-testid=project-status]:has-text('Завершён')", { timeout: 240000 });
assert.ok(await page.locator("[data-testid=pr-link]").count() === 1);
assert.equal(await page.locator("[data-testid=task-status]:has-text('Готово')").count(), 1);
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s6-done.png`, fullPage: true });
console.log("✓ project completed through the real adapter: task done, PR link");
await browser.close();
assert.deepEqual(problems, []);
console.log("ALL REAL-ADAPTER E2E CHECKS PASSED");
