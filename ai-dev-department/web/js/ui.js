// Tiny DOM helpers. Everything user-provided goes through textContent, never
// innerHTML, so server data cannot inject markup.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8m13 10v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
  github: "M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22",
  folder: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2m8-10a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
  plus: "M12 5v14M5 12h14",
  refresh: "M23 4v6h-6M1 20v-6h6m14.49-5A9 9 0 0 0 5.64 5.64L1 10m22 4-4.64 4.36A9 9 0 0 1 3.51 15",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6m4-3h6v6M10 14 21 3",
  close: "M18 6 6 18M6 6l12 12",
};

export function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || "");
  svg.append(path);
  return svg;
}

export function toast(message, { error = false, timeout = 4000 } = {}) {
  const box = document.getElementById("toasts");
  const el = h("div", { class: `toast${error ? " error" : ""}` }, message);
  box.append(el);
  setTimeout(() => el.remove(), timeout);
}

export function errorText(err) {
  return err?.detail || err?.message || "Неизвестная ошибка";
}

/** Modal dialog. `body(close)` returns content; resolves with the value passed to close(). */
export function modal(title, body) {
  return new Promise((resolve) => {
    const backdrop = h("div", { class: "modal-backdrop" });
    const close = (value) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (e) => e.key === "Escape" && close(undefined);
    const dialog = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("h2", {}, title), body(close));
    backdrop.append(dialog);
    backdrop.addEventListener("click", (e) => e.target === backdrop && close(undefined));
    document.addEventListener("keydown", onKey);
    document.body.append(backdrop);
    dialog.querySelector("input, select, textarea, button")?.focus();
  });
}

export function confirmDialog(title, text, okLabel = "Да") {
  return modal(title, (close) => h("div", {},
    h("p", { class: "muted" }, text),
    h("div", { class: "modal-actions" },
      h("button", { onclick: () => close(false) }, "Отмена"),
      h("button", { class: "primary", onclick: () => close(true) }, okLabel))));
}

export function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(String(value).endsWith("Z") ? value : `${value}Z`);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function emptyState(title, text, ...actions) {
  return h("div", { class: "empty" }, h("h2", {}, title), h("p", {}, text),
    actions.length ? h("div", { class: "actions" }, actions) : null);
}

/** Minimal, safe Markdown → DOM (headings, lists, code blocks, `code`, **bold**,
 *  http(s) links). Built with DOM nodes only: no HTML from the text is parsed. */
export function markdown(text) {
  const root = h("div", { class: "md" });
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  let list = null;
  let para = [];
  const flushPara = () => {
    if (para.length) root.append(h("p", {}, inline(para.join(" "))));
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fence) {
      flushPara(); list = null;
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
      root.append(h("pre", {}, h("code", {}, body.join("\n"))));
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    const item = line.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/);
    if (heading) {
      flushPara(); list = null;
      root.append(h(`h${Math.min(6, heading[1].length + 2)}`, {}, inline(heading[2])));
    } else if (item) {
      flushPara();
      const tag = item[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) root.append((list = h(tag, {})));
      list.append(h("li", {}, inline(item[2])));
    } else if (!line.trim()) {
      flushPara(); list = null;
    } else {
      list = null;
      para.push(line.trim());
    }
  }
  flushPara();
  return root;
}

function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s)]+)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", {}, m[1].slice(1, -1)));
    else if (m[2]) out.push(h("strong", {}, m[2].slice(2, -2)));
    else out.push(h("a", { href: m[4] || m[5], target: "_blank", rel: "noopener noreferrer" }, m[3] || m[5]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
