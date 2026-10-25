// One graph node's model calls, live (/node/<run>/<node>): what the model is
// doing right now -- the commands it runs and their output, files it changes,
// its reasoning, what it is writing -- and every earlier call of the node,
// with what it was sent and what it answered.

import { api } from "./api.js";
import { el, clear, pill, when } from "./dom.js";

const $ = (id) => document.getElementById(id);
const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
const runId = parts[1];
const node = parts[2];
const LIVE_MS = 1000;
const PAST_MS = 5000;

const view = { running: [], past: [], selected: null, sent: new Map(), pastAt: 0, lastRunning: "", open: {} };

// ---- what an event looks like --------------------------------------------------------

const MARK = { command: "$", tool: "*", file_change: "~", reasoning: "·", message: ">",
  web_search: "?", plan: "=", error: "!", answer: "\u2713", turn: "\u2500" };

// The events worth a row: a command once (its start, completed with its
// output), the newest reasoning, files, messages, searches, errors.
export function rows(events) {
  const out = [];
  const open = new Map();          // command -> its row, until it completes
  for (const e of events || []) {
    const kind = e.kind || "";
    if (kind === "command" || kind === "tool") {
      const key = `${kind}:${e.command}`;
      if (e.phase === "started") {
        const row = { kind, text: shell(e.command || kind), at: e.at, running: true };
        open.set(key, row);
        out.push(row);
      } else {
        const row = open.get(key) || (out.push({ kind, text: shell(e.command || kind), at: e.at }), out[out.length - 1]);
        row.running = false;
        row.exit = e.exit_code;
        if (e.output) row.output = e.output;
        open.delete(key);
      }
    } else if (kind === "file_change" && e.phase !== "started") {
      out.push({ kind, text: (e.changes || []).map((c) => `${c.kind ? c.kind + " " : ""}${c.path}`).join(", "), at: e.at });
    } else if (kind === "reasoning" && (e.summary || []).length) {
      const last = out[out.length - 1];
      const text = e.summary.join("\n");
      if (last && last.kind === "reasoning" && text.startsWith(last.text.slice(0, 40))) last.text = text;
      else out.push({ kind, text, at: e.at });
    } else if (kind === "message" && e.phase !== "started" && e.text) {
      const final = e.is_final_json === true || e.is_final_json === "True";
      out.push(final ? { kind: "answer", text: "answered (its structured answer is below)", at: e.at }
        : { kind, text: e.text, at: e.at });
    } else if (kind === "turn_started" || kind === "turn_completed") {
      out.push({ kind: "turn", text: kind === "turn_started" ? "turn started" : "turn ended", at: e.at });
    } else if (kind === "web_search" && e.phase !== "started") {
      out.push({ kind, text: `searched: ${e.query || ""}`, at: e.at });
    } else if (kind === "plan" && e.text) {
      out.push({ kind, text: e.text, at: e.at });
    } else if (kind === "error") {
      out.push({ kind, text: e.message || "error", at: e.at });
    }
  }
  return out;
}

