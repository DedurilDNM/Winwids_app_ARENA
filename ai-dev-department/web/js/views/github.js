// «GitHub»: repository connection for the agents (implemented in stage 3).

import { emptyState, h } from "../ui.js";

export async function render(main) {
  main.append(
    h("div", { class: "page-head" }, h("h1", {}, "GitHub")),
    emptyState("Подключение GitHub",
      "Здесь вы подключите репозиторий и проверите, что каждый агент видит его через GitHub-коннектор сервиса."));
}
