// The models every connected aggregator offers, for the model pickers: one request for
// all providers (GET /api/keys/models/all), kept for a few minutes, with a search box.

import { api } from "./api.js";
import { errorText, h } from "./ui.js";

const SHOW = 60;
let cache = null; // { at, data }

export async function allModels(force = false) {
  if (!force && cache && Date.now() - cache.at < 5 * 60 * 1000) return cache.data;
  const data = (await api.allModels()).providers || [];
  cache = { at: Date.now(), data };
  return data;
}

/** A search box and a list of models, grouped by aggregator.
 *  onPick(provider, model); `extra` items go first (e.g. «Авто»); `typed` lets the user
 *  take the name exactly as written when the aggregator does not list it (without it: one
 *  «use as written» item per aggregator). */
export function modelMenu({ onPick, extra = [], typed = null, focus = true }) {
  const search = h("input", { class: "mm-search", type: "search", placeholder: "Поиск модели", "data-testid": "model-search", autocomplete: "off" });
  const list = h("div", { class: "mm-list" });
  const box = h("div", { class: "model-menu", onclick: (e) => e.stopPropagation() }, search, list);
  let data = null;
  let failed = null;

  const item = (title, sub, onclick, testid) => h("div", { class: "it", onclick, "data-testid": testid }, h("b", {}, title), sub ? h("small", {}, sub) : null);

  function paint() {
    const q = search.value.trim().toLowerCase();
    const rows = [...extra.filter((x) => !q || x.title.toLowerCase().includes(q)).map((x) => item(x.title, x.sub, x.onclick))];
    if (failed) rows.push(h("div", { class: "it muted" }, h("small", {}, failed)));
    else if (!data) rows.push(h("div", { class: "it" }, h("small", {}, "Загружаю модели…")));
    else if (!data.length) rows.push(h("a", { class: "it", href: "#/settings/keys" }, h("small", {}, "Нет ключей: добавьте ключ в настройках")));
    else {
      let shown = 0;
      let hidden = 0;
      for (const p of data) {
        const names = p.models.filter((m) => !q || m.toLowerCase().includes(q));
        if (p.error && !q) rows.push(h("div", { class: "mm-group" }, p.name, h("small", {}, ` · ${p.error}`)));
        if (!names.length) continue;
        rows.push(h("div", { class: "mm-group" }, p.name));
        for (const m of names) {
          if (shown >= SHOW) { hidden += 1; continue; }
          shown += 1;
          rows.push(item(m, null, () => onPick(p.provider, m), "model-option"));
        }
      }
      if (hidden) rows.push(h("div", { class: "it" }, h("small", {}, `ещё ${hidden} — уточните поиск`)));
      const raw = search.value.trim();
      if (q && !data.some((p) => p.models.some((m) => m.toLowerCase() === q))) {
        // a model the aggregator does not list: take the name as written
        if (typed) rows.push(item(`Использовать «${raw}»`, typed.sub, () => typed.onclick(raw)));
        else for (const p of data) rows.push(item(`Использовать «${raw}»`, `как есть, у ${p.name}`, () => onPick(p.provider, raw), "model-typed"));
      }
    }
    list.replaceChildren(...rows);
  }

  search.addEventListener("input", paint);
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); (list.querySelector("[data-testid='model-option']") || list.querySelector("[data-testid='model-typed'], .it"))?.click(); }
  });
  paint();
  allModels().then((d) => { data = d; paint(); }).catch((e) => { failed = errorText(e); paint(); });
  if (focus) setTimeout(() => search.focus(), 0);
  return box;
}
