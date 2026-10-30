// The page: opens a run, keeps every panel in step with its events, and
// wires the buttons and dialogs.

import { api, subscribe } from "./api.js";
import { Chat } from "./chat.js";
import { el, clear, when } from "./dom.js";
import { GraphPanel, nodeOfLabel } from "./graph.js";
import { FilesPanel, HistoryPanel, Inspector } from "./panels.js";
import { StateView } from "./state.js";

const $ = (id) => document.getElementById(id);
const JOBS_POLL_MS = 5000;

const app = { runId: null, detail: null, unsubscribe: null, tab: "state", side: "run", jobsTimer: null,
  graphView: null };

const chat = new Chat({ onSend: send });
const stateView = new StateView($("state-view"), { onSession: (sid) => inspector.openSession(sid), onError: problem,
  onExecute: () => { $("exec-error").textContent = ""; $("dialog-execute").showModal(); } });
const graph = new GraphPanel($("graph-canvas"), $("graph-legend"), (node, kind) => openNode(node, kind));
const files = new FilesPanel($("files-list"), $("files-view"), problem);
const historyPanel = new HistoryPanel($("history-list"), $("history-view"), problem);
const inspector = new Inspector({ onError: problem, onNode: (node) => openNode(node) });

// A node's live stream, in its own window: what its model is doing now, and
// every earlier call of it.
export function openNode(node, kind) {
  if (!app.runId) return;
  if (kind === "human") { $("composer-input").focus(); return; }
  if (node === "wait") { document.querySelector('#inspector-tabs .tab[data-tab="jobs"]').click(); return; }
  window.open(`/node/${encodeURIComponent(app.runId)}/${encodeURIComponent(node)}`,
    `node-${app.runId}-${node}`, "width=1100,height=900");
}

function problem(err) {
  chat.line(`! ${err.message || err}`, "error");
}

// ---- opening a run ---------------------------------------------------------------------

async function openRun(id) {
  if (app.unsubscribe) app.unsubscribe();
  app.unsubscribe = null;
  let d;
  try { d = await api.run(id); } catch (err) { problem(err); return; }
  app.runId = id;
  app.detail = d;
  app.graphView = null;
  window.history.replaceState(null, "", `#${encodeURIComponent(id)}`);
  chat.reset();
  chat.history(d.transcript || []);
  if (d.held_idea) chat.line(`Your idea, not sent yet: "${d.held_idea}". Choose the models, backups, skills and sources, then press Start the run.`, "note");
  stateView.setRun(id);
  stateView.load();
  inspector.setRun(id);
  graph.active = null;
  renderDetail(d);
  loadTab(app.tab);
  if (app.side === "jobs") inspector.loadJobs();
  if (app.side === "calls") inspector.loadSessions();
  inspector.loadModels();
  loadPapers();
  reviewCount = -1;
  loadReviews();
  app.unsubscribe = subscribe(id, d.last_seq, {
    status: (e) => { app.detail = { ...app.detail, ...e }; renderDetail(app.detail); },
    chat: (e) => chat.message(e.role, e.text, e.kind, e.noted),
    call: (e) => {
      if (e.state === "start") {
        chat.line(`> ${e.label}: asking ${e.model}${e.attempt > 1 ? ` (attempt ${e.attempt})` : ""}`, "call");
        graph.highlight(nodeOfLabel(e.label));
      } else {
        chat.line(`< ${e.label}: ${e.state === "problems" ? "answer not usable, retrying" : `answered in ${e.seconds}s`}`,
          e.state === "problems" ? "warn" : "call");
      }
      inspector.call(e);
    },
    step: (e) => { if (e.node !== "human") chat.line(`· ${e.text}`, "step"); },
    note: (e) => chat.line(e.text, "note"),
    state: () => {
      stateView.refresh();
      if (app.tab === "history") historyPanel.load(id);
      if (app.tab === "graph") loadGraph();
      if (app.detail) renderActions(app.detail);
      loadPapers();
      loadReviews();
    },
    error: (e) => chat.line(`! ${e.message}`, "error"),
    open: () => $("run-activity").classList.remove("lost"),
    lost: () => { $("run-activity").textContent = "reconnecting..."; $("run-activity").classList.add("lost"); },
  });
}

// The Paper button: shown once the run has a PDF, marked when a writer's.
async function loadPapers() {
  const id = app.runId;
  let papers = [];
  try { ({ papers } = await api.papers(id)); } catch { return; }
  if (id !== app.runId) return;
  const button = $("btn-paper");
  const fresh = papers.some((p) => p.paper) && !button.classList.contains("has-paper");
  button.hidden = !papers.length;
  button.classList.toggle("has-paper", papers.some((p) => p.paper));
  if (fresh && app.paperRun === id) chat.line("The paper is ready: press Paper at the top to read it.", "note");
  app.paperRun = id;
}

