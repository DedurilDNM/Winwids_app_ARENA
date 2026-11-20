// E2E stage 3: GitHub connection page (server runs with an in-memory GitHub).
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const SHOTS = process.env.SHOTS || "";
const TOKEN = "ghp_test_token_1234567890"; // accepted by tests/fake_github.py
const problems = [];
// Start from a disconnected state regardless of earlier test runs.
const login = await (await fetch(`${BASE}/api/auth/login`, {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: "admin@local", password: process.env.ADMIN_PASSWORD }),
})).json();
await fetch(`${BASE}/api/github`, { method: "DELETE", headers: { authorization: `Bearer ${login.access_token}` } });

const browser = await chromium.launch();

for (const [label, opts] of [["desktop", { viewport: { width: 1280, height: 860 } }], ["phone", devices["Pixel 7"]]]) {
  const page = await (await browser.newContext(opts)).newPage();
  page.on("pageerror", (e) => problems.push(`${label} pageerror: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && !/status of 400/.test(m.text()) && problems.push(`${label} console: ${m.text()}`));
  await page.goto(`${BASE}/app/#/github`);
  await page.fill("input[name=email]", "admin@local");
  await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
  await page.click("button[type=submit]");
  await page.waitForSelector("h1:has-text('GitHub')");

  await page.fill("input[name=token]", "ghp_wrong_token_000000000");
  await page.click("button:has-text('Подключить')");
  await page.waitForSelector(".form-error:has-text('rejected')");
  await page.fill("input[name=token]", TOKEN);
  await page.click("button:has-text('Подключить')");
  await page.waitForSelector("[data-testid=gh-status]:has-text('octo')");
  assert.ok(!(await page.content()).includes(TOKEN), "token never rendered");
  console.log(`✓ ${label}: wrong token rejected, valid token connected (login octo, hint only)`);

  await page.fill("input[name=repo]", "https://github.com/octo/website");
  await page.click("button:has-text('Проверить')");
  await page.waitForSelector("[data-testid=repo-check] .badge.ok");
  await page.fill("input[name=repo]", "octo/missing");
  await page.click("button:has-text('Проверить')");
  await page.waitForSelector("[data-testid=repo-check]:has-text('не найден')");
  assert.ok(!/\bnull\b|undefined/.test(await page.textContent("[data-testid=repo-check]")), "no null/undefined rendered");
  console.log(`✓ ${label}: repo check ok / not found`);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/s3-github-${label}.png`, fullPage: true });

  await page.click("button:has-text('Отключить')");
  await page.click(".modal button:has-text('Отключить')");
  await page.waitForSelector("input[name=token]");
  console.log(`✓ ${label}: disconnect`);
}
await browser.close();
assert.deepEqual(problems, [], problems.join("\n"));
console.log("ALL STAGE 3 E2E CHECKS PASSED");
