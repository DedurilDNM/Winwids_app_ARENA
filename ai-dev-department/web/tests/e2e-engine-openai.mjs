// E2E: Claude Code on an aggregator that speaks only the OpenAI protocol. The key's /v1/messages
// answers 404, so the proxy translates: Claude Code's Anthropic request -> chat completions, and the
// streamed OpenAI answer (text + a tool call split in pieces) -> Anthropic events.
//
//   python3 web/tests/fake_llm.py 9200 &   FRESH=1 ENGINE=claude_code web/tests/e2e-server.sh
//   NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node web/tests/e2e-engine-openai.mjs
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const LLM = process.env.LLM || "http://127.0.0.1:9200/v1";
const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1280, height: 860 } })).newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`${BASE}/app/`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=chat-input]");
await page.click("[data-testid=settings]");
await page.waitForSelector("[data-testid=key-summary]");
await page.getByRole("button", { name: "Ключ в котёл" }).first().click();
await page.selectOption("[data-testid=key-provider]", "custom");
await page.fill("[data-testid=key-secret]", "sk-oai-5555");
await page.fill("input[placeholder^='https://']", LLM);
await page.getByRole("button", { name: "Проверить и добавить" }).click();
await page.waitForSelector("[data-testid=key]");
await page.click("[data-testid=set-models]");
await page.click("[data-testid=auto-roles]");
await page.waitForSelector("[data-testid=auto-result] table", { timeout: 60000 });

await page.click("[data-testid=new-chat]");
await page.locator(".seg button", { hasText: "Один агент" }).click();
await page.fill("[data-testid=chat-input]", "Сделай файл");
await page.keyboard.press("Enter");
await page.waitForURL(/#\/chat\/\d+/);
await page.waitForFunction(() => /Готово через OpenAI-протокол/.test(document.querySelector("[data-testid=thread]").innerText), null, { timeout: 180000 });
const thread = await page.locator("[data-testid=thread]").innerText();
assert.match(thread, /Write/);
assert.match(thread, /openai\.txt/);
const log = await (await fetch(LLM.replace(/\/v1$/, "") + "/anthropic-log")).json();
assert.ok(log.openai.length >= 2 && log.openai.every((r) => r.stream), "Claude Code's calls came translated and streamed");
assert.deepEqual(errors, []);
await browser.close();
console.log("e2e-engine-openai: OK");
