// «Аккаунты»: the user's service accounts (agents), their roles and the
// embedded browser used to log into each of them.

import { api } from "../api.js";
import { openBrowser } from "../browser.js";
import { confirmDialog, emptyState, errorText, fmtDate, h, icon, modal, toast } from "../ui.js";

export const ROLES = ["Orchestrator", "Architect", "Coder", "Reviewer", "Tester", "Integrator", "ImagePrompter"];
export const ROLE_LABELS = {
  Orchestrator: "Оркестратор", Architect: "Архитектор", Coder: "Разработчик",
  Reviewer: "Ревьюер", Tester: "Тестировщик", Integrator: "Интегратор",
  ImagePrompter: "Промптер изображений",
};
export const MAX_ACCOUNTS = 10;
export const STATUS = {
  pending_login: ["Ждёт входа", "warn"],
  active: ["Активен", "ok"],
  relogin_needed: ["Нужен повторный вход", "danger"],
  disabled: ["Отключён", "idle"],
};

export function statusBadge(status) {
  const [label, cls] = STATUS[status] || [status, "idle"];
  return h("span", { class: `badge ${cls}`, "data-testid": "status" }, label);
}

function parseRepos(text) {
  return text.split(/[\s,;]+/).map((r) => r.trim()).filter(Boolean);
}

// Suggestions for the model field (the user can type any name the service shows).
const MODELS = ["claude-sonnet-5-5-high", "gemini-4-argon-high", "gpt-6-luna-max", "qwen3.8-max",
  "deepseek-v4.1-flash-max"];

function roleSelect(value) {
  return h("select", { name: "role" },
    ROLES.map((r) => h("option", { value: r, selected: r === value }, ROLE_LABELS[r])));
}

/** Create/edit form. Resolves with the payload or undefined if cancelled. */
function accountForm(title, initial = {}, submitLabel = "Сохранить") {
  return modal(title, (close) => {
    const name = h("input", { name: "name", required: true, maxLength: 120, value: initial.name || "", placeholder: "Например: coder-1" });
    const role = roleSelect(initial.default_role || "Coder");
    const repos = h("input", { name: "repos", value: (initial.github_repos || []).join(", "), placeholder: "owner/repo (необязательно)" });
    const note = h("input", { name: "note", maxLength: 255, value: initial.service_note || "", placeholder: "Заметка (необязательно)" });
    const model = h("input", { name: "model", maxLength: 120, list: "aidd-models", autocomplete: "off",
      value: initial.model || "", placeholder: "Например: claude-sonnet-5-5-high (пусто — не проверять)" });
    const models = h("datalist", { id: "aidd-models" },
      MODELS.map((m) => h("option", { value: m })));
    const email = h("input", { name: "login_email", type: "email", maxLength: 255, autocomplete: "off",
      value: initial.login_email || "", placeholder: "Если пусто — почта из заметки" });
    const password = h("input", { name: "login_password", type: "password", maxLength: 256, autocomplete: "new-password",
      placeholder: initial.has_login_password ? "Сохранён — оставьте пустым, чтобы не менять" : "Пароль от аккаунта сервиса" });
    const forget = h("input", { type: "checkbox", name: "forget_password" });
    const error = h("div", { class: "form-error", role: "alert" });
    const form = h("form", {},
      h("label", { class: "field" }, "Название", name),
      h("label", { class: "field" }, "Роль", role),
      h("label", { class: "field" }, "GitHub-репозитории", repos),
      h("label", { class: "field" }, "Заметка", note),
      h("label", { class: "field" }, "Модель в чате сервиса", model), models,
      h("fieldset", { class: "fieldset" },
        h("legend", {}, "Автовход (если сервис выкинет из аккаунта)"),
        h("label", { class: "field" }, "Почта для входа", email),
        h("label", { class: "field" }, "Пароль", password),
        initial.has_login_password
          ? h("label", { class: "checkbox" }, forget, "Удалить сохранённый пароль")
          : null,
        h("p", { class: "small muted" }, "Пароль хранится на сервере в зашифрованном виде и никогда не показывается.")),
      error,
      h("div", { class: "modal-actions" },
        h("button", { type: "button", onclick: () => close(undefined) }, "Отмена"),
        h("button", { type: "submit", class: "primary" }, submitLabel)));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const list = parseRepos(repos.value);
      if (!name.value.trim()) return (error.textContent = "Укажите название");
      const bad = list.filter((r) => !/^[\w.-]+\/[\w.-]+$/.test(r));
      if (bad.length) return (error.textContent = `Формат репозитория owner/name: ${bad.join(", ")}`);
      const data = { name: name.value.trim(), default_role: role.value, github_repos: list, service_note: note.value.trim(),
        login_email: email.value.trim(), model: model.value.trim() };
      if (password.value) data.login_password = password.value;
      else if (forget.checked) data.login_password = "";
      close(data);
    });
    return form;
  });
}

