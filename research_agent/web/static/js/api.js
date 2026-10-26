// The server's API, one function per endpoint, and the live event stream.

export class Problem extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data && data.detail;
    if (d && d.code) throw new Problem(res.status, d.code, d.message);
    if (Array.isArray(d)) throw new Problem(res.status, "invalid", d.map((x) => x.msg).join("; "));
    throw new Problem(res.status, "http", `${res.status} ${res.statusText}`);
  }
  return data;
}

const run = (id) => `/api/runs/${encodeURIComponent(id)}`;

export const api = {
  runs: () => request("GET", "/api/runs"),
  newRun: (body) => request("POST", "/api/runs", body),
  run: (id) => request("GET", run(id)),
  closeRun: (id) => request("DELETE", run(id)),
  message: (id, text) => request("POST", `${run(id)}/message`, { text }),
  approve: (id, sections) => request("POST", `${run(id)}/approve`, { sections }),
  go: (id) => request("POST", `${run(id)}/go`),
  stop: (id) => request("POST", `${run(id)}/stop`),
  pause: (id) => request("POST", `${run(id)}/pause`),
  resume: (id) => request("POST", `${run(id)}/resume`),
  setBreak: (id, step, when, on) => request("POST", `${run(id)}/break`, { step, when, on }),
  stepAction: (id, step, action, why) => request("POST", `${run(id)}/step`, { step, action, why: why || "" }),
  execute: (id, body) => request("POST", `${run(id)}/execute`, body),
  state: (id) => request("GET", `${run(id)}/state`),
  history: (id) => request("GET", `${run(id)}/history`),
  historyEntry: (id, n) => request("GET", `${run(id)}/history/${n}`),
  graph: (id, view) => request("GET", `${run(id)}/graph${view ? `?view=${view}` : ""}`),
  sessions: (id) => request("GET", `${run(id)}/sessions`),
  session: (id, sid) => request("GET", `${run(id)}/sessions/${encodeURIComponent(sid)}`),
  files: (id) => request("GET", `${run(id)}/files`),
  file: (id, path) => request("GET", `${run(id)}/file?path=${encodeURIComponent(path)}`),
  jobs: (id) => request("GET", `${run(id)}/jobs`),
  providers: (refresh) => request("GET", `/api/providers${refresh ? "?refresh=true" : ""}`),
  nodeInfo: (id, node) => request("GET", `${run(id)}/nodes/${encodeURIComponent(node)}`),
  skills: (id) => request("GET", `${run(id)}/skills`),
  setSkills: (id, node, body) => request("PUT", `${run(id)}/nodes/${encodeURIComponent(node)}/skills`, body),
  research: (id) => request("GET", `${run(id)}/research`),
  setResearch: (id, sources) => request("PUT", `${run(id)}/research`, { sources }),
  models: (id) => request("GET", `${run(id)}/models`),
  setModel: (id, body) => request("PUT", `${run(id)}/models`, body),
  live: (id, node) => request("GET", `${run(id)}/live?node=${encodeURIComponent(node || "")}`),
  activity: (id, node) => request("GET", `${run(id)}/activity?node=${encodeURIComponent(node || "")}`),
  jobLog: (id, job) => request("GET", `${run(id)}/jobs/${encodeURIComponent(job)}/log`),
};

export const EVENT_TYPES = ["status", "chat", "call", "step", "note", "state", "error"];

// Subscribe to a run's events from `afterSeq` on. EventSource reconnects by
// itself and sends Last-Event-ID, so nothing is lost or repeated. Returns a
// function that closes the stream.
export function subscribe(id, afterSeq, handlers) {
  const source = new EventSource(`${run(id)}/events?after_seq=${afterSeq || 0}`);
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (e) => {
      if (handlers[type]) handlers[type](JSON.parse(e.data));
    });
  }
  source.onopen = () => handlers.open && handlers.open();
  source.onerror = () => handlers.lost && handlers.lost();
  return () => source.close();
}
