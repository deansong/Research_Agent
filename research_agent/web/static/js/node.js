// One graph node's model calls (/node/<run>/<node>): on the left, every call
// in a line or three -- what went in, what came out, where it handed to --
// with the call under way pinned on top; on the right, the chosen call in
// tabs: a summary, the brief it was sent (by section), what it did, its
// answer as a tree, and the raw JSON. The node's settings are in a drawer.

import { api } from "./api.js";
import { el, clear, pill } from "./dom.js";
import { markdown } from "./md.js";

const $ = (id) => document.getElementById(id);
const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
const runId = parts[1];
const node = parts[2];
const LIVE_MS = 1000;
const PAST_MS = 5000;

const query = new URLSearchParams(location.search);
const view = { running: [], past: [], selected: null, sent: new Map(), pastAt: 0, lastRunning: "",
  tab: query.get("tab") || "summary", filter: "all", redraw: false };

// ---- what an event looks like --------------------------------------------------------

const MARK = { command: "$", tool: "*", file_change: "~", reasoning: "·", message: ">",
  web_search: "?", plan: "=", error: "!", answer: "✓", turn: "─" };

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
      out.push(final ? { kind: "answer", text: "answered (see Summary and Output)", at: e.at }
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

// ---- a brief and an answer, read ------------------------------------------------------

// A brief's "=== TITLE ===" sections: [title, text]; text before the first is "".
export function sections(prompt) {
  const out = [];
  const re = /^=== (.+?) ===$/gm;
  let last = { title: "", start: 0 };
  let m;
  while ((m = re.exec(prompt || ""))) {
    out.push([last.title, prompt.slice(last.start, m.index).trim()]);
    last = { title: m[1], start: m.index + m[0].length };
  }
  out.push([last.title, (prompt || "").slice(last.start).trim()]);
  return out.filter(([t, x]) => t || x);
}

function parsed(text) {
  try { const d = JSON.parse(text); return d && typeof d === "object" ? d : null; } catch { return null; }
}

const isEmpty = (v) => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)
  || (typeof v === "object" && !Array.isArray(v) && Object.values(v).every(isEmpty));

function oneLine(text, n) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n - 1) + "…" : flat;
}

function kTokens(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : String(n || 0);
}

