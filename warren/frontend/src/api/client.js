// Single place that talks to the Flask API. Components never call fetch directly.
const TOKEN_KEY = "warren_token";

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => (onUnauthorized = fn);

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && token) onUnauthorized();
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

const qs = (params) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  return s ? `?${s}` : "";
};

export const api = {
  authConfig: () => request("GET", "/auth/config"),
  me: () => request("GET", "/auth/me"),
  demoLogin: () => request("POST", "/auth/demo"),
  tokenLogin: (token) => request("POST", "/auth/token-login", { token }),
  health: () => request("GET", "/health"),
  streamTicket: () => request("POST", "/stream/ticket"),

  repos: () => request("GET", "/repos"),
  refreshRepos: () => request("POST", "/repos/refresh"),
  enableRepo: (id, enabled) => request("POST", `/repos/${id}/enable`, { enabled }),
  saveSettings: (id, settings) => request("PATCH", `/repos/${id}/settings`, settings),
  syncRepo: (id) => request("POST", `/repos/${id}/sync`),
  learnings: (id) => request("GET", `/repos/${id}/learnings`),
  deleteLearning: (repoId, id) => request("DELETE", `/repos/${repoId}/learnings/${id}`),

  board: (params) => request("GET", `/prs${qs(params)}`),
  pr: (id) => request("GET", `/prs/${id}`),
  review: (id) => request("POST", `/prs/${id}/review`),
  fixPr: (id, finding_ids) => request("POST", `/prs/${id}/fix`, { finding_ids }),
  fixFinding: (id) => request("POST", `/findings/${id}/fix`),
  dismissFinding: (id) => request("POST", `/findings/${id}/dismiss`),
  chat: (id, message) => request("POST", `/prs/${id}/chat`, { message }),
  assign: (id, logins) => request("POST", `/prs/${id}/reviewers`, { logins }),
  job: (id) => request("GET", `/jobs/${id}`),
  cancelJob: (id) => request("POST", `/jobs/${id}/cancel`),

  incidents: () => request("GET", "/incidents"),
  incident: (id) => request("GET", `/incidents/${id}`),
  approveIncident: (id, approved, note = "") => request("POST", `/incidents/${id}/approve`, { approved, note }),
  retryIncident: (id, note = "") => request("POST", `/incidents/${id}/retry`, { note }),
  dismissIncident: (id) => request("POST", `/incidents/${id}/dismiss`),

  securityOverview: (repo_id) => request("GET", `/security/overview${qs({ repo_id })}`),
  securityScan: (repo_id) => request("POST", "/security/scan", { repo_id }),
  securityFixPr: (id) => request("POST", `/security/findings/${id}/fix-pr`),
};

// Poll a background job until it finishes; `onStep` receives the live progress text.
export async function waitForJob(jobId, onStep = () => {}) {
  if (!jobId) return { status: "done" };
  for (;;) {
    const { job } = await api.job(jobId);
    onStep(job.step || job.status);
    if (job.status === "done") return job;
    if (job.status === "failed") throw new Error(job.error || "Job failed");
    await new Promise((r) => setTimeout(r, 1500));
  }
}