const STEP_LABELS = {
  starting: "запуск",
  "opening a new chat": "открываю новый чат",
  "sending the test message": "отправляю тестовое сообщение",
  "waiting for the answer": "жду ответ",
  "finding the answer on the page": "ищу ответ на странице",
  "checking that the answer is read correctly": "проверяю, что ответ читается",
  "waiting for a human": "нужна ваша помощь в браузере аккаунта (вход или капча) — см. баннер сверху",
};

/** One line on the account card: is the account calibrated for the current chat mode? */
function calibrationLine(c) {
  if (!c) return h("p", { class: "small muted", "data-testid": "calibration" }, "Калибровка: не выполнена");
  const when = c.checked_at ? fmtDate(c.checked_at) : "";
  return h("p", { class: `small ${c.ok ? "muted" : "danger-text"}`, "data-testid": "calibration" },
    c.ok ? `Калибровка: ✓ ${when}${c.modality && c.modality !== "default" ? ` (${c.modality})` : ""}`
      : "Калибровка: не удалась — запустите ещё раз");
}

function calibrationDetails(res) {
  if (!res) return h("p", { class: "small" }, "Нет результата");
  const row = (ok, text) => h("li", {}, `${ok ? "✓" : "✗"} ${text}`);
  const checks = res.checks || {};
  return h("ul", { class: "checklist" },
    row(res.ok, res.ok
      ? (res.reader === "selector" ? "Ответ читается (найден устойчивый признак сообщения)"
        : "Ответ читается (по соседству с вашим сообщением)")
      : `Ответ не прочитан${res.reason ? `: ${res.reason}` : ""}`),
    row(checks.code_block, "Блоки кода сохраняются"),
    row(res.busy_found, res.busy_found ? "Найден индикатор «сервис пишет»"
      : "Индикатор «пишет» не найден — сервер ждёт, пока текст перестанет меняться"),
    res.reply_seconds != null ? h("li", {}, `⏱ ответ начался через ${res.reply_seconds} с`) : null);
}

