// «GitHub»: connect the user's token. Agents never touch GitHub: the server
// commits the files they return to the project branch and opens a PR.

import { api } from "../api.js";
import { confirmDialog, errorText, h, toast } from "../ui.js";

const TOKEN_URL = "https://github.com/settings/personal-access-tokens/new";

export async function render(main) {
  const body = h("div", { class: "stack" });
  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "GitHub")),
    h("p", { class: "muted" },
      "Агенты не работают с GitHub напрямую: они присылают файлы в ответе, а сервер сам коммитит их в ветку проекта ",
      h("code", {}, "ai/project-<id>"), " и в конце открывает Pull Request."),
    body);

  async function load() {
    let status;
    try {
      status = await api.githubStatus();
    } catch (err) {
      toast(`Не удалось загрузить: ${errorText(err)}`, { error: true });
      return;
    }
    body.replaceChildren(status.connected ? connected(status) : connectForm(), repoChecker(status.connected));
  }

  function connectForm() {
    const token = h("input", { type: "password", name: "token", autocomplete: "off", placeholder: "github_pat_…", required: true });
    const error = h("div", { class: "form-error", role: "alert" });
    const submit = h("button", { type: "submit", class: "primary" }, "Подключить");
    const form = h("form", { class: "card" },
      h("h2", {}, "Подключить токен"),
      h("ol", { class: "steps" },
        h("li", {}, "Откройте ", h("a", { href: TOKEN_URL, target: "_blank", rel: "noopener noreferrer" }, "создание fine-grained токена"), "."),
        h("li", {}, "Repository access → ", h("b", {}, "Only select repositories"), " → выберите рабочий репозиторий."),
        h("li", {}, "Permissions → Repository: ", h("b", {}, "Contents"), ", ", h("b", {}, "Pull requests"), " и ",
          h("b", {}, "Workflows"), " — Read and write; ", h("b", {}, "Actions"), " — Read-only (для проверки сборки, CI)."),
        h("li", {}, "Скопируйте токен и вставьте сюда. Он хранится на сервере в зашифрованном виде.")),
      h("label", { class: "field" }, "Токен", token),
      error,
      h("div", { class: "actions" }, submit));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      error.textContent = "";
      submit.disabled = true;
      try {
        const res = await api.githubConnect(token.value.trim());
        token.value = "";
        toast(`GitHub подключён: ${res.login}`);
        await load();
      } catch (err) {
        error.textContent = errorText(err);
      } finally {
        submit.disabled = false;
      }
    });
    return form;
  }

  function connected(status) {
    return h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("div", { class: "card-title" }, "Подключено"),
        h("span", { class: "badge ok", "data-testid": "gh-status" }, status.login || "GitHub")),
      h("dl", { class: "kv" },
        h("dt", {}, "Аккаунт"), h("dd", {}, status.login || "—"),
        h("dt", {}, "Токен"), h("dd", {}, status.token_hint || "—")),
      h("div", { class: "actions" },
        h("button", {
          class: "danger",
          onclick: async () => {
            if (!(await confirmDialog("Отключить GitHub?", "Запущенные проекты не смогут коммитить.", "Отключить"))) return;
            try {
              await api.githubDisconnect();
              toast("GitHub отключён");
              await load();
            } catch (err) {
              toast(errorText(err), { error: true });
            }
          },
        }, "Отключить")));
  }

  function repoChecker(isConnected) {
    const repo = h("input", { name: "repo", placeholder: "owner/repo или ссылка на GitHub", required: true });
    const result = h("div", { class: "check-result", "data-testid": "repo-check" });
    const btn = h("button", { type: "submit", disabled: !isConnected }, "Проверить");
    const form = h("form", { class: "card" },
      h("h2", {}, "Проверить репозиторий"),
      h("p", { class: "muted small" }, "Убедитесь, что сервер сможет коммитить в репозиторий проекта."),
      h("div", { class: "row" }, repo, btn),
      result);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      btn.disabled = true;
      result.replaceChildren(h("span", { class: "muted" }, "Проверяю…"));
      try {
        const r = await api.githubRepoCheck(repo.value.trim());
        result.replaceChildren(...[
          h("span", { class: `badge ${r.ok ? "ok" : "danger"}` }, r.ok ? "Готово к работе" : "Есть проблема"),
          h("p", { class: "small mt-8" }, translate(r.message)),
          r.default_branch ? h("p", { class: "small muted" }, `Основная ветка: ${r.default_branch}`) : null,
          r.ok ? h("p", { class: "small", "data-testid": "ci-access" }, r.actions
            ? "✓ Проверка сборки (GitHub Actions) доступна"
            : "⚠ Для проверки сборки добавьте токену право Actions: Read-only (и Workflows: Read and write)") : null,
        ].filter(Boolean));
      } catch (err) {
        result.replaceChildren(h("span", { class: "badge danger" }, "Ошибка"), h("p", { class: "small mt-8" }, errorText(err)));
      } finally {
        btn.disabled = false;
      }
    });
    return form;
  }

  await load();
}

function translate(message) {
  const map = [
    [/^OK for commits/, "Сервер может коммитить в этот репозиторий."],
    [/^OK/, "Сервер может коммитить в этот репозиторий."],
    [/^Read-only/, "Доступ только на чтение: дайте токену Contents и Pull requests — Read and write."],
    [/empty/, "Репозиторий пустой: создайте в нём README (любой первый коммит)."],
    [/not found|no access/i, "Репозиторий не найден или токен не имеет к нему доступа."],
    [/rejected the token/, "GitHub отклонил токен (истёк или отозван). Подключите новый."],
  ];
  return (map.find(([re]) => re.test(message)) || [null, message])[1];
}
