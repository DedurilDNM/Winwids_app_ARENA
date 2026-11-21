// E2E regression: watching an agent must not break its work.
// After a server restart (browsers restored), start a project through the
// REAL adapter; while the agent works open «Смотреть агента», wait, close
// it - the project must still complete.
import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
async function call(method, path, body, token, retry = 3) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
    method, headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  } catch (err) {  // a pooled connection from before a server restart
    if (retry > 0) return call(method, path, body, token, retry - 1);
    throw err;
  }
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${text}`);
  return text ? JSON.parse(text) : null;
}
const login = async () => (await call("POST", "/api/auth/login", { email: "admin@local", password: process.env.ADMIN_PASSWORD })).access_token;
let T = await login();
await call("PUT", "/api/github", { token: "ghp_test_token_1234567890" }, T);
const ids = {};
for (const [name, role] of [["boss", "Orchestrator"], ["dev", "Coder"]]) {
  const acc = await call("POST", "/api/accounts", { name, default_role: role }, T);
  await call("POST", `/api/accounts/${acc.id}/confirm-login`, null, T);
  ids[name] = acc.id;
}
execSync(process.env.RESTART_CMD, { stdio: "inherit" });  // browsers come back from the saved sessions
T = await login();
const project = await call("POST", "/api/projects", {
  name: "Watch me", description: "", task: "One part", github_repo: "octo/website", max_rounds: 0, ci_enabled: false,
  orchestrator_account_id: ids.boss, assignments: [{ account_id: ids.dev, role: "Coder" }],
}, T);
console.log("✓ setup: accounts restored after a server restart, project created");

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
await page.goto(`${BASE}/app/#/projects/${project.id}`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=live].on");
await page.click("button:has-text('Запустить')");
await page.waitForSelector(".task button:has-text('Смотреть агента')", { timeout: 90000 });
await page.click(".task button:has-text('Смотреть агента')");
await page.waitForFunction(() => document.querySelector(".browser-status")?.textContent.includes("Подключено"), null, { timeout: 30000 });
await page.waitForTimeout(3000);
await page.click(".browser-overlay button[aria-label='Закрыть']");
console.log("✓ opened and closed the agent's browser while it was working");
await page.waitForSelector("[data-testid=project-status]:has-text('Завершён'), [data-testid=project-status]:has-text('ошибка')", { timeout: 120000 });
const status = await page.textContent("[data-testid=project-status]");
const result = await page.textContent("[data-testid=result]").catch(() => "");
await browser.close();
assert.match(status, /Завершён/, `project did not complete: ${result}`);
console.log("ALL WATCH-AGENT E2E CHECKS PASSED");
