// The Files and History tabs, and the right column: run facts, model calls
// (live, and every session), jobs.

import { api } from "./api.js";
import { el, clear, pill, when, size } from "./dom.js";

// ---- Files ---------------------------------------------------------------------------

export class FilesPanel {
  constructor(list, view, onError) {
    this.list = list;
    this.view = view;
    this.onError = onError;
    this.runId = null;
    this.selected = null;
  }

  async load(runId) {
    this.runId = runId;
    try {
      const { files } = await api.files(runId);
      const groups = {};
      for (const f of files) {
        const parts = f.path.split("/");
        const group = parts.length === 1 ? "run folder" : parts[0] === "state" && parts[1] === "history" ? "state/history" : parts[0];
        (groups[group] ||= []).push(f);
      }
      clear(this.list);
      for (const [top, items] of Object.entries(groups)) {
        const open = ["artifacts", "reviews", "state", "work"].includes(top) || (top !== "state/history" && items.length < 12);
        this.list.append(el("details", { class: "file-group", open },
          el("summary", { text: `${top} (${items.length})` }),
          items.map((f) => el("a", {
            href: "#", class: `file ${f.path === this.selected ? "selected" : ""} ${f.readable ? "" : "dim"}`,
            title: `${f.path} · ${size(f.size)} · ${when(f.modified)}`,
            onclick: (e) => { e.preventDefault(); this.show(f.path); },
          }, el("span", { text: top === "run folder" ? f.path : f.path.slice(top.length + 1) }),
            el("small", { text: size(f.size) })))));
      }
      if (!files.length) this.list.append(el("div", { class: "empty", text: "No files yet." }));
    } catch (err) { this.onError(err); }
  }

  async show(path) {
    this.selected = path;
    this.list.querySelectorAll(".file").forEach((a) => a.classList.toggle("selected", a.title.startsWith(path + " ")));
    try {
      const f = await api.file(this.runId, path);
      clear(this.view).append(el("div", { class: "file-head mono", text: `${path} · ${size(f.size)}` }),
        f.text == null ? el("div", { class: "empty", text: f.skipped })
          : el("pre", { class: "code" }, f.text.split("\n").map((line, i) =>
            el("div", { class: "code-line" }, el("span", { class: "gutter", text: String(i + 1) }), line))));
    } catch (err) { this.onError(err); }
  }
}

// ---- History ---------------------------------------------------------------------------

export class HistoryPanel {
  constructor(list, view, onError) {
    this.list = list;
    this.view = view;
    this.onError = onError;
  }

  async load(runId) {
    this.runId = runId;
    try {
      const entries = await api.history(runId);
      clear(this.list);
      for (const h of entries.slice().reverse()) {
        this.list.append(el("a", { href: "#", class: "hist", onclick: (e) => { e.preventDefault(); this.show(h.n, e.currentTarget); } },
          el("span", { class: "id", text: `#${h.n}` }), pill(h.by || "?", h.by === "user" ? "human" : ""),
          el("span", { class: "grow small", text: (h.changed || []).join(", ") || "-" }),
          el("small", { class: "dim", text: when(h.at) })));
      }
      if (!entries.length) this.list.append(el("div", { class: "empty", text: "No changes yet." }));
    } catch (err) { this.onError(err); }
  }

  async show(n, link) {
    this.list.querySelectorAll(".hist").forEach((a) => a.classList.toggle("selected", a === link));
    try {
      const h = await api.historyEntry(this.runId, n);
      clear(this.view).append(el("div", { class: "file-head" },
        `#${n} by ${h.by || "?"} at ${when(h.at)} — changed: ${(h.changed || []).join(", ") || "-"}`),
      el("pre", { class: "pre small", text: JSON.stringify(h.ops, null, 2) }));
    } catch (err) { this.onError(err); }
  }
}

// ---- The right column -----------------------------------------------------------------

export class Inspector {
  constructor({ onError, onNode }) {
    this.onError = onError;
    this.onNode = onNode;
    this.facts = document.getElementById("run-facts");
    this.live = document.getElementById("calls-live");
    this.sessionsList = document.getElementById("sessions-list");
    this.jobsList = document.getElementById("jobs-list");
    this.jobLog = document.getElementById("job-log");
    this.dialog = document.getElementById("dialog-session");
    this.calls = new Map();        // label -> row while it runs
    this.runId = null;
    document.getElementById("btn-sessions").addEventListener("click", () => this.loadSessions());
    document.getElementById("session-close").addEventListener("click", () => this.dialog.close());
  }

  setRun(runId) {
    this.runId = runId;
    clear(this.live);
    clear(this.sessionsList);
    clear(this.jobsList);
    this.jobLog.hidden = true;
    this.calls.clear();
  }