export async function render(main) {
  let accounts = [];
  const list = h("div", { class: "grid", "data-testid": "accounts" });
  const counter = h("span", { class: "muted small" });
  const refreshBtn = h("button", { onclick: () => load(), title: "Обновить" }, icon("refresh"), h("span", { class: "hide-sm" }, "Обновить"));
  const addBtn = h("button", { class: "primary", onclick: addAccount }, icon("plus"), "Добавить аккаунт");

  main.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Аккаунты"), counter),
      h("div", { class: "actions m-0" }, refreshBtn, addBtn)),
    list);

  function card(acc) {
    const role = roleSelect(acc.default_role);
    role.setAttribute("aria-label", `Роль аккаунта ${acc.name}`);
    role.addEventListener("change", async () => {
      role.disabled = true;
      try {
        await api.updateAccount(acc.id, { default_role: role.value });
        acc.default_role = role.value;
        toast(`Роль «${acc.name}»: ${ROLE_LABELS[role.value]}`);
      } catch (err) {
        role.value = acc.default_role;
        toast(`Не удалось сменить роль: ${errorText(err)}`, { error: true });
      } finally {
        role.disabled = false;
      }
    });

    const needsLogin = acc.status === "pending_login" || acc.status === "relogin_needed";
    const disabled = acc.status === "disabled";
    const primary = needsLogin
      ? h("button", { class: "primary", onclick: () => browse(acc, "login") }, acc.status === "relogin_needed" ? "Перелогиниться" : "Войти")
      : h("button", { onclick: () => browse(acc, "view"), disabled }, "Открыть браузер");

    return h("article", { class: `card${acc.status === "relogin_needed" ? " card-alert" : ""}`, dataset: { accountId: acc.id } },
      h("div", { class: "card-head" }, h("div", { class: "card-title" }, acc.name), statusBadge(acc.status)),
      h("label", { class: "inline-field" }, h("span", { class: "muted small" }, "Роль"), role),
      h("div", { class: "meta" },
        h("span", {}, acc.github_repos?.length ? `GitHub: ${acc.github_repos.join(", ")}` : "GitHub не задан"),
        h("span", {}, `Проверка: ${fmtDate(acc.last_health_check)}`)),
      acc.service_note ? h("p", { class: "small muted mt-8" }, acc.service_note) : null,
      acc.model ? h("p", { class: "small muted", "data-testid": "model" }, `Модель: ${acc.model}`) : null,
      acc.has_login_password
        ? h("p", { class: "small muted", "data-testid": "auto-login" }, "Автовход: включён")
        : null,
      calibrationLine(acc.calibration),
      h("div", { class: "actions" },
        primary,
        h("button", { onclick: () => edit(acc) }, "Изменить"),
        h("button", { onclick: () => calibrateAccount(acc), disabled, title: "Сервер сам изучит, как этот сайт показывает ответы (1 тестовое сообщение)" }, "Калибровка"),
        h("button", { onclick: () => diagnose(acc), disabled, title: "Проверить, что адаптер умеет отправлять сообщения и читать ответы" }, "Диагностика"),
        h("button", { onclick: () => toggle(acc) }, disabled ? "Включить" : "Отключить"),
        h("button", { class: "danger", onclick: () => remove(acc) }, "Удалить")));
  }

  async function load() {
    refreshBtn.disabled = true;
    try {
      accounts = await api.listAccounts();
      counter.textContent = `${accounts.length} из ${MAX_ACCOUNTS}`;
      addBtn.disabled = accounts.length >= MAX_ACCOUNTS;
      addBtn.title = addBtn.disabled ? `Лимит: ${MAX_ACCOUNTS} аккаунтов` : "";
      list.replaceChildren(...(accounts.length
        ? accounts.map(card)
        : [emptyState("Пока нет аккаунтов",
            "Добавьте аккаунт сервиса и войдите в него во встроенном браузере. Каждому аккаунту можно дать свою роль.",
            h("button", { class: "primary", onclick: addAccount }, icon("plus"), "Добавить аккаунт"))]));
    } catch (err) {
      toast(`Не удалось загрузить аккаунты: ${errorText(err)}`, { error: true });
    } finally {
      refreshBtn.disabled = false;
    }
  }

  async function addAccount() {
    const data = await accountForm("Новый аккаунт", {}, "Создать и войти");
    if (!data) return;
    let acc;
    try {
      acc = await api.createAccount(data);
    } catch (err) {
      return toast(`Не удалось создать аккаунт: ${errorText(err)}`, { error: true });
    }
    await load();
    await browse(acc, "login");
  }

  async function browse(acc, mode) {
    await openBrowser(acc, { mode });
    await load();
  }

  async function edit(acc) {
    const data = await accountForm(`Изменить «${acc.name}»`, acc);
    if (!data) return;
    try {
      await api.updateAccount(acc.id, data);
      toast("Сохранено");
      await load();
    } catch (err) {
      toast(`Не удалось сохранить: ${errorText(err)}`, { error: true });
    }
  }

  async function calibrateAccount(acc) {
    await modal(`Калибровка: ${acc.name}`, (close) => {
      let stopped = false;
      const status = h("p", { class: "small muted" },
        "Сервер откроет новый чат, отправит одно тестовое сообщение и сам найдёт, где на странице ответ "
        + "и как видно, что сервис ещё пишет. Текст страницы никуда не передаётся. Обычно 1–3 минуты.");
      const result = h("div", { "data-testid": "calibration-result" });
      const runBtn = h("button", { class: "primary" }, "Запустить");
      const closeBtn = h("button", { onclick: () => { stopped = true; close(); load(); } }, "Закрыть");
      const show = (res) => {
        runBtn.disabled = false;
        runBtn.textContent = "Запустить ещё раз";
        status.textContent = res && res.ok ? "Готово: настройки сохранены для этого аккаунта." : "Не получилось.";
        result.replaceChildren(calibrationDetails(res));
      };
      async function poll() {
        while (!stopped) {
          await new Promise((r) => setTimeout(r, 2000));
          if (stopped) return;
          try {
            const st = await api.calibrateStatus(acc.id);
            if (!st.running) return show(st.result);
            status.textContent = `Идёт калибровка: ${STEP_LABELS[st.step] || st.step || "…"}`;
          } catch (err) {
            return show({ ok: false, reason: errorText(err) });
          }
        }
      }
      runBtn.addEventListener("click", async () => {
        runBtn.disabled = true;
        result.replaceChildren();
        status.textContent = "Идёт калибровка…";
        try {
          const st = await api.calibrateStart(acc.id);
          if (!st.running) return show(st.result);
          poll();
        } catch (err) {
          runBtn.disabled = false;
          status.textContent = `Не удалось запустить: ${errorText(err)}`;
        }
      });
      return h("div", {}, status, result, h("div", { class: "modal-actions" }, closeBtn, runBtn));
    });
  }

  async function diagnose(acc) {
    await modal(`Диагностика: ${acc.name}`, (close) => {
      let stopped = false;
      const status = h("p", { class: "small muted" },
        "Откроет новый чат в браузере этого аккаунта и отправит 3 коротких тестовых сообщения. "
        + "В отчёт попадает только устройство страницы — без адресов и без текста страницы.");
      const report = h("pre", { class: "report", hidden: true, "data-testid": "probe-report" });
      const copyBtn = h("button", { hidden: true }, "Копировать");
      const runBtn = h("button", { class: "primary" }, "Запустить");
      const closeBtn = h("button", { onclick: () => { stopped = true; close(); } }, "Закрыть");

      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(report.textContent);
          toast("Отчёт скопирован");
        } catch {
          const range = document.createRange();
          range.selectNodeContents(report);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          toast("Выделено — скопируйте вручную");
        }
      });

      async function poll() {
        while (!stopped) {
          await new Promise((r) => setTimeout(r, 2000));
          if (stopped) return;
          try {
            const st = await api.probeStatus(acc.id);
            if (!st.running) return show(st.report || "Нет отчёта");
          } catch (err) {
            return show(`Ошибка: ${errorText(err)}`);
          }
        }
      }
      function show(text) {
        runBtn.disabled = false;
        runBtn.textContent = "Запустить ещё раз";
        status.textContent = "Готово. Скопируйте отчёт и отправьте разработчику.";
        report.textContent = text;
        report.hidden = false;
        copyBtn.hidden = false;
      }
      runBtn.addEventListener("click", async () => {
        runBtn.disabled = true;
        report.hidden = true;
        copyBtn.hidden = true;
        status.textContent = "Идёт проверка… обычно 1–4 минуты. Окно можно не закрывать.";
        try {
          const st = await api.probeStart(acc.id);
          if (!st.running) return show(st.report || "");
          poll();
        } catch (err) {
          runBtn.disabled = false;
          status.textContent = `Не удалось запустить: ${errorText(err)}`;
        }
      });
      return h("div", {}, status, report, h("div", { class: "modal-actions" }, copyBtn, closeBtn, runBtn));
    });
  }

  async function toggle(acc) {
    // "active" can only be set by confirm-login, so re-enabling goes through pending_login.
    const next = acc.status === "disabled" ? "pending_login" : "disabled";
    try {
      await api.updateAccount(acc.id, { status: next });
      await load();
    } catch (err) {
      toast(`Не удалось изменить статус: ${errorText(err)}`, { error: true });
    }
  }

  async function remove(acc) {
    const ok = await confirmDialog("Удалить аккаунт?",
      `«${acc.name}»: сохранённая сессия и браузер на сервере будут удалены, аккаунт отвяжется от проектов.`, "Удалить");
    if (!ok) return;
    try {
      await api.deleteAccount(acc.id);
      toast("Аккаунт удалён");
      await load();
    } catch (err) {
      toast(`Не удалось удалить: ${errorText(err)}`, { error: true });
    }
  }

  await load();
}