function clock(at) {
  const d = new Date(at);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function day(at) {
  const d = new Date(at);
  if (isNaN(d)) return "";
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

// ---- the list of calls -----------------------------------------------------------------

const FILTERS = [["all", "all"], ["asides", "asides"], ["problems", "problems"]];

function shown(c) {
  if (view.filter === "asides") return /\(aside\)/.test(c.label || "");
  if (view.filter === "problems") return (c.problems || []).length || c.attempt > 1;
  return true;
}

function key(c, running) {
  return running ? `run:${c.session}` : `done:${c.session}:${c.turn}:${c.attempt}`;
}

function drawFilter() {
  clear($("call-filter")).append(...FILTERS.map(([f, text]) => el("button", {
    class: `chip ${view.filter === f ? "on" : ""}`, text,
    onclick: () => { view.filter = f; drawFilter(); drawList(); } })));
}

function drawList() {
  const list = clear($("call-list"));
  for (const c of view.running) list.append(liveRow(c));
  let lastDay = null;
  const past = view.past.filter(shown);
  for (const c of past) {
    const d = day(c.at);
    if (d !== lastDay) { list.append(el("div", { class: "day", text: d })); lastDay = d; }
    list.append(callRow(c));
  }
  if (!view.running.length && !past.length) {
    list.append(el("div", { class: "empty", text: view.past.length ? "No calls match." : "No calls yet." }));
  }
  const sel = list.querySelector(".crow.selected");
  if (sel && view.scrollList) { sel.scrollIntoView({ block: "nearest" }); view.scrollList = false; }
}

function pick(k, running) {
  $("follow").checked = !!running;
  view.scrollList = true;
  select(k);
}

function liveRow(c) {
  const k = key(c, true);
  return el("a", { href: "#", class: `crow running ${view.selected === k ? "selected" : ""}`,
    onclick: (e) => { e.preventDefault(); pick(k, true); } },
  el("div", { class: "l1" }, el("span", { class: "spinner" }), el("b", { text: "now" }),
    el("span", { class: "dim", text: ` working ${Math.round(c.elapsed)}s` }),
    c.attempt > 1 ? pill(`attempt ${c.attempt}`, "warn") : null,
    /\(aside\)/.test(c.label) ? pill("aside", "human") : null),
  el("div", { class: "l2", text: oneLine(saidOf(c.prompt || ""), 110) }),
  c.notice ? el("div", { class: "l3 waiting", text: `\u23f8 ${oneLine(c.notice, 110)}` })
    : c.last ? el("div", { class: "l3 dim", text: oneLine(c.last, 110) }) : null);
}

function callRow(c) {
  const k = key(c, false);
  const bad = (c.problems || []).length;
  const tokens = c.tokens ? `${kTokens(c.tokens.input_tokens)} in · ${kTokens(c.tokens.output_tokens)} out` : "";
  return el("a", { href: "#", class: `crow ${bad ? "bad" : ""} ${view.selected === k ? "selected" : ""}`,
    title: `${c.label} · ${c.session}${tokens ? " · " + tokens : ""}`,
    onclick: (e) => { e.preventDefault(); pick(k, false); } },
  el("div", { class: "l1" },
    el("span", { class: `mark ${bad ? "bad" : "ok"}`, text: bad ? "⚠" : "✓" }),
    el("span", { class: "mono", text: clock(c.at) }),
    el("span", { class: "dim", text: ` ${c.seconds}s` }),
    tokens ? el("span", { class: "dim", text: ` · ${kTokens(c.tokens.input_tokens)}` }) : null,
    c.attempt > 1 ? pill(`retry ${c.attempt}`, "warn") : null,
    /\(aside\)/.test(c.label) ? pill("aside", "human") : null,
    c.label !== node && !/\(aside\)/.test(c.label) ? el("span", { class: "dim small", text: ` ${c.label}` }) : null,
    c.to ? el("span", { class: "to", text: `→ ${c.to}` }) : null),
  c.said ? el("div", { class: "l2", text: c.said }) : null,
  c.outcome ? el("div", { class: `l3 ${bad ? "bad" : ""}`, text: c.outcome }) : null);
}

function saidOf(prompt) {
  if (prompt.startsWith("Your answer could not be used:")) return "retry: " + (prompt.split("\n")[1] || "").replace(/^- /, "");
  const s = Object.fromEntries(sections(prompt));
  const aside = Object.keys(s).some((t) => t.startsWith("AN ASIDE"));
  if (s["THE USER SAYS"]) return `${aside ? "you (aside)" : "you"}: ${s["THE USER SAYS"]}`;
  for (const t of ["THE LAST PIECE OF WORK", "TASK", "YOUR TASK", "THE STEP", "NOW"]) if (s[t]) return s[t];
  return (sections(prompt).find(([, x]) => x) || ["", ""])[1];
}

function visibleKeys() {
  return [...view.running.map((c) => key(c, true)), ...view.past.filter(shown).map((c) => key(c, false))];
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

// A session's settings and turns; fetched again when it lacks the turn wanted.
async function sessionOf(sid, turn) {
  try {
    const have = view.sent.get(sid);
    if (!have || (turn != null && !(have.turns || []).some((t) => String(t.turn) === String(turn)))) {
      view.sent.set(sid, await api.session(runId, sid));
    }
    return view.sent.get(sid);
  } catch { return null; }
}

// ---- the chosen call -------------------------------------------------------------------

const TABS_PAST = [["summary", "Summary"], ["input", "Input"], ["activity", "Activity"], ["output", "Output"], ["raw", "Raw"]];
const TABS_LIVE = [["live", "Live"], ["input", "Input"], ["raw", "Raw"]];

// The fields of an answer that say what it decided, in the order they are read.
const HEADLINE = [["reply", "Reply"], ["hand_to", "Hands to"], ["action", "Action"], ["step", "Step"],
  ["verdict", "Verdict"], ["state", "State"], ["reason", "Why"], ["task", "Task"], ["summary", "Summary"],
  ["next", "Next"], ["note_for_discussor", "For the discussor"], ["questions_for_user", "For the user"],
  ["problems", "Problems"]];

async function drawSelected() {
  const c = current();
  const stream = $("stream");
  const head = clear($("call-head"));
  const tabs = clear($("call-tabs"));
  clear($("tails"));
  if (!c) {
    clear(stream).append(el("div", { class: "empty", text: view.running.length || view.past.length ? "Pick a call." : "Waiting for this node's first call..." }));
    return;
  }
  const running = view.selected.startsWith("run:");
  const s = await sessionOf(c.session, running ? null : c.turn);
  const turn = !running && s ? (s.turns || []).find((t) => String(t.turn) === String(c.turn)) : null;
  const prompt = running ? c.prompt || "" : (turn && turn.prompt) || "";
  const reply = running ? "" : (turn && turn.reply) || c.reply || "";
  const data = parsed(reply);
  const tokens = c.tokens || (turn && turn.tokens);

  head.append(...[
    running ? pill(`working ${Math.round(c.elapsed)}s${c.idle != null ? ` · quiet ${Math.round(c.idle)}s` : ""}`, "live")
      : pill((c.problems || []).length ? "not usable" : "answered", (c.problems || []).length ? "bad" : "ok"),
    el("b", { text: ` ${c.label}` }),
    el("span", { class: "dim small", text: [running ? "" : `${day(c.at)} ${clock(c.at)}`, running ? "" : `${c.seconds}s`,
      tokens ? `${kTokens(tokens.input_tokens)} in / ${kTokens(tokens.output_tokens)} out` : "",
      c.attempt > 1 ? `attempt ${c.attempt}` : "", c.session].filter(Boolean).map((x) => ` · ${x}`).join("") }),
  ]);

  const list = running ? TABS_LIVE : TABS_PAST;
  if (!list.some(([t]) => t === view.tab)) view.tab = running ? "live" : "summary";
  tabs.append(...list.map(([t, text]) => el("button", { class: `tab ${view.tab === t ? "active" : ""}`, text,
    onclick: () => { view.tab = t; view.top = true; drawSelected(); } })));

  const pinned = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60;
  const scroll = stream.scrollTop;
  let body;
  if (view.tab === "summary") body = summary(c, prompt, data, reply);
  else if (view.tab === "input") body = input(prompt);
  else if (view.tab === "activity") body = activity(c, false);
  else if (view.tab === "output") body = data ? el("div", { class: "tree" }, tree(data)) : el("pre", { class: "pre small", text: reply || "(none)" });
  else if (view.tab === "raw") body = raw(s, reply, running);
  else body = activity(c, true);
  clear(stream).append(...[running && c.notice ? waitingBox(c) : null, body].filter(Boolean));
  if (view.top) { stream.scrollTop = 0; view.top = false; }
  else if (running && view.tab === "live" && pinned) stream.scrollTop = stream.scrollHeight;
  else stream.scrollTop = scroll;

  if (running && view.tab === "live") {
    for (const [name, text] of Object.entries(c.live || {})) {
      if (text && text.trim()) $("tails").append(el("div", { class: "tail" }, el("div", { class: "tail-head", text: `writing now: ${name}` }), el("pre", { text: tail(text, 30) })));
    }
  }
}

// A call that is not working but waiting: for credits, or for a full model.
function waitingBox(c) {
  const until = c.until ? new Date(c.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return el("div", { class: "callout waiting" },
    el("b", { text: `Waiting${until ? ` until ${until}` : ""} -- not working` }),
    el("div", { class: "pre", text: c.notice }),
    el("div", { class: "small", text: "Pause (main page) stops the wait at once; give the role a backup or another model in the Run tab, then Carry on." }));
}

function summary(c, prompt, data, reply) {
  const out = [];
  if ((c.problems || []).length) {
    out.push(el("div", { class: "callout bad" }, el("b", { text: "Its answer could not be used" }),
      el("ul", {}, c.problems.map((p) => el("li", { text: p })))));
  }
  const s = Object.fromEntries(sections(prompt));
  const said = s["THE USER SAYS"];
  out.push(el("div", { class: "sum-block" }, el("h2", { text: said ? "You said" : "Asked to" }),
    said ? el("blockquote", { class: "said", text: said })
      : el("div", { class: "said-task", text: oneLine(saidOf(prompt), 600) || "(not known)" }),
    el("a", { href: "#", class: "small", text: `the whole brief: ${prompt.length.toLocaleString()} characters, ${sections(prompt).length} sections →`,
      onclick: (e) => { e.preventDefault(); view.tab = "input"; view.top = true; drawSelected(); } })));
  if (!data) {
    out.push(el("div", { class: "sum-block" }, el("h2", { text: "It answered" }),
      el("div", { class: "msg-text", html: markdown(reply || "(nothing)") })));
    return el("div", { class: "summary" }, out);
  }
  const facts = [];
  for (const [k, label] of HEADLINE) {
    const v = data[k];
    if (isEmpty(v)) continue;
    let shownValue;
    if (k === "reply") shownValue = el("div", { class: "msg-text", html: markdown(String(v)) });
    else if (Array.isArray(v)) shownValue = el("ul", { class: "plain" }, v.map((x) => el("li", { text: typeof x === "string" ? x : JSON.stringify(x) })));
    else if (["hand_to", "action", "verdict", "state"].includes(k)) shownValue = pill(String(v), v === "pass" || v === "done" ? "ok" : v === "fail" || v === "failed" ? "bad" : "live");
    else shownValue = el("div", { class: "pre", text: String(v) });
    facts.push(el("dt", { text: label }), el("dd", {}, shownValue));
  }
  out.push(el("div", { class: "sum-block" }, el("h2", { text: "It answered" }),
    facts.length ? el("dl", { class: "sum-facts" }, facts) : el("div", { class: "dim", text: "(see Output)" })));
  // What else it recorded: the lists and objects with something in them.
  const headline = new Set(HEADLINE.map(([k]) => k));
  const more = Object.entries(data).filter(([k, v]) => !headline.has(k) && !isEmpty(v) && typeof v === "object");
  if (more.length) {
    out.push(el("div", { class: "sum-block" }, el("h2", { text: "It also gave" }),
      el("div", { class: "chips" }, more.map(([k, v]) => el("a", { href: "#", class: "chip on",
        text: Array.isArray(v) ? `${k.replace(/_/g, " ")}: ${v.length}`
          : `${k.replace(/_/g, " ")}: ${Object.keys(v).filter((x) => !isEmpty(v[x])).join(", ")}`,
        onclick: (e) => { e.preventDefault(); view.tab = "output"; view.top = true; view.focus = k; drawSelected(); } })))));
  }
  return el("div", { class: "summary" }, out);
}

function input(prompt) {
  if (!prompt) return el("div", { class: "empty", text: "Not known: the call may predate this view." });
  const secs = sections(prompt);
  const id = (i) => `sec-${i}`;
  return el("div", { class: "brief" },
    el("div", { class: "sec-index" },
      el("span", { class: "dim small", text: `${prompt.length.toLocaleString()} characters · ` }),
      secs.map(([t], i) => el("a", { href: "#", class: "chip", text: t || "(start)",
        onclick: (e) => { e.preventDefault(); const d = document.getElementById(id(i)); d.open = true; d.scrollIntoView({ block: "start" }); } })),
      el("a", { href: "#", class: "small", text: " collapse all", onclick: (e) => {
        e.preventDefault(); document.querySelectorAll(".brief details").forEach((d) => { d.open = false; }); } })),
    secs.map(([t, text], i) => el("details", { class: "call-section", id: id(i), open: text.length < 6000 },
      el("summary", {}, el("b", { text: t || "(start)" }), el("span", { class: "dim small", text: `  ${text.length.toLocaleString()} characters` })),
      el("pre", { class: "pre small sent", text }))));
}

function activity(c, running) {
  const items = rows(c.events).map(row);
  return el("div", {},
    el("div", { class: "dim small", text: `${items.length} of ${c.events_total ?? (c.events || []).length} events shown; bookkeeping left out` }),
    el("div", { class: "tx-list" }, items.length ? items
      : el("div", { class: "empty", text: running ? "Started; nothing reported yet." : "No events were recorded for this call." })));
}

// An answer as a tree: empty fields left out; lists of records as cards.
function tree(v, k) {
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x !== "object" || x === null)) return el("ul", { class: "plain" }, v.map((x) => el("li", { text: String(x) })));
    return el("div", { class: "cards" }, v.map((x, i) => el("div", { class: "card" },
      el("div", { class: "card-head dim small", text: x && (x.id || x.what || x.title) ? String(x.id || x.what || x.title).slice(0, 80) : `#${i + 1}` }),
      tree(x))));
  }
  if (v && typeof v === "object") {
    const entries = Object.entries(v).filter(([, x]) => !isEmpty(x));
    if (!entries.length) return el("span", { class: "dim", text: "(empty)" });
    return el("dl", { class: "tree-dl" }, entries.flatMap(([name, x]) => {
      const dt = el("dt", { text: name.replace(/_/g, " ") });
      if (k === undefined && view.focus === name) { dt.classList.add("focus"); setTimeout(() => dt.scrollIntoView({ block: "start" }), 0); }
      return [dt, el("dd", {}, tree(x, name))];
    }));
  }
  return el("div", { class: "pre", text: String(v) });
}

function raw(s, reply, running) {
  let pretty = reply;
  try { pretty = JSON.stringify(JSON.parse(reply), null, 2); } catch { /* text */ }
  return el("div", {},
    s ? el("div", { class: "settings mono small" },
      `${s.provider}${s.model ? ":" + s.model : " (default model)"}${s.effort ? " · effort " + s.effort : ""}`
      + ` · ${s.access} access${s.skills ? ` · skills: ${Array.isArray(s.skills) ? s.skills.join(", ") : s.skills}` : ""}`
      + ` · works in ${s.workdir || "-"}`) : null,
    running ? null : el("details", { class: "call-section", open: true }, el("summary", { text: "Its answer, as sent back" }),
      el("pre", { class: "pre small reply", text: pretty || "(none)" })),
    s && s.instructions ? el("details", { class: "call-section" }, el("summary", { text: "Its standing instructions (sent with every message)" }),
      el("pre", { class: "pre small", text: s.instructions })) : null);
}

// ---- the node, in a drawer ---------------------------------------------------------------

async function drawNode() {
  const box = clear($("node-info"));
  try {
    const n = await api.nodeInfo(runId, node);
    const r = n.runs_on || {};
    $("node-model").textContent = [`${r.provider || ""}${r.model && !r.model.startsWith("(") ? ":" + r.model : ""}`,
      r.effort && !r.effort.startsWith("(") ? `effort ${r.effort}` : "", n.access].filter(Boolean).join(" · ");
    box.append(...[
      el("dl", { class: "facts small" },
        [["Role", `${n.role}${n.group ? ` (${n.group})` : ""}`],
          ["Runs on", `${r.provider}${r.model ? " · " + r.model : ""}`],
          ["Effort", r.effort], ["Access", n.access], ["Steps", (n.steps || []).join(", ")]]
          .filter(([, v]) => v).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      el("p", { class: "hint", text: "Change the model and its backups in the main page's Run tab." }),
      node === "researcher" ? await sourcesBox() : null,
      await skillsBox(n),
      n.design ? el("details", { class: "fold" }, el("summary", { text: "Its part of the design" }),
        el("pre", { class: "pre small", text: n.design })) : null,
      n.instructions ? el("details", { class: "fold" }, el("summary", { text: "Its standing instructions" }),
        el("pre", { class: "pre small", text: n.instructions })) : null].filter(Boolean));
  } catch (err) {
    box.append(el("div", { class: "dim small", text: err.message }));
  }
}

// The researcher's literature sources: a box each, saved as soon as it is
// ticked. A source whose key is not set cannot be ticked.
function showSources(sources) {
  const chip = $("node-sources");
  chip.hidden = node !== "researcher";
  chip.textContent = `sources: ${sources.join(", ") || "none"}`;
}

async function sourcesBox() {
  let r;
  try { r = await api.research(runId); } catch (err) { return el("div", { class: "dim small", text: err.message }); }
  showSources(r.sources.filter((s) => (r.available.find((a) => a.name === s) || {}).keyed));
  const picked = new Set(r.sources);
  const status = el("span", { class: "dim small" });
  const rows = r.available.map((a) => el("label", { class: `source ${a.keyed ? "" : "off"}`,
    title: a.keyed ? a.about : `Needs ${a.key} in the environment the server was started from` },
  el("input", { type: "checkbox", value: a.name, checked: picked.has(a.name), disabled: !a.keyed }),
  el("span", {}, el("b", { text: a.label }), el("span", { class: "dim", text: ` — ${a.about}` })),
  a.keyed ? null : el("span", { class: "pill pill-warn", text: `needs ${a.key}` })));
  const box = el("div", { class: "sources-box" }, el("h2", { text: "Literature sources" }), rows,
    el("div", { class: "hint", text: `The research skill's search uses the ticked sources; the others are never asked. Applies from the researcher's next search; kept in ${r.file}.` }),
    status);
  box.addEventListener("change", async () => {
    // A chosen source without its key stays chosen (ticked, greyed): it is
    // used once the key is set.
    const sources = [...box.querySelectorAll("input:checked")].map((i) => i.value);
    try {
      const now = await api.setResearch(runId, sources);
      status.textContent = " \u2713 saved";
      showSources(now.sources.filter((s) => (now.available.find((a) => a.name === s) || {}).keyed));
    } catch (err) { status.textContent = ` ! ${err.message}`; }
  });
  return box;
}

// The node's skills: which are given, and choosing them. A slot per place a
// choice is kept (the planner has one per job).
let catalogue = null;

async function skillsBox(n) {
  if (!(n.slots || []).length) return null;
  catalogue ||= (await api.skills(runId).catch(() => ({ skills: [] }))).skills;
  const describe = Object.fromEntries(catalogue.map((s) => [s.name, s.description]));
  return el("div", { class: "skills-box" }, el("h2", { text: "Skills" }),
    n.slots.map((slot) => skillSlot(slot, describe, n.slots.length > 1)));
}

function clip(text, n) {
  return text.length > n ? text.slice(0, n).replace(/\s+\S*$/, "") + "..." : text;
}

function skillSlot(slot, describe, labelled) {
  const status = el("span", { class: "dim small" });
  const modes = [["none", "none"], ["auto", "auto: every skill listed, the model reads what it needs"],
    ["chosen", "chosen skills"]];
  if (slot.group === "execution") modes.push(["design", "the design's"]);
  const mode = el("select", { class: "skill-mode" }, modes.map(([v, t]) => el("option", {
    value: v, text: t, selected: v === (slot.group === "execution" && slot.from_design ? "design" : slot.mode) })));
  const chosen = new Set(slot.skills);
  const list = el("div", { class: "skill-list" }, catalogue.map((s) => el("label", { class: "skill", title: s.description },
    el("input", { type: "checkbox", value: s.name, checked: chosen.has(s.name) }),
    el("span", { class: "mono", text: s.name }), el("small", { class: "dim", text: ` ${s.description.slice(0, 90)}${s.description.length > 90 ? "..." : ""}` }))));
  const showList = () => { list.hidden = mode.value !== "chosen"; };
  mode.addEventListener("change", showList);
  showList();
  const given = slot.mode === "chosen" ? slot.skills : [];
  return el("div", { class: "skill-slot" },
    labelled ? el("div", { class: "small", text: slot.label }) : null,
    el("div", { class: "small" },
      slot.mode === "none" ? "No skills." : slot.mode === "auto" ? "Every skill's name and description; the model reads those it needs."
        : null,
      given.length ? el("ul", { class: "given" }, given.map((s) => el("li", { title: describe[s] || "" },
        el("span", { class: "mono", text: s }),
        el("span", { class: "dim", text: describe[s] ? ` — ${clip(describe[s], 100)}` : " (not in the skills folder)" })))) : null,
      slot.from_design ? el("div", { class: "dim", text: "From the approved design." }) : null),
    el("details", { class: "fold" }, el("summary", { text: "Change" }),
      mode, list,
      el("div", {}, el("button", { class: "btn small primary", text: "Save", onclick: async () => {
        const skills = [...list.querySelectorAll("input:checked")].map((i) => i.value);
        try {
          await api.setSkills(runId, node, { key: slot.key, mode: mode.value, skills });
          status.textContent = " ✓ saved: used from its next call";
          drawNode();
        } catch (err) { status.textContent = ` ! ${err.message}`; }
      } }), status)),
    el("div", { class: "hint", text: `kept in ${slot.file}` }));
}

function drawer(open) {
  $("node-drawer").hidden = !open;
  $("node-settings").classList.toggle("on", open);
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
      view.redraw = true;
    }
    if (ended || Date.now() - view.pastAt > PAST_MS) await pollPast(ended);
    if (view.selected && !current() && view.selected.startsWith("run:") && view.past.length) {
      view.selected = key(view.past[0], false);       // it just finished: show how it ended
      view.redraw = true;
    }
    if (!view.selected && view.past.length) { view.selected = key(view.past[0], false); view.redraw = true; }
    drawList();
    const c = current();
    // A call under way is redrawn every second only where it changes: Live.
    if (c && ((view.selected.startsWith("run:") && view.tab === "live") || view.redraw)) {
      view.redraw = false;
      await drawSelected();
    }
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

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea")) return;
  if (e.key === "Escape") { drawer(false); return; }
  if (e.key !== "j" && e.key !== "k") return;
  const keys = visibleKeys();
  if (!keys.length) return;
  const at = keys.indexOf(view.selected);
  const next = keys[Math.max(0, Math.min(keys.length - 1, at < 0 ? 0 : at + (e.key === "j" ? 1 : -1)))];
  pick(next, next.startsWith("run:"));
});

$("node-settings").addEventListener("click", () => drawer($("node-drawer").hidden));
$("node-sources").addEventListener("click", (e) => { e.preventDefault(); drawer(true); });
$("drawer-close").addEventListener("click", () => drawer(false));
$("node-name").textContent = node;
$("run-name").textContent = runId;
document.title = `${node} — ${runId}`;
drawFilter();
drawNode();
if (query.get("drawer")) drawer(true);
pollLive().then(drawSelected);
setInterval(pollLive, LIVE_MS);
