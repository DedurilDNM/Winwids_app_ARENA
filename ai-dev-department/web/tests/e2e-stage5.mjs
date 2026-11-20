// E2E: in-app adapter diagnostics in the account's LIVE browser, against a
// fake service that needs Agent mode, shows a one-time pop-up on send and
// renders replies in [data-role=assistant]. Server env: see run in README.
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const SHOTS = process.env.SHOTS || "";
const res = await fetch(`${BASE}/api/auth/login`, {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: "admin@local", password: process.env.ADMIN_PASSWORD }),
});
const { access_token: T } = await res.json();
const acc = await (await fetch(`${BASE}/api/accounts`, {
  method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${T}` },
  body: JSON.stringify({ name: "diag", default_role: "Coder" }),
})).json();
console.log(`✓ account #${acc.id} created, its live browser opened the service`);

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(e.message));
await page.goto(`${BASE}/app/`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.click(`[data-account-id="${acc.id}"] button:has-text('Диагностика')`);
await page.click(".modal button:has-text('Запустить')");
await page.waitForSelector("[data-testid=probe-report]:not([hidden])", { timeout: 400000 });
const report = await page.textContent("[data-testid=probe-report]");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s5-diagnostics.png` });
console.log(report.split("\n").filter((l) => /setup click|login form|adapter reply|parsed|NOT sent|mode:/.test(l)).join("\n"));

assert.match(report, /mode: the account's live browser/);
assert.equal((report.match(/setup click .*: OK/g) || []).length, 2, "Mode + Agent clicks");
assert.match(report, /login form visible: no/);
assert.match(report, /adapter reply: 'PONG\d+' -> OK/);
assert.match(report, /parsed: status=DONE files=\['hello.py'\] -> OK/);
assert.ok(!/https?:\/\//.test(report), "no URLs in the report");
assert.ok(!/Switch to Agent mode|Logged in as/.test(report), "no page text in the report");
await page.click(".modal button:has-text('Копировать')");
await page.waitForSelector(".toast");
await browser.close();
assert.deepEqual(problems, []);
console.log("ALL DIAGNOSTICS E2E CHECKS PASSED");
