// E2E: Claude Code as the engine of the chats. The real `claude` CLI runs in the sandbox and talks to
// this server's model proxy; the proxy takes keys from the pool and calls a fake Anthropic-format
// aggregator (fake_llm.py on :9200) that plays Claude: a plan, a coder sub-agent writes the page, the
// lead looks at a screenshot and sends the page; a follow-up message resumes the same session.
//
//   python3 web/tests/fake_llm.py 9200 &   FRESH=1 ENGINE=claude_code web/tests/e2e-server.sh
//   NODE_PATH=$(npm root -g) ADMIN_PASSWORD=adminpass123 node web/tests/e2e-engine.mjs
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const LLM = process.env.LLM || "http://127.0.0.1:9200/v1";
const SHOTS = process.env.SHOTS || "";
const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: SHOTS ? 2 : 1 })).newPage();
const errors = [];
const logUrl = LLM.replace(/\/v1$/, "") + "/anthropic-log";
const firstCall = (await (await fetch(logUrl)).json()).requests.length;  // the fake may have served other tests
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(`${BASE}/app/`);
await page.fill("input[name=email]", "admin@local");
await page.fill("input[name=password]", process.env.ADMIN_PASSWORD);
await page.click("button[type=submit]");
await page.waitForSelector("[data-testid=chat-input]");

// a key of a «custom» aggregator, models chosen automatically
await page.click("[data-testid=settings]");
await page.waitForSelector("[data-testid=key-summary]");
await page.getByRole("button", { name: "Ключ в котёл" }).first().click();
await page.selectOption("[data-testid=key-provider]", "custom");
await page.fill("[data-testid=key-secret]", "sk-good-3333");
await page.fill("input[placeholder^='https://']", LLM);
await page.getByRole("button", { name: "Проверить и добавить" }).click();
await page.waitForSelector("[data-testid=key]");
await page.click("[data-testid=set-models]");
assert.match(await page.locator("[data-testid=engine]").innerText(), /Claude Code/);
await page.click("[data-testid=auto-roles]");
await page.waitForSelector("[data-testid=auto-result] table", { timeout: 60000 });

// a task in a new chat: Claude Code works, the chat shows it like the built-in department
await page.click("[data-testid=new-chat]");
await page.fill("[data-testid=chat-input]", "Сделай прототип экрана входа");
await page.keyboard.press("Enter");
await page.waitForURL(/#\/chat\/\d+/);
await page.waitForSelector("[data-testid=attachment] .att-frame", { timeout: 180000 });
await page.waitForFunction(() => /проверил по скриншоту/.test(document.querySelector("[data-testid=thread]").innerText), null, { timeout: 120000 });
const thread = await page.locator("[data-testid=thread]").innerText();
assert.ok(await page.locator("[data-testid=plan]").count() === 1, "the plan (TodoWrite) is shown");
assert.match(thread, /Кодер/, "the coder sub-agent is shown");
assert.match(await page.locator("[data-testid=agent]").first().innerText(), /Write|prototype\/index\.html/);
assert.match(thread, /Screenshot/);
assert.doesNotMatch(thread, /БЕЗ КАРТИНКИ/, "the screenshot reached the model as an image");
const frame = page.frameLocator(".att-frame").first();
await frame.locator("#go").click();
assert.equal(await frame.locator("#go").innerText(), "Вошли");  // the live prototype
if (SHOTS) await page.screenshot({ path: `${SHOTS}/engine-chat.png` });

// a follow-up resumes the same Claude Code session (it remembers the first turn); thinking: max
const before = (await (await fetch(logUrl)).json()).requests.length - firstCall;
await page.click("[data-testid=model-btn]");
await page.click("[data-testid=effort-seg] [data-effort=max]");
assert.match(await page.locator("[data-testid=model-btn]").innerText(), /Авто\s*·\s*max/);
await page.keyboard.press("Escape");
await page.locator("[data-testid=thread]").click({ position: { x: 5, y: 5 } });
await page.fill("[data-testid=chat-input]", "Сделай фон темнее");
await page.keyboard.press("Enter");
await page.waitForFunction(() => /Сделал темнее/.test(document.querySelector("[data-testid=thread]").innerText), null, { timeout: 180000 });
assert.match(await page.locator("[data-testid=thread]").innerText(), /помню прошлый ход/);

// the calls went through the key pool: the key has requests and tokens
await page.click("[data-testid=settings]");
await page.waitForSelector("[data-testid=key]");
const log = await (await fetch(logUrl)).json();
log.requests = log.requests.slice(firstCall);
assert.ok(log.requests.length >= 6 && log.requests.every((r) => r.key === "3333"), "every model call used the pool's key");
assert.ok(log.requests.every((r) => !r.tools.includes("WebSearch")), "no web search tool through an aggregator");
const second = log.requests.slice(before).filter((r) => r.model === "fake-model" && r.tools.length);
console.log("effort seen by the aggregator:", JSON.stringify(second.map((r) => [r.output_config, r.thinking]).slice(0, 2)));
assert.ok(second.some((r) => JSON.stringify([r.output_config, r.thinking]).includes("max")), "thinking: max reached the model");
assert.deepEqual(errors, []);
await browser.close();
console.log("e2e-engine: OK");