// The Review button: shown once the run has a review.
let reviewCount = -1;
async function loadReviews() {
  const id = app.runId;
  let st;
  try { ({ state: st } = await api.state(id)); } catch { return; }
  if (id !== app.runId) return;
  const n = (st.reviews || []).length;
  $("btn-review-open").hidden = !n;
  if (n > reviewCount && reviewCount >= 0 && app.reviewRun === id) {
    const v = st.reviews[n - 1];
    chat.line(`Review ${v.id} is in${v.decision ? `: ${v.decision}, ${v.rating}/10` : ""}. Press Review at the top to read it.`, "note");
  }
  reviewCount = n;
  app.reviewRun = id;
}

function openReview() {
  if (app.runId) window.open(`/review/${encodeURIComponent(app.runId)}`, `review-${app.runId}`, "width=1000,height=1100");
}

function openPaper() {
  if (app.runId) window.open(`/paper/${encodeURIComponent(app.runId)}`, `paper-${app.runId}`, "width=1000,height=1100");
}

function renderDetail(d) {
  $("run-name").textContent = d.id;
  $("run-name").title = d.folder;
  setPill($("run-stage"), d.stage);
  setPill($("run-phase"), d.phase);
  const activity = d.busy ? `working: ${d.current || "..."}` : d.waiting ? "waiting for you"
    : d.stopped_midway ? "stopped midway" : d.outcome || "";
  $("run-activity").textContent = activity;
  $("run-activity").className = `activity ${d.busy ? "busy" : d.waiting ? "waiting" : ""}`;
  $("btn-pause").hidden = !d.busy;
  $("btn-resume").hidden = !(d.stopped_midway && !d.busy);
  $("btn-execute").hidden = !d.can_execute;
  const finished = d.stage === "execution" && d.outcome === "done" && d.waiting && !d.busy;
  $("btn-review").hidden = !finished;
  $("btn-write-paper").hidden = !finished;
  $("btn-redesign").hidden = !(d.stage === "execution" && !d.busy && (d.waiting || d.outcome));
  $("btn-back-design").hidden = !(d.stage === "execution" && !d.busy);
  $("btn-stop").hidden = !(d.stage === "execution" && (d.busy || d.waiting) && d.outcome !== "done");
  chat.setMode(d);
  stateView.setControls({ stage: d.stage, canApprove: !!(d.waiting && !d.busy), awaiting: d.awaiting || "",
    canExecute: !!d.can_execute });
  $("btn-execute").classList.toggle("pulse", !!d.can_execute);
  const ap = d.autopilot || {};
  $("autopilot-toggle").hidden = false;
  $("autopilot").checked = !!ap.on;
  $("autopilot-state").textContent = !ap.on ? "" : ap.handed_back ? "(handed back to you)"
    : `(${ap.streak || 0}/${ap.max_decisions} in a row)`;
  $("autopilot-toggle").classList.toggle("on", !!ap.on);
  inspector.showFacts(d);
  renderActions(d);
}

// Buttons over the composer: what you can do now besides talking.
function renderActions(d) {
  const buttons = [];
  const st = stateView.data;
  if (d.stage === "bootstrap" && d.waiting && !d.busy && st) {
    const pending = st.approvable.filter((s) => (st.state.versions || {})[s] && !st.approved[s]);
    for (const s of pending) buttons.push({ label: `Approve ${s}`, kind: "primary", onclick: () => act(() => api.approve(d.id, [s])) });
    if (pending.length > 1) buttons.push({ label: "Approve all", onclick: () => act(() => api.approve(d.id, pending)) });
  }
  if (d.awaiting && d.waiting && !d.busy) {
    const what = d.awaiting.startsWith("after:") ? `Go on after step ${d.awaiting.slice(6)}` : `Go: step ${d.awaiting}`;
    buttons.push({ label: what, kind: "primary", onclick: () => act(() => api.go(d.id)) });
  }
  if (d.stopped_midway && !d.busy) buttons.push({ label: "Carry on", onclick: () => act(() => api.resume(d.id)) });
  if (d.held_idea && !d.started && !d.busy) {
    buttons.push({ label: "Start the run", kind: "primary", title: d.held_idea, onclick: () => act(() => api.start(d.id)) });
    buttons.push({ label: "Models and backups", onclick: () => document.querySelector('#inspector-tabs .tab[data-tab="run"]').click() });
  }
  if (d.can_execute) buttons.push({ label: "Start execution", kind: "primary", onclick: () => $("dialog-execute").showModal() });
  chat.setActions(buttons);
}

