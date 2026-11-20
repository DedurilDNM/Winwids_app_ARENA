// E2E: login, navigation, session restore, registration, mobile layout, logout.
// Usage: BASE=http://127.0.0.1:8000 ADMIN_PASSWORD=... INVITE=... node e2e-stage1.mjs
import { createRequire } from "node:module";
const { chromium, devices } = createRequire(import.meta.url)("playwright"); // global install: run with NODE_PATH=$(npm root -g)
import assert from "node:assert/strict";

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const SHOTS = process.env.SHOTS || "";
const browser = await chromium.launch();
const problems = [];

function watch(page) {
  page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
}

async function login(page, email, password) {
  await page.fill("input[name=email]", email);
  await page.fill("input[name=password]", password);
  await page.click("button[type=submit]");
}

// ---------- desktop ----------
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await ctx.newPage();
watch(page);
await page.goto(`${BASE}/`);
assert.match(page.url(), /\/app\/$/, "root redirects to /app/");
await page.waitForSelector("input[name=email]");

await login(page, "admin@local", "wrong-password");
await page.waitForSelector(".form-error:has-text('Неверный email или пароль')");
console.log("✓ wrong password rejected");

await login(page, "admin@local", process.env.ADMIN_PASSWORD);
await page.waitForSelector("h1:has-text('Аккаунты')");
assert.match(page.url(), /#\/accounts$/);
await page.waitForSelector(".empty:has-text('Пока нет аккаунтов')");
console.log("✓ login → accounts (empty state)");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/desktop-accounts.png` });

for (const [route, title] of [["github", "GitHub"], ["projects", "Проекты"], ["profile", "Профиль"]]) {
  await page.click(`.nav a[data-route=${route}]`);
  await page.waitForSelector(`h1:has-text('${title}')`);
  assert.equal(await page.getAttribute(`.nav a[data-route=${route}]`, "aria-current"), "page");
}
assert.equal(await page.textContent("[data-testid=me-email]"), "admin@local");
console.log("✓ navigation across all sections");

await page.reload();
await page.waitForSelector("h1:has-text('Профиль')");
console.log("✓ session survives reload (refresh token)");

await page.click("button:has-text('Выйти')");
await page.waitForSelector("input[name=email]");
await page.reload();
await page.waitForSelector("input[name=email]");
console.log("✓ logout clears session");

// ---------- registration + mobile ----------
const mctx = await browser.newContext({ ...devices["Pixel 7"] });
const m = await mctx.newPage();
watch(m);
await m.goto(`${BASE}/app/`);
await m.click("button[role=tab]:has-text('Регистрация')");
const newEmail = `user${Date.now()}@example.com`;
await m.fill("input[name=email]", newEmail);
await m.fill("input[name=password]", "password123");
await m.fill("input[name=invite]", process.env.INVITE);
await m.click("button[type=submit]");
await m.waitForSelector("h1:has-text('Аккаунты')");
console.log("✓ registration with invite code (mobile)");

const nav = await m.locator(".sidebar").boundingBox();
const vp = m.viewportSize();
assert.ok(nav.y + nav.height >= vp.height - 1 && nav.width >= vp.width - 1, "bottom nav on phone");
const overflow = await m.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
assert.equal(overflow, false, "no horizontal scroll on phone");
await m.click(".nav a[data-route=profile]");
await m.waitForSelector(`[data-testid=me-email]:has-text('${newEmail}')`);
console.log("✓ mobile layout: bottom nav, no horizontal scroll");
if (SHOTS) {
  await m.click(".nav a[data-route=accounts]");
  await m.waitForSelector(".empty");
  await m.screenshot({ path: `${SHOTS}/mobile-accounts.png` });
}

const manifest = await (await m.request.get(`${BASE}/app/manifest.webmanifest`)).json();
assert.equal(manifest.display, "standalone");
const swState = await m.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.active ? "active" : "none");
console.log(`✓ PWA manifest ok, service worker: ${swState}`);

await browser.close();
const real = problems.filter((p) => !/401|Unauthorized/.test(p));
assert.deepEqual(real, [], `browser errors:\n${real.join("\n")}`);
console.log("ALL STAGE 1 E2E CHECKS PASSED");