  showFacts(d) {
    const rows = [
      ["Folder", d.folder], ["Stage", d.stage], ["Phase", d.phase],
      ["Now", d.busy ? (d.current || "working") : d.waiting ? "waiting for you" : d.stopped_midway ? "stopped midway" : "idle"],
      ["Waiting on your go", d.awaiting || ""], ["Outcome", d.outcome || ""], ["Last error", d.error || ""],
    ];
    clear(this.facts).append(...rows.filter(([, v]) => v).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]),
      el("dt", { text: "Models" }),
      el("dd", {}, el("table", { class: "grid small" }, el("tbody", {},
        Object.entries(d.roles || {}).map(([r, m]) => el("tr", {}, el("td", { text: r }), el("td", { class: "mono", text: m })))))));
  }

  // A model call started or ended (from the event stream).
  call(e) {
    if (e.state === "start") {
      const row = el("div", { class: "call running" },
        el("span", { class: "spinner" }), el("b", { text: e.label }), el("span", { class: "dim small", text: ` ${e.model}` }),
        e.session ? el("a", { href: "#", class: "small", text: ` ${e.session}`, onclick: (ev) => { ev.preventDefault(); this.openSession(e.session); } }) : null,
        e.attempt > 1 ? pill(`attempt ${e.attempt}`, "warn") : null,
        e.node ? el("a", { href: "#", class: "small live-link", text: " live \u2197", title: `Watch ${e.node} work, in its own window`,
          onclick: (ev) => { ev.preventDefault(); this.onNode && this.onNode(e.node); } }) : null);
      this.calls.set(e.label, row);
      this.live.prepend(row);
    } else {
      const row = this.calls.get(e.label);
      if (!row) return;
      row.classList.remove("running");
      row.classList.add(e.state === "problems" ? "bad" : "done");
      row.querySelector(".spinner")?.remove();
      const tokens = e.tokens ? ` · ${e.tokens.input.toLocaleString()} in / ${e.tokens.output.toLocaleString()} out` : "";
      row.append(el("span", { class: "dim small", text: ` ${e.seconds}s${tokens}` }));
      if ((e.problems || []).length) row.append(el("div", { class: "small bad", text: e.problems.join("; ") }));
      this.calls.delete(e.label);
    }
    while (this.live.children.length > 60) this.live.lastChild.remove();
  }

  async loadSessions() {
    if (!this.runId) return;
    try {
      const sessions = await api.sessions(this.runId);
      clear(this.sessionsList).append(...sessions.map((s) => el("a", {
        href: "#", class: "call done", onclick: (e) => { e.preventDefault(); this.openSession(s.id); },
      }, el("b", { text: s.id }), el("span", { class: "dim small", text: ` ${s.provider}${s.model ? ":" + s.model : ""} · ${s.turns} turns · ${when(s.updated)}` }))));
    } catch (err) { this.onError(err); }
  }

  async openSession(sid) {
    try {
      const s = await api.session(this.runId, sid);
      document.getElementById("session-title").textContent = `${s.id} — ${s.provider}${s.model ? ":" + s.model : ""}, ${s.access} access`;
      const body = clear(document.getElementById("session-body"));
      body.append(el("details", { class: "fold" }, el("summary", { text: "Standing instructions" }),
        el("pre", { class: "pre small", text: s.instructions || "" })));
      for (const t of s.turns || []) {
        let reply = t.reply || t.error || "";
        try { reply = JSON.stringify(JSON.parse(reply), null, 2); } catch { /* plain text */ }
        body.append(el("div", { class: "turn" },
          el("div", { class: "sv-head" }, el("b", { text: `Turn ${t.turn}` }),
            el("span", { class: "dim small", text: `${when(t.at)} · ${t.seconds || "?"}s${t.schema ? " · " + t.schema : ""}` }),
            t.error ? pill("error", "bad") : null),
          el("details", { class: "fold" }, el("summary", { text: `Sent (${(t.prompt || "").length.toLocaleString()} characters)` }),
            el("pre", { class: "pre small", text: t.prompt || "" })),
          el("pre", { class: "pre small reply", text: reply })));
      }
      this.dialog.showModal();
    } catch (err) { this.onError(err); }
  }

  async loadJobs() {
    if (!this.runId) return;
    try {
      const jobs = await api.jobs(this.runId);
      clear(this.jobsList);
      if (!jobs.length) { this.jobsList.append(el("div", { class: "empty", text: "No jobs (they come with the execution)." })); return; }
      this.jobsList.append(el("table", { class: "grid small" },
        el("thead", {}, el("tr", {}, ["Job", "Step", "State", "GPUs", "Hours", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, jobs.slice().reverse().map((j) => el("tr", {},
          el("td", { class: "mono", text: j.id }), el("td", { text: j.step }),
          el("td", {}, pill(j.state + (j.exit != null ? ` ${j.exit}` : ""), j.state === "running" ? "live" : j.exit === 0 ? "ok" : "bad")),
          el("td", { text: (j.gpus || []).join(",") || "-" }), el("td", { text: j.hours.toFixed(2) }),
          el("td", {}, el("button", { class: "btn tiny", text: "log", onclick: () => this.showLog(j.id) })))))));
    } catch (err) { this.onError(err); }
  }

  async showLog(job) {
    try {
      const { text } = await api.jobLog(this.runId, job);
      this.jobLog.hidden = false;
      this.jobLog.textContent = `---- ${job}\n${text}`;
    } catch (err) { this.onError(err); }
  }
}