function setPill(node, text) {
  node.textContent = text || "-";
  node.className = `pill pill-${text || "none"}`;
}

async function act(fn) {
  try { await fn(); } catch (err) { problem(err); }
}

async function send(text) {
  if (!app.runId) {
    const d = await api.newRun({ idea: text }).catch((err) => { problem(err); return null; });
    if (d) await openRun(d.id);
    return;
  }
  await api.message(app.runId, text).catch(problem);
}

// ---- tabs, panels, dialogs ----------------------------------------------------------------

function loadTab(tab) {
  if (!app.runId) return;
  if (tab === "graph") loadGraph();
  if (tab === "files") files.load(app.runId);
  if (tab === "history") historyPanel.load(app.runId);
}

// The stage's graph. In the bootstrap, once there is a design, the execution
// graph it builds can be shown too -- first, once the design is approved.
async function loadGraph() {
  try {
    const d = app.detail || {};
    const view = app.graphView ?? (d.stage === "bootstrap" && d.can_execute ? "execution" : "");
    const g = await api.graph(app.runId, view);
    graph.render(g);
    const bar = $("graph-views");
    bar.hidden = !(d.stage === "bootstrap" && g.can_preview);
    bar.querySelectorAll("button").forEach((b) => b.classList.toggle("primary", b.dataset.view === (g.preview ? "execution" : "")));
  } catch (err) { problem(err); }
}

function wireTabs(navId, onShow) {
  const nav = $(navId);
  nav.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    nav.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const panel = nav.parentElement;
    panel.querySelectorAll(":scope > .tab-body").forEach((b) => b.classList.toggle("active", b.id === `tab-${tab.dataset.tab}`));
    onShow(tab.dataset.tab);
  });
}

function wireResize() {
  for (const divider of document.querySelectorAll(".divider")) {
    divider.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const which = divider.dataset.resize;
      const move = (ev) => {
        const width = which === "chat" ? ev.clientX : window.innerWidth - ev.clientX;
        document.documentElement.style.setProperty(which === "chat" ? "--col-chat" : "--col-inspector", `${Math.max(240, width)}px`);
        graph.resize();
      };
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }
}

async function showOpen() {
  const list = clear($("run-list"));
  try {
    const runs = await api.runs();
    if (!runs.length) list.append(el("div", { class: "empty", text: "No runs yet: start a new one." }));
    for (const r of runs) {
      list.append(el("a", { href: "#", class: "run-row", onclick: (e) => { e.preventDefault(); $("dialog-open").close(); openRun(r.id); } },
        el("div", { class: "run-row-head" }, el("b", { class: "mono", text: r.id }),
          r.busy ? el("span", { class: "pill pill-live", text: "working" }) : null,
          el("button", { class: "btn tiny danger run-delete", type: "button", text: "Delete",
            title: "Move this run to the .trash folder of the runs root", disabled: r.busy,
            onclick: (e) => { e.preventDefault(); e.stopPropagation(); deleteRun(r.id); } })),
        el("div", { class: "small", text: r.goal || "(no goal yet)" }),
        el("div", { class: "dim small", text: `${r.stage} · ${r.phase} · ${when(r.updated)}` })));
    }
  } catch (err) { problem(err); }
  if (!$("dialog-open").open) $("dialog-open").showModal();
}

async function deleteRun(id) {
  if (!confirm(`Delete the run ${id}?\n\nIts folder moves to the .trash folder of the runs root; move it back to restore it.`)) return;
  try {
    await api.trash(id);
    if (app.runId === id) { window.history.replaceState(null, "", location.pathname + location.search); location.reload(); return; }
    await showOpen();
  } catch (err) { problem(err); }
}

async function showNew() {
  $("new-error").textContent = "";
  const from = clear($("new-from"));
  from.append(el("option", { value: "", text: "-- a new idea --" }));
  try {
    for (const r of await api.runs()) from.append(el("option", { value: r.id, text: `${r.id} (${r.phase})` }));
  } catch { /* the list is optional */ }
  try {
    const f = await api.fields();
    clear($("new-field")).append(...f.fields.map((x) => el("option", { value: x.name, text: x.title,
      selected: x.name === f.default })));
  } catch { /* the server's default */ }
  $("dialog-new").showModal();
}

