// The research state in its own window (/state/<run>): the same view as the
// main page's tab, kept live by the run's event stream.

import { api, subscribe } from "./api.js";
import { StateView } from "./state.js";

const $ = (id) => document.getElementById(id);
const runId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");

const view = new StateView($("state-view"), {
  onError: (err) => { $("run-activity").textContent = `! ${err.message || err}`; },
  onSession: () => window.open(`/#${encodeURIComponent(runId)}`, "_blank"),
});

function show(d) {
  $("run-name").textContent = runId;
  $("run-phase").textContent = d.phase || "-";
  $("run-phase").className = `pill pill-${d.phase || "none"}`;
  $("run-activity").textContent = d.busy ? `working: ${d.current || "..."}` : d.waiting ? "waiting for the user" : d.outcome || "";
  view.setControls({ stage: d.stage, canApprove: !!(d.waiting && !d.busy), awaiting: d.awaiting || "",
    canExecute: !!d.can_execute });
  document.title = `Research State — ${runId}`;
}

async function main() {
  view.setRun(runId);
  let d;
  try { d = await api.run(runId); } catch (err) { $("run-activity").textContent = `! ${err.message}`; return; }
  show(d);
  await view.load();
  let detail = d;
  subscribe(runId, d.last_seq, {
    status: (e) => { detail = { ...detail, ...e }; show(detail); },
    state: () => view.refresh(),
    open: () => $("live-dot").classList.add("on"),
    lost: () => $("live-dot").classList.remove("on"),
  });
}

main();
