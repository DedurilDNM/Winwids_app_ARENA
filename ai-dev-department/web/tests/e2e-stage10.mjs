// E2E: an endless captcha that only clears after a page reload. The human
// opens the agent's browser from the «нужна помощь» banner and presses F5
// through VNC; the server notices the reload, sends the task again, and the
// project completes.
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
  name: "F5", description: "", task: "One part", github_repo: "octo/website", max_rounds: 0, ci_enabled: false,
  orchestrator_account_id: ids.boss, assignments: [{ account_id: ids.dev, role: "Coder" }],
}, T);

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1366, height: 900 } })).newPage();
await page.goto(`${BASE}/app/#/projects/${project.id}`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=live].on");
await page.click("button:has-text('Запустить')");

let helped = 0;
const deadline = Date.now() + 240000;
while (Date.now() < deadline) {
  const status = await page.textContent("[data-testid=project-status]");
  if (/Завершён|ошибка/.test(status)) break;
  if (await page.locator("[data-testid=attention]").count()) {
    // «нужна помощь» -> open the agent's browser -> F5 inside the remote browser
    await page.click("[data-testid=attention] button:has-text('Открыть браузер')");
    await page.waitForFunction(() => document.querySelector(".browser-status")?.textContent.includes("Подключено"), null, { timeout: 30000 });
    await page.waitForTimeout(1500);
    const vnc = page.frames().find((f) => f.url().includes("/vnc.html"));
    await vnc.locator("canvas").filter({ visible: true }).first().click({ position: { x: 600, y: 400 } });
    await page.keyboard.press("F5");
    if (SHOTS && !helped) await page.screenshot({ path: `${SHOTS}/s10-f5.png` });
    await page.waitForTimeout(3000);
    await page.click(".browser-overlay button[aria-label='Закрыть']");
    helped += 1;
    await page.waitForSelector("[data-testid=attention]", { state: "detached", timeout: 60000 });
    console.log(`✓ captcha #${helped}: pressed F5 in the agent's browser through VNC, the banner cleared`);
  }
  await page.waitForTimeout(1000);
}
const status = await page.textContent("[data-testid=project-status]");
const result = await page.textContent("[data-testid=result]").catch(() => "");
await browser.close();
assert.ok(helped >= 2, `expected to help the orchestrator and the agent, helped ${helped}`);
assert.match(status, /Завершён/, `project did not complete: ${result}`);
console.log("ALL RELOAD (F5) E2E CHECKS PASSED");