function wireDialogs() {
  $("btn-open").addEventListener("click", showOpen);
  $("open-cancel").addEventListener("click", () => $("dialog-open").close());
  $("btn-new").addEventListener("click", showNew);
  $("new-cancel").addEventListener("click", () => $("dialog-new").close());
  $("form-new").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { idea: $("new-idea").value.trim(), start_from: $("new-from").value, name: $("new-name").value.trim(),
      field: $("new-field").value || "", start: !$("new-hold").checked };
    if (!body.idea && !body.start_from) { $("new-error").textContent = "Give your idea, or a run to start from."; return; }
    try {
      const d = await api.newRun(body);
      $("dialog-new").close();
      $("new-idea").value = "";
      await openRun(d.id);
    } catch (err) { $("new-error").textContent = err.message; }
  });
  $("btn-execute").addEventListener("click", () => { $("exec-error").textContent = ""; $("dialog-execute").showModal(); });
  $("exec-cancel").addEventListener("click", () => $("dialog-execute").close());
  $("form-execute").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api.execute(app.runId, { wall_hours: Number($("exec-wall").value) || 0,
        gpu_hours: Number($("exec-gpu").value) || 0, confirm: $("exec-confirm").value });
      $("dialog-execute").close();
      await openRun(app.runId);
    } catch (err) { $("exec-error").textContent = err.message; }
  });
  $("btn-pause").addEventListener("click", () => act(() => api.pause(app.runId)));
  $("btn-resume").addEventListener("click", () => act(() => api.resume(app.runId)));
  $("btn-stop").addEventListener("click", () => {
    if (confirm("Stop the execution? Running jobs are killed.")) act(() => api.stop(app.runId));
  });
  $("btn-paper").addEventListener("click", openPaper);
  $("btn-review-open").addEventListener("click", openReview);
  $("btn-review").addEventListener("click", () => act(() => api.review(app.runId)));
  $("btn-write-paper").addEventListener("click", () => act(() => api.writePaper(app.runId)));
  $("btn-back-design").addEventListener("click", async () => {
    if (!confirm("Go back to the design stage in this run? Running jobs are stopped, the steps' outputs are moved aside (kept), and the designer designs again from the approved brief.")) return;
    try { await api.backToDesign(app.runId); await openRun(app.runId); } catch (err) { problem(err); }
  });
  $("btn-redesign").addEventListener("click", async () => {
    if (!confirm("Start a follow-up run from this run's paper and review? This run stays as it is; the new one starts at the design stage.")) return;
    try { const d = await api.redesign(app.runId); await openRun(d.id); } catch (err) { problem(err); }
  });
  $("btn-popout").addEventListener("click", () => {
    if (app.runId) window.open(`/state/${encodeURIComponent(app.runId)}`, `state-${app.runId}`, "width=980,height=1000");
  });
}

wireTabs("canvas-tabs", (tab) => { app.tab = tab; loadTab(tab); if (tab === "graph") setTimeout(() => graph.resize(), 0); });
wireTabs("inspector-tabs", (tab) => {
  app.side = tab;
  clearInterval(app.jobsTimer);
  if (tab === "calls") inspector.loadSessions();
  if (tab === "jobs") { inspector.loadJobs(); app.jobsTimer = setInterval(() => inspector.loadJobs(), JOBS_POLL_MS); }
});
wireResize();
wireDialogs();
$("graph-views").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) { app.graphView = b.dataset.view; loadGraph(); }
});
chat.setMode(null);
window.addEventListener("resize", () => graph.resize());

// /?tab=graph&side=jobs#<run>: open that run, with those tabs showing.
const params = new URLSearchParams(location.search);
for (const [nav, name] of [["canvas-tabs", params.get("tab")], ["inspector-tabs", params.get("side")]]) {
  const tab = name && document.querySelector(`#${nav} .tab[data-tab="${name}"]`);
  if (tab) tab.click();
}
const start = decodeURIComponent(location.hash.slice(1));
if (start) openRun(start); else showOpen();

// ---- autopilot -----------------------------------------------------------------------

$("autopilot").addEventListener("change", async (e) => {
  if (!app.runId) return;
  if (!e.target.checked) {
    try { await api.setAutopilot(app.runId, { on: false }); } catch (err) { problem(err); e.target.checked = true; }
    return;
  }
  e.target.checked = false;                 // on only once the limits are confirmed
  $("ap-error").textContent = "";
  try {
    const now = await api.autopilot(app.runId);
    $("ap-max").value = now.max_decisions || 20;
    if (now.wall_hours > 0) $("ap-wall").value = now.wall_hours;
    if (now.gpu_hours > 0) $("ap-gpu").value = now.gpu_hours;
  } catch (err) { problem(err); return; }
  $("dialog-autopilot").showModal();
});

$("ap-cancel").addEventListener("click", () => $("dialog-autopilot").close());

$("form-autopilot").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api.setAutopilot(app.runId, { on: true, max_decisions: Number($("ap-max").value),
      wall_hours: Number($("ap-wall").value), gpu_hours: Number($("ap-gpu").value) });
    $("autopilot").checked = true;
    $("dialog-autopilot").close();
  } catch (err) { $("ap-error").textContent = err.message; }
});
