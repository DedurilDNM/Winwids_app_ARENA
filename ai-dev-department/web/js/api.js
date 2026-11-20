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
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    return await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
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

export async function request(method, path, body, { authRequired = true } = {}) {
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
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
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

  githubStatus: () => request("GET", "/api/github"),
  githubConnect: (token) => request("PUT", "/api/github", { token }),
  githubDisconnect: () => request("DELETE", "/api/github"),
  githubRepoCheck: (repo) => request("GET", `/api/github/repo-check?repo=${encodeURIComponent(repo)}`),

  listProjects: () => request("GET", "/api/projects"),
  getProject: (id) => request("GET", `/api/projects/${id}`),
  createProject: (data) => request("POST", "/api/projects", data),
  updateProject: (id, data) => request("PATCH", `/api/projects/${id}`, data),
  startProject: (id) => request("POST", `/api/projects/${id}/start`),
  stopProject: (id) => request("POST", `/api/projects/${id}/stop`),
  deleteProject: (id) => request("DELETE", `/api/projects/${id}`),
};
