// API client for the AI Dev Department server (same origin).
//
// Tokens: the access token lives only in memory; the refresh token is kept in
// localStorage ("remember me") or sessionStorage, and is exchanged for a new
// pair on page load and whenever a request returns 401.

const REFRESH_KEY = "aidd.refresh";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status; // 0 = network error
    this.detail = detail;
  }
}

function formatDetail(detail) {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (d && typeof d === "object") {
          const loc = (d.loc || []).filter((x) => x !== "body").join(".");
          return loc ? `${loc}: ${d.msg}` : d.msg;
        }
        return String(d);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

function safeStorage(kind) {
  try {
    const s = kind === "local" ? window.localStorage : window.sessionStorage;
    const probe = "__aidd";
    s.setItem(probe, "1");
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

let accessToken = null;
let refreshInFlight = null;
const listeners = new Set();

export const auth = {
  get loggedIn() {
    return Boolean(accessToken);
  },
  onLogout(fn) {
    listeners.add(fn);
  },
  _refreshToken() {
    return safeStorage("session")?.getItem(REFRESH_KEY) || safeStorage("local")?.getItem(REFRESH_KEY) || null;
  },
  _save(data, remember) {
    accessToken = data.access_token;
    const keep = remember ?? Boolean(safeStorage("local")?.getItem(REFRESH_KEY));
    safeStorage("local")?.removeItem(REFRESH_KEY);
    safeStorage("session")?.removeItem(REFRESH_KEY);
    safeStorage(keep ? "local" : "session")?.setItem(REFRESH_KEY, data.refresh_token);
  },
  clear() {
    accessToken = null;
    safeStorage("local")?.removeItem(REFRESH_KEY);
    safeStorage("session")?.removeItem(REFRESH_KEY);
  },
  logout() {
    this.clear();
    listeners.forEach((fn) => fn());
  },
};

async function send(method, path, body, token) {
  const headers = {};
  const form = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined && !form) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    return await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : form ? body : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (err) {
    throw new ApiError(0, "Нет связи с сервером");
  }
}

async function readError(res) {
  let detail = null;
  try {
    const data = await res.json();
    detail = formatDetail(data && typeof data === "object" && "detail" in data ? data.detail : data);
  } catch {
    detail = null;
  }
  if (res.status === 429) detail = "Слишком много попыток, подождите минуту";
  return new ApiError(res.status, detail || `Ошибка сервера (HTTP ${res.status})`);
}

/** Exchange the stored refresh token for a new pair. Concurrent callers share one request. */
export function refreshSession() {
  const rt = auth._refreshToken();
  if (!rt) return Promise.resolve(false);
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await send("POST", "/api/auth/refresh", { refresh_token: rt });
        if (!res.ok) {
          if (res.status === 401) auth.clear();
          return false;
        }
        auth._save(await res.json());
        return true;
      } catch {
        return false; // network error: keep the refresh token for a later retry
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

/** Current access token for the WebSocket auth message (refreshes if needed). */
export async function accessTokenForWs({ forceRefresh = false } = {}) {
  if (forceRefresh || !accessToken) await refreshSession();
  return accessToken;
}

export async function request(method, path, body, { authRequired = true, raw = false } = {}) {
  let res = await send(method, path, body, authRequired ? accessToken : null);
  if (authRequired && res.status === 401) {
    if (await refreshSession()) {
      res = await send(method, path, body, accessToken);
    } else {
      auth.logout();
      throw new ApiError(401, "Сессия истекла, войдите снова");
    }
  }
  if (!res.ok) throw await readError(res);
  if (raw) return res;
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function withFiles(data, files) {
  if (!files || !files.length) return data;
  const form = new FormData();
  for (const [k, v] of Object.entries(data)) if (v !== null && v !== undefined) form.append(k, v);
  for (const f of files) form.append("files", f, f.name);
  return form;
}

export const api = {
  async login(email, password, remember) {
    const data = await request("POST", "/api/auth/login", { email, password }, { authRequired: false });
    auth._save(data, remember);
    return data;
  },
  async register(email, password, inviteCode, remember) {
    const data = await request(
      "POST", "/api/auth/register",
      { email, password, invite_code: inviteCode },
      { authRequired: false },
    );
    auth._save(data, remember);
    return data;
  },
  me: () => request("GET", "/api/auth/me"),

  listAccounts: () => request("GET", "/api/accounts"),
  createAccount: (data) => request("POST", "/api/accounts", data),
  updateAccount: (id, data) => request("PATCH", `/api/accounts/${id}`, data),
  deleteAccount: (id) => request("DELETE", `/api/accounts/${id}`),
  confirmLogin: (id) => request("POST", `/api/accounts/${id}/confirm-login`),
  vncStart: (id) => request("POST", `/api/accounts/${id}/vnc/start`),
  vncStop: (id) => request("POST", `/api/accounts/${id}/vnc/stop`),
  vncInfo: (id) => request("GET", `/api/accounts/${id}/vnc`),
  probeStart: (id) => request("POST", `/api/accounts/${id}/probe`),
  probeStatus: (id) => request("GET", `/api/accounts/${id}/probe`),
  calibrateStart: (id) => request("POST", `/api/accounts/${id}/calibrate`),
  calibrateStatus: (id) => request("GET", `/api/accounts/${id}/calibrate`),

  githubStatus: () => request("GET", "/api/github"),
  githubConnect: (token) => request("PUT", "/api/github", { token }),
  githubDisconnect: () => request("DELETE", "/api/github"),
  githubRepos: () => request("GET", "/api/github/repos"),
  githubRepoCheck: (repo) => request("GET", `/api/github/repo-check?repo=${encodeURIComponent(repo)}`),

  attention: () => request("GET", "/api/attention"),

  listProjects: () => request("GET", "/api/projects"),
  getProject: (id) => request("GET", `/api/projects/${id}`),
  createProject: (data) => request("POST", "/api/projects", data),
  updateProject: (id, data) => request("PATCH", `/api/projects/${id}`, data),
  startProject: (id) => request("POST", `/api/projects/${id}/start`),
  stopProject: (id) => request("POST", `/api/projects/${id}/stop`),
  resumeProject: (id) => request("POST", `/api/projects/${id}/resume`),
  deleteProject: (id) => request("DELETE", `/api/projects/${id}`),

  // chats with the API department
  listChats: () => request("GET", "/api/chats"),
  createChat: (data, files) => request("POST", "/api/chats", withFiles(data, files)),
  getChat: (id) => request("GET", `/api/chats/${id}`),
  sendChatMessage: (id, data, files) => request("POST", `/api/chats/${id}/messages`, withFiles(data, files)),
  chatFileBlob: async (id, path) => (await request("GET", `/api/chats/${id}/file?path=${encodeURIComponent(path)}`, undefined, { raw: true })).blob(),
  chatPreview: (id, path) => request("GET", `/api/chats/${id}/preview?path=${encodeURIComponent(path)}`),
  openChatPr: (id) => request("POST", `/api/chats/${id}/pr`),
  chatLink: async (id, path) => (await request("POST", `/api/chats/${id}/link`, { path })).url,
  importSkill: (url) => request("POST", "/api/skills/import", { url }),
  stopChat: (id) => request("POST", `/api/chats/${id}/stop`),
  renameChat: (id, title) => request("PATCH", `/api/chats/${id}`, { title }),
  deleteChat: (id) => request("DELETE", `/api/chats/${id}`),
  chatEvents: (id, after) => request("GET", `/api/chats/${id}/events?after=${after || 0}`),
  chatChanges: (id, run) => request("GET", `/api/chats/${id}/changes${run ? `?run=${run}` : ""}`),
  chatDiff: async (id, path, run) => (await request("GET", `/api/chats/${id}/diff?path=${encodeURIComponent(path)}${run ? `&run=${run}` : ""}`, undefined, { raw: true })).text(),
  chatArchive: async (id) => (await request("GET", `/api/chats/${id}/archive`, undefined, { raw: true })).blob(),

  // API department: key pool, roles, skills, runs
  providers: () => request("GET", "/api/keys/providers"),
  listKeys: () => request("GET", "/api/keys"),
  addKey: (data) => request("POST", "/api/keys", data),
  updateKey: (id, data) => request("PATCH", `/api/keys/${id}`, data),
  deleteKey: (id) => request("DELETE", `/api/keys/${id}`),
  checkKey: (id) => request("POST", `/api/keys/${id}/check`),
  allModels: () => request("GET", "/api/keys/models/all"),
  providerModels: (provider) => request("GET", `/api/keys/models?provider=${encodeURIComponent(provider)}`),
  listRoles: () => request("GET", "/api/agent-roles"),
  saveRole: (role, data) => request("PUT", `/api/agent-roles/${encodeURIComponent(role)}`, data),
  resetRole: (role) => request("DELETE", `/api/agent-roles/${encodeURIComponent(role)}`),
  autoRoles: (check = true) => request("POST", "/api/agent-roles/auto", { check }),
  autoRolesStatus: () => request("GET", "/api/agent-roles/auto"),
  listSkills: () => request("GET", "/api/skills"),
  getSkill: (name) => request("GET", `/api/skills/${encodeURIComponent(name)}`),
  addSkillText: (text) => request("POST", "/api/skills", { text }),
  addSkillFile: (file) => { const f = new FormData(); f.append("file", file); return request("POST", "/api/skills", f); },
  deleteSkill: (name) => request("DELETE", `/api/skills/${encodeURIComponent(name)}`),
  listRuns: () => request("GET", "/api/runs"),
  createRun: (data) => request("POST", "/api/runs", data),
  getRun: (id) => request("GET", `/api/runs/${id}`),
  runEvents: (id, after) => request("GET", `/api/runs/${id}/events?after=${after || 0}`),
  stopRun: (id) => request("POST", `/api/runs/${id}/stop`),
  deleteRun: (id) => request("DELETE", `/api/runs/${id}`),
  runFiles: (id) => request("GET", `/api/runs/${id}/files`),
  runFile: async (id, path) => (await request("GET", `/api/runs/${id}/file?path=${encodeURIComponent(path)}`, undefined, { raw: true })).text(),
  runArchive: async (id) => (await request("GET", `/api/runs/${id}/archive`, undefined, { raw: true })).blob(),
};
