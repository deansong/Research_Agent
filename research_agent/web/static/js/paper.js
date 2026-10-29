// The run's paper in its own window (/paper/<run>): the PDFs the writers
// listed first, any other PDF of the run after; it follows the run live.

import { api, subscribe } from "./api.js";
import { el, clear, size, when } from "./dom.js";

const $ = (id) => document.getElementById(id);
const runId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let shown = "";
let papers = [];
let picked = "";        // a paper you chose stays; otherwise the newest is shown

function label(p) {
  return `${p.step ? `step ${p.step}: ` : ""}${p.path}`;
}

function show(path) {
  const p = papers.find((x) => x.path === path);
  if (!p) return;
  const src = `${api.raw(runId, p.path)}&v=${Math.floor(p.modified)}`;
  if (shown !== src) $("paper-frame").src = src;
  shown = src;
  $("paper-pick").value = p.path;
  $("paper-info").textContent = `${size(p.size)} · ${when(p.modified)}`;
  $("paper-tab").href = src;
}

async function load() {
  try { ({ papers } = await api.papers(runId)); } catch (err) { $("paper-info").textContent = `! ${err.message}`; return; }
  const none = !papers.length;
  $("paper-empty").hidden = !none;
  $("paper-frame").hidden = none;
  $("paper-pick").hidden = papers.length < 2;
  $("paper-tab").hidden = none;
  if (none) return;
  clear($("paper-pick")).append(...papers.map((p) => el("option", { value: p.path, text: label(p) })));
  show(papers.some((p) => p.path === picked) ? picked : papers[0].path);
}

$("paper-pick").addEventListener("change", (e) => { picked = e.target.value; show(picked); });
$("run-name").textContent = runId;
document.title = `Paper — ${runId}`;
load().then(async () => {
  let d;
  try { d = await api.run(runId); } catch { return; }
  subscribe(runId, d.last_seq, {
    state: () => load(),
    open: () => $("live-dot").classList.add("on"),
    lost: () => $("live-dot").classList.remove("on"),
  });
});