function shell(command) {
  const m = String(command).match(/^(?:\/bin\/)?(?:ba)?sh -lc ['"]([\s\S]*)['"]$/);
  return m ? m[1] : String(command);
}

function row(r) {
  const failed = r.exit != null && r.exit !== 0;
  return el("div", { class: `tx tx-${r.kind} ${r.running ? "running" : ""} ${failed ? "failed" : ""}` },
    el("span", { class: "tx-mark", text: r.running ? "◌" : MARK[r.kind] || "•" }),
    el("div", { class: "tx-body" },
      el("div", { class: "tx-text", text: r.text }),
      failed ? el("div", { class: "tx-exit", text: `exit ${r.exit}` }) : null,
      r.output ? el("pre", { class: "tx-out", text: tail(r.output, 12) }) : null),
    r.at != null ? el("span", { class: "tx-at", text: `${Math.round(r.at)}s` }) : null);
}

function tail(text, lines) {
  const all = String(text).replace(/\s+$/, "").split("\n");
  return (all.length > lines ? `... ${all.length - lines} more lines\n` : "") + all.slice(-lines).join("\n");
}

// ---- drawing --------------------------------------------------------------------------

function drawList() {
  const list = clear($("call-list"));
  for (const c of view.running) {
    list.append(callRow(c, true));
  }
  for (const c of view.past) list.append(callRow(c, false));
  if (!view.running.length && !view.past.length) list.append(el("div", { class: "empty", text: "No calls yet." }));
}

function key(c, running) {
  return running ? `run:${c.session}` : `done:${c.session}:${c.turn}:${c.attempt}`;
}

function callRow(c, running) {
  const k = key(c, running);
  return el("a", {
    href: "#", class: `call ${running ? "running" : (c.problems || []).length ? "bad" : "done"} ${view.selected === k ? "selected" : ""}`,
    onclick: (e) => { e.preventDefault(); $("follow").checked = running; select(k); },
  }, running ? el("span", { class: "spinner" }) : null,
  el("b", { text: c.label }),
  el("div", { class: "dim small", text: running ? `${c.session} · working ${Math.round(c.elapsed)}s · ${c.events_total} events`
    : `${c.session} · ${when(c.at)} · ${c.seconds}s${c.attempt > 1 ? ` · attempt ${c.attempt}` : ""}` }));
}

function select(k) {
  view.selected = k;
  drawList();
  drawSelected();
}

function current() {
  if (!view.selected) return null;
  const [state] = view.selected.split(":");
  const pool = state === "run" ? view.running : view.past;
  return pool.find((c) => key(c, state === "run") === view.selected) || null;
}

async function sessionOf(sid) {
  try {
    if (!view.sent.has(sid)) view.sent.set(sid, await api.session(runId, sid));
    return view.sent.get(sid);
  } catch { return null; }
}

// What the node is: its role, the model it runs on, skills, access, steps,
// and its standing instructions and part of the design.
async function drawNode() {
  const box = clear($("node-info"));
  try {
    const n = await api.nodeInfo(runId, node);
    const r = n.runs_on || {};
    box.append(...[el("h2", { text: "Node" }),
      el("dl", { class: "facts small" },
        [["Role", `${n.role}${n.group ? ` (${n.group})` : ""}`],
          ["Runs on", `${r.provider}${r.model ? " \u00b7 " + r.model : ""}`],
          ["Effort", r.effort], ["Access", n.access], ["Steps", (n.steps || []).join(", ")],
          ["Skills", (n.skills || []).join(", ")]]
          .filter(([, v]) => v).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      n.design ? el("details", { class: "fold" }, el("summary", { text: "Its part of the design" }),
        el("pre", { class: "pre small", text: n.design })) : null,
      n.instructions ? el("details", { class: "fold" }, el("summary", { text: "Its standing instructions" }),
        el("pre", { class: "pre small", text: n.instructions })) : null,
      el("p", { class: "hint", text: "Change the model in the main page's Run tab." })].filter(Boolean));
  } catch (err) {
    box.append(el("div", { class: "dim small", text: err.message }));
  }
}

function section(title, body, open, cls) {
  return el("details", { class: `call-section ${cls || ""}`, open }, el("summary", { text: title }), body);
}

async function drawSelected() {
  const c = current();
  const stream = $("stream");
  const tails = clear($("tails"));
  const head = clear($("call-head"));
  if (!c) {
    clear(stream).append(el("div", { class: "empty", text: view.running.length || view.past.length ? "Pick a call." : "Waiting for this node's first call..." }));
    return;
  }
  const running = view.selected.startsWith("run:");
  const s = await sessionOf(c.session);
  head.append(...[el("b", { text: c.label }), " ", el("span", { class: "mono small dim", text: `${c.session}${c.model ? " · " + c.model : ""}` }), " ",
    running ? pill(`working ${Math.round(c.elapsed)}s${c.idle != null ? ` · quiet ${Math.round(c.idle)}s` : ""}`, "live")
      : pill(`${c.seconds}s`, (c.problems || []).length ? "bad" : "ok"),
    running && c.events_total > c.events.length ? el("span", { class: "dim small", text: ` showing the last ${c.events.length} of ${c.events_total} events` }) : null]
    .filter((x) => x !== null));
  const pinned = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60;
  const open = view.open;                     // which sections the user opened or closed
  const keep = (name, dflt) => (name in open ? open[name] : dflt);
  // What it ran with -- from its session.
  const settings = s ? el("div", { class: "settings mono small" },
    `${s.provider}${s.model ? ":" + s.model : " (default model)"}${s.effort ? " \u00b7 effort " + s.effort : ""}`
    + ` \u00b7 ${s.access} access${s.skills ? ` \u00b7 skills: ${Array.isArray(s.skills) ? s.skills.join(", ") : s.skills}` : ""}`
    + ` \u00b7 works in ${s.workdir || "-"}`) : null;
  // What it was sent: the message of the call under way, or of this turn.
  const turn = !running && s ? (s.turns || []).find((t) => String(t.turn) === String(c.turn)) : null;
  const sent = running ? c.prompt || "" : (turn && turn.prompt) || "";
  const items = rows(c.events).map(row);
  let answer = c.reply || "";
  try { answer = JSON.stringify(JSON.parse(answer), null, 2); } catch { /* text */ }
  clear(stream).append(...[
    settings,
    section(`Sent to the model (${sent.length.toLocaleString()} characters)`,
      el("pre", { class: "pre small sent", text: sent || "(not known: the call may predate this view)" }),
      keep("sent", true), "sent-section"),
    section(`What it did (${items.length} of ${c.events_total ?? (c.events || []).length} events shown; bookkeeping left out)`,
      el("div", { class: "tx-list" }, items.length ? items : el("div", { class: "empty", text: running ? "Started; nothing reported yet." : "No events were recorded for this call." })),
      keep("did", true), "did-section"),
    !running && (c.problems || []).length ? el("div", { class: "tx tx-error" }, el("span", { class: "tx-mark", text: "!" }),
      el("div", { class: "tx-body", text: `answer not usable: ${c.problems.join("; ")}` })) : null,
    !running ? section("Its answer", el("pre", { class: "pre small reply", text: answer || "(none)" }), keep("answer", true), "answer-section") : null,
    s && s.instructions ? section("Its standing instructions (sent with every message)",
      el("pre", { class: "pre small", text: s.instructions }), keep("instructions", false), "instructions-section") : null,
  ].filter(Boolean));
  for (const [name, cls] of [["sent", "sent-section"], ["did", "did-section"], ["answer", "answer-section"], ["instructions", "instructions-section"]]) {
    const d = stream.querySelector(`.${cls}`);
    if (d) d.addEventListener("toggle", () => { view.open[name] = d.open; });
  }
  if (running) {
    for (const [name, text] of Object.entries(c.live || {})) {
      if (text && text.trim()) tails.append(el("div", { class: "tail" }, el("div", { class: "tail-head", text: `writing now: ${name}` }), el("pre", { text: tail(text, 30) })));
    }
  }
  if (pinned && running) stream.scrollTop = stream.scrollHeight;
}

// ---- keeping it live -------------------------------------------------------------------------

async function pollLive() {
  try {
    const { running } = await api.live(runId, node);
    const ids = running.map((c) => c.session).join(",");
    const ended = view.lastRunning && view.lastRunning !== ids;
    view.running = running;
    view.lastRunning = ids;
    $("live-dot").classList.add("on");
    const state = running.length ? `working: ${running.map((c) => c.label).join(", ")}` : "idle";
    $("node-state").textContent = state;
    $("node-state").className = `pill ${running.length ? "pill-live" : ""}`;
    if (running.length && $("follow").checked && !(view.selected || "").startsWith("run:" + running[0].session)) {
      view.selected = key(running[0], true);
    }
    if (ended || Date.now() - view.pastAt > PAST_MS) await pollPast(ended);
    if (view.selected && !current() && view.selected.startsWith("run:") && view.past.length) {
      view.selected = key(view.past[0], false);       // it just finished: show how it ended
    }
    if (!view.selected && view.past.length) view.selected = key(view.past[0], false);
    drawList();
    const c = current();
    if (c && (view.selected.startsWith("run:") || view.redraw)) { view.redraw = false; await drawSelected(); }
  } catch {
    $("live-dot").classList.remove("on");
  }
}

async function pollPast(force) {
  view.pastAt = Date.now();
  const { calls } = await api.activity(runId, node);
  const before = view.past.map((c) => key(c, false)).join(",");
  view.past = calls;
  if (force || before !== view.past.map((c) => key(c, false)).join(",")) view.redraw = true;
}

$("node-name").textContent = node;
$("run-name").textContent = runId;
document.title = `${node} — ${runId}`;
drawNode();
pollLive().then(drawSelected);
setInterval(pollLive, LIVE_MS);
