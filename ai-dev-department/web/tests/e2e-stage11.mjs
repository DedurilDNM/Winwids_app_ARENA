// E2E: self-calibration + preflight + pulse against a service laid out like the
// real one in text mode (no roles/data attributes, an older chat hidden in the
// page) while the .env message selector is WRONG. The orchestrator is calibrated
// with the «Калибровка» button; the coder is calibrated automatically by the
// preflight check when the project starts; the project then completes.
// Server env: see the run line in README (E2E_SERVICE_URL=...?as=e2e&real=1).
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
  name: "Calibrated", description: "", task: "One part", github_repo: "octo/website", max_rounds: 0, ci_enabled: false,
  orchestrator_account_id: ids.boss, assignments: [{ account_id: ids.dev, role: "Coder" }],
}, T);

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1366, height: 900 } })).newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`${BASE}/app/#/accounts`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");

// 1. «Калибровка» for the orchestrator account
const bossCard = page.locator(`[data-account-id="${ids.boss}"]`);
await bossCard.waitFor();
assert.match(await bossCard.locator("[data-testid=calibration]").textContent(), /не выполнена/);
await bossCard.locator("button:has-text('Калибровка')").click();
await page.click(".modal button:has-text('Запустить')");
const result = page.locator("[data-testid=calibration-result]");
await result.locator("li").first().waitFor({ timeout: 180000 });
const resultText = await result.textContent();
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s11-calibration.png` });
assert.match(resultText, /✓ Ответ читается/, `calibration failed: ${resultText}`);
assert.match(resultText, /✓ Найден индикатор/, `busy indicator not found: ${resultText}`);
console.log("✓ «Калибровка»: the answer is read, the busy indicator found");
await page.click(".modal button:has-text('Закрыть')");
await page.waitForFunction((id) => /Калибровка: ✓/.test(document.querySelector(`[data-account-id="${id}"] [data-testid=calibration]`)?.textContent || ""), ids.boss);
console.log("✓ the account card shows «Калибровка: ✓»");

// 2. start the project: preflight calibrates the coder by itself, then the run completes
await page.goto(`${BASE}/app/#/projects/${project.id}`);
await page.waitForSelector("[data-testid=live].on");
await page.click("button:has-text('Запустить')");
let pulseSeen = false;
const deadline = Date.now() + 300000;
while (Date.now() < deadline) {
  const status = await page.textContent("[data-testid=project-status]");
  if (/Завершён|ошибка/.test(status)) break;
  const pulses = await page.locator("[data-pulse], [data-testid=orch-pulse]:not([hidden])").allTextContents();
  if (pulses.some((t) => /\d+:\d\d/.test(t))) pulseSeen = true;
  await page.waitForTimeout(700);
}
const feed = await page.textContent("[data-testid=feed]");
const status = await page.textContent("[data-testid=project-status]");
const res = await page.textContent("[data-testid=result]").catch(() => "");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s11-project.png`, fullPage: true });
await browser.close();
assert.match(feed, /Аккаунт «dev»: калибровка/, "the coder was not calibrated by the preflight");
assert.match(feed, /Аккаунт «boss»: готов к работе/);
assert.match(feed, /Аккаунт «dev»: готов к работе/);
console.log("✓ preflight: boss checked quickly, dev calibrated automatically");
assert.match(status, /Завершён/, `project did not complete: ${res}`);
assert.doesNotMatch(res, /Thought for|model-x/, `the reply header leaked into the summary: ${res}`);
console.log("✓ the project completed with the WRONG .env message selector (calibrated profiles were used)");
assert.ok(pulseSeen, "no pulse was shown while agents worked");
console.log("✓ the agent pulse was shown");
assert.deepEqual(errors, []);
console.log("ALL CALIBRATION / PREFLIGHT E2E CHECKS PASSED");
