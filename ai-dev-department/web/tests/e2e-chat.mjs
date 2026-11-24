// E2E: the chat interface against a fake OpenAI-compatible aggregator (fake_llm.py on :9200).
// Settings: a key into the pool, models chosen automatically. Then a new chat: the task is
// written, the department works in the chat (plan, steps, sub-agents), the changes card
// and the changes panel with a diff; a follow-up message continues in the same files.
//
//   python3 web/tests/fake_llm.py 9200 &   FRESH=1 web/tests/e2e-server.sh
//   NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node web/tests/e2e-chat.mjs
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const LLM = process.env.LLM || "http://127.0.0.1:9200/v1";
const SHOTS = process.env.SHOTS || "";
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: SHOTS ? 2 : 1 });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`${BASE}/app/`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=chat-input]");  // a new chat is the start page

// settings: a key, then the models automatically
await page.click("[data-testid=settings]");
await page.waitForSelector("[data-testid=key-summary]");
await page.getByRole("button", { name: "Ключ в котёл" }).first().click();
await page.selectOption("[data-testid=key-provider]", "custom");
await page.fill("[data-testid=key-secret]", "sk-good-2222");
await page.fill("input[placeholder^='https://']", LLM);
await page.getByRole("button", { name: "Проверить и добавить" }).click();
await page.waitForSelector("[data-testid=key]");
await page.click("[data-testid=set-models]");
await page.click("[data-testid=auto-roles]");
await page.waitForSelector("[data-testid=auto-result] table", { timeout: 60000 });

// a new chat
await page.click("[data-testid=new-chat]");
await page.fill("[data-testid=chat-input]", "Сделай hello.py и проверь запуск");
await page.keyboard.press("Enter");
await page.waitForURL(/#\/chat\/\d+/);
await page.waitForSelector("[data-testid=step]");
await page.waitForSelector("[data-testid=agent]");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/chat-running.png` });
await page.waitForSelector("[data-testid=changes-card]", { timeout: 60000 });
assert.equal(await page.locator("[data-testid=turn]").count(), 1);
assert.ok(await page.locator("[data-testid=plan]").count() === 1, "the lead's plan is in the chat");
assert.ok(await page.locator("[data-testid=agent]").count() >= 2, "sub-agents are shown");
assert.match(await page.locator(".prose").last().innerText(), /hello\.py/);
assert.match(await page.locator("[data-testid=chat-list]").innerText(), /Сделай hello\.py/);

// open a step (the new file) and the changes panel
await page.locator("[data-testid=agent]").first().locator(".who").click();
await page.click("[data-testid=changes-btn]");
await page.waitForSelector("[data-testid=changes-panel] .diff");
assert.match(await page.locator("[data-testid=changes-panel] .diff").innerText(), /print\('Hello'\)/);
if (SHOTS) await page.screenshot({ path: `${SHOTS}/chat-changes.png` });
await page.locator("[data-testid=changes-panel] .icon-x").click();

// a follow-up in the same chat
await page.fill("[data-testid=chat-input]", "Теперь тёмная тема: поменяй приветствие");
await page.keyboard.press("Enter");
await page.waitForFunction(() => document.querySelectorAll("[data-testid=turn]").length === 2);
await page.waitForFunction(() => document.querySelectorAll("[data-testid=changes-card]").length === 2, null, { timeout: 60000 });
const second = page.locator("[data-testid=turn]").nth(1);
assert.match(await second.locator("[data-testid=changes-card]").innerText(), /hello\.py[\s\S]*\+1[\s\S]*−1/);
if (SHOTS) await page.screenshot({ path: `${SHOTS}/chat-done.png` });

// files: an attachment goes with the message; the department sends back a live prototype and a table
const fs = await import("node:fs");
fs.writeFileSync("/tmp/aidd-e2e-brief.csv", "задача,срок\nтаймер,1 день\n");
await page.setInputFiles("[data-testid=attach-input]", "/tmp/aidd-e2e-brief.csv");
assert.match(await page.locator(".chips-files").innerText(), /aidd-e2e-brief\.csv/);
await page.fill("[data-testid=chat-input]", "Сделай прототип экрана и таблицу итогов");
await page.keyboard.press("Enter");
await page.waitForFunction(() => document.querySelectorAll("[data-testid=attachment]").length === 2, null, { timeout: 60000 });
const frame = page.frameLocator(".att-frame").first();
await frame.locator("#go").click();
assert.equal(await frame.locator("#t").innerText(), "24:59");  // the prototype is live
assert.match(await page.locator(".att-table").innerText(), /Январь\s+42/);
assert.equal(await page.evaluate(() => { try { return document.querySelector(".att-frame").contentWindow.document.title; } catch { return "blocked"; } }), "blocked");
const third = await page.evaluate(() => document.querySelectorAll("[data-testid=turn]")[2].innerText);
assert.match(third, /uploads\/aidd-e2e-brief\.csv/);
assert.ok(await page.locator(".user-files .chip-file").count() === 1, "the attachment is shown as a chip");
if (SHOTS) {
  await page.locator("[data-testid=attachment]").first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${SHOTS}/chat-files.png` });
}

// settings pages render in the new shell
for (const tab of ["models", "skills", "accounts", "github", "profile"]) {
  await page.goto(`${BASE}/app/#/settings/${tab}`);
  await page.waitForSelector(".set-body");
}

// the phone
const phone = await (await browser.newContext({ ...devices["iPhone 13"] })).newPage();
await phone.goto(`${BASE}/app/`);
await phone.fill("input[name=email]", "admin@local");
await phone.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await phone.click("button[type=submit]");
await phone.waitForSelector("[data-testid=chat-input]");
if (SHOTS) await phone.screenshot({ path: `${SHOTS}/chat-phone-new.png` });
await phone.click(".menu-btn");
await phone.locator(".side-chat").first().click();
await phone.waitForSelector("[data-testid=turn]");
await phone.waitForTimeout(500);  // the side panel has slid away
assert.ok(!(await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)), "no sideways scroll");
if (SHOTS) await phone.screenshot({ path: `${SHOTS}/chat-phone.png` });

assert.deepEqual(errors, []);
await browser.close();
console.log("e2e-chat: OK");
