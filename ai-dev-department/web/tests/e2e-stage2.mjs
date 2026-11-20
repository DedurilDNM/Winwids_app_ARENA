// E2E stage 2: accounts + embedded browser over the REAL VNC stack.
// Needs: e2e_server.py running (Xvfb/x11vnc/noVNC + Chromium-as-browser) and
// fake_service.py as SERVICE_URL. RESTART_CMD restarts the server mid-test.
import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const SERVICE_LOG = process.env.SERVICE_LOG;
const SHOTS = process.env.SHOTS || "";
const problems = [];
const visits = () => readFileSync(SERVICE_LOG, "utf8").trim().split("\n");

const browser = await chromium.launch();
function watch(page) {
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && m.location().url.startsWith(BASE)) problems.push(`console: ${m.text()}`);
  });
}
async function login(page) {
  await page.goto(`${BASE}/app/`);
  await page.fill("input[name=email]", "admin@local");
  await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
  await page.click("button[type=submit]");
  await page.waitForSelector("h1:has-text('Аккаунты')");
}
async function vncFrame(page) {
  let frame;
  await waitFor(() => (frame = page.frames().find((f) => f.url().includes("/vnc.html"))), () => `noVNC iframe; frames=${page.frames().map((f) => f.url().slice(0, 40))}`, 30000);
  return frame;
}
async function waitNoVNC(page) {
  const frame = await vncFrame(page);
  // noVNC keeps hidden helper canvases; wait for the visible remote screen.
  const handle = await frame.waitForFunction(() => {
    const c = [...document.querySelectorAll("canvas")].find((x) => x.width > 200 && x.offsetParent !== null);
    return c ? { w: c.width, h: c.height } : null;
  }, null, { timeout: 30000 });
  return handle.jsonValue();
}

// ---------- A. add account → VNC login → «Готово» ----------
const page = await (await browser.newContext({ viewport: { width: 1366, height: 860 } })).newPage();
watch(page);
await login(page);
await page.click("button:has-text('Добавить аккаунт')");
await page.fill(".modal input[name=name]", "coder-1");
await page.selectOption(".modal select[name=role]", "Coder");
await page.fill(".modal input[name=repos]", "me/demo-repo");
await page.click(".modal button[type=submit]");
await page.waitForSelector(".browser-overlay");
const size = await waitNoVNC(page);
console.log(`✓ add account → embedded noVNC connected (remote screen ${size.w}x${size.h})`);

// Log into the fake service THROUGH VNC: keystrokes travel noVNC → x11vnc → browser.
const before = visits().length;
await page.waitForFunction(() => document.querySelector(".browser-status")?.textContent.includes("Подключено"));
await page.waitForTimeout(1500);
const vnc = await vncFrame(page);
const canvas = vnc.locator("canvas").filter({ visible: true }).first();
// remote 1600x900 is scaled into the iframe: aim at the input field (~y=160 in the scaled canvas)
await canvas.click({ position: { x: 220, y: 168 } });
await page.keyboard.type("artem", { delay: 60 });
await page.keyboard.press("Enter");
await waitFor(() => visits().slice(before).includes("VISIT user=artem"), "login typed via VNC reached the service");
console.log("✓ typed login into the remote browser via VNC");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s2-browser-desktop.png` });

await page.click("button:has-text('Готово, я вошёл')");
await page.waitForSelector(".browser-overlay", { state: "detached" });
const card = page.locator("[data-account-id]").first();
await card.locator("[data-testid=status]:has-text('Активен')").waitFor();
console.log("✓ «Готово» → confirm-login → status «Активен»");

// ---------- B. role change persists ----------
await card.locator("select[name=role]").selectOption("Reviewer");
await page.waitForSelector(".toast:has-text('Ревьюер')");
await page.reload();
await page.waitForSelector("[data-account-id]");
assert.equal(await page.locator("[data-account-id] select[name=role]").first().inputValue(), "Reviewer");
console.log("✓ role change saved");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/s2-accounts-desktop.png` });

// ---------- C. server restart → session restored logged in ----------
execSync(process.env.RESTART_CMD, { stdio: "inherit" });
await page.reload();
await page.locator("[data-account-id] [data-testid=status]:has-text('Активен')").first().waitFor();
const beforeOpen = visits().length;
await page.click("[data-account-id] button:has-text('Открыть браузер')");
await waitNoVNC(page);
await waitFor(() => visits().slice(beforeOpen).includes("VISIT user=artem"), "restored session is logged in");
assert.ok(!visits().slice(beforeOpen).includes("VISIT user=None"), "no logged-out visit after restore");
console.log("✓ after server restart: still «Активен», browser opens the service already logged in");
await page.click(".browser-overlay button[aria-label='Закрыть']");
await page.waitForSelector(".browser-overlay", { state: "detached" });

// ---------- D. phone: watch the agent's browser ----------
const m = await (await browser.newContext({ ...devices["Pixel 7"] })).newPage();
watch(m);
await login(m);
const beforePhone = visits().length;
await m.click("[data-account-id] button:has-text('Открыть браузер')");
await m.waitForSelector(".browser-overlay");
await waitNoVNC(m);
await m.waitForTimeout(1500);
assert.equal(visits().length, beforePhone, "re-attached to the live browser, no relaunch");
const fr = await m.locator(".browser-frame").boundingBox();
const vp = m.viewportSize();
assert.ok(fr.width >= vp.width - 1 && fr.height > vp.height * 0.7, "browser fills the phone screen");
assert.equal(await m.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
console.log("✓ phone: re-attaches to the live agent browser, full-screen");
if (SHOTS) await m.screenshot({ path: `${SHOTS}/s2-browser-phone.png` });
await m.click(".browser-overlay button[aria-label='Закрыть']");
await m.waitForSelector(".browser-overlay", { state: "detached" });

// ---------- E. delete ----------
await page.reload();
await page.click("[data-account-id] button:has-text('Удалить')");
await page.click(".modal button:has-text('Удалить')");
await page.waitForSelector(".empty:has-text('Пока нет аккаунтов')");
console.log("✓ account deleted");

await browser.close();
assert.deepEqual(problems, [], `browser errors:\n${problems.join("\n")}`);
console.log("ALL STAGE 2 E2E CHECKS PASSED");

async function waitFor(pred, what, ms = 40000) {
  const t = Date.now();
  while (!pred()) {
    if (Date.now() - t > ms) { if (SHOTS) await page.screenshot({ path: `${SHOTS}/fail.png` }); throw new Error(`timeout: ${typeof what === "function" ? what() : what}`); }
    await new Promise((r) => setTimeout(r, 300));
  }
}
