// A background job in its own window (/job/<run>/<job>): what it runs and
// why, its GPUs now, its script, and its log as it is written.

import { api } from "./api.js";
import { el, clear, pill } from "./dom.js";

const $ = (id) => document.getElementById(id);
const [runId, jobId] = location.pathname.split("/").filter(Boolean).slice(-2).map(decodeURIComponent);
const KEEP = 400_000;           // characters of the log the page keeps
const STREAM_MS = 1500;
const FACTS_MS = 5000;
let raw = "";
let offset = 0;
let running = true;
let scriptShown = "";

// A progress bar rewrites its line with \r: show each line as it ends up.
function shown(text) {
  return text.split("\n").map((line) => line.slice(line.lastIndexOf("\r") + 1)).join("\n");
}

function hours(h) {
  return h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(2)} h`;
}

async function facts() {
  let j;
  try { j = await api.job(runId, jobId); } catch (err) { $("job-info").textContent = `! ${err.message}`; return; }
  running = j.state === "running";
  $("job-name").textContent = `${j.id}${j.name ? ` · ${j.name}` : ""}`;
  const s = $("job-state");
  s.replaceWith(Object.assign(pill(j.state + (j.exit != null ? ` (exit ${j.exit})` : ""),
    j.state === "running" ? "live" : j.exit === 0 ? "ok" : "bad"), { id: "job-state" }));
  $("job-info").textContent = [`step ${j.step}`, hours(j.hours), j.gpus.length ? `GPUs ${j.gpus.join(",")}` : "no GPU",
    j.gpu_hours ? `${j.gpu_hours.toFixed(2)} GPU-h` : "", `started ${(j.started_at || "").slice(0, 19).replace("T", " ")}`]
    .filter(Boolean).join(" · ");
  clear($("job-for")).append(el("b", { text: `Step ${j.step}: ${j.step_title}` }),
    j.owner ? el("span", { class: "dim", text: ` (${j.owner})` }) : null,
    j.step_summary ? el("div", { class: "dim", text: `The owner said: ${j.step_summary}` }) : null);
  $("job-command").textContent = `$ cd ${j.cwd}\n$ ${j.command}`;
  clear($("job-gpus")).append(...(j.gpus_now || []).map((g) => el("span", { class: "gpu-chip" },
    `GPU ${g.index}: ${g.util}% busy, ${(g.used / 1024).toFixed(1)} / ${(g.total / 1024).toFixed(0)} GB`)));
  if (j.script && j.script !== scriptShown) {
    scriptShown = j.script;
    $("job-script-box").hidden = false;
    $("job-script-title").textContent = `The script: ${j.script}`;
    if (j.script_in_run) {
      try {
        const f = await api.file(runId, j.script);
        $("job-script").textContent = f.text ?? f.skipped;
      } catch (err) { $("job-script").textContent = err.message; }
    } else {
      $("job-script").textContent = "(outside the run folder: not shown here)";
    }
  }
  document.title = `Job ${j.id} — ${j.state}`;
}

async function stream() {
  try {
    const r = await api.jobStream(runId, jobId, offset);
    if (r.restart) raw = "";
    if (r.text) raw = (raw + r.text).slice(-KEEP);
    offset = r.offset;
    running = r.state === "running";
    const log = $("job-log");
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent = shown(raw) || "(nothing written yet)";
    if ($("follow").checked || atEnd) log.scrollTop = log.scrollHeight;
    $("live-dot").classList.toggle("on", running);
  } catch (err) { $("live-dot").classList.remove("on"); }
  setTimeout(stream, running ? STREAM_MS : 10_000);
}

async function main() {
  await facts();
  stream();
  let last = false;           // one more look once it has finished: its exit code
  setInterval(() => { if (running || !last) { last = !running; facts(); } }, FACTS_MS);
}

main();
