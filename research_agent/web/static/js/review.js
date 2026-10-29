// The run's reviews in their own window (/review/<run>): the meta-review
// and each reviewer's form, as reviews/V<n>.md has them; the newest first.

import { api, subscribe } from "./api.js";
import { el, clear } from "./dom.js";
import { markdown } from "./md.js";

const $ = (id) => document.getElementById(id);
const runId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let reviews = [];
let picked = "";        // a review you chose stays; otherwise the newest is shown

async function show(id) {
  const v = reviews.find((x) => x.id === id);
  if (!v) return;
  $("review-pick").value = id;
  $("review-info").textContent = [v.decision, v.rating ? `${v.rating}/10` : "", v.at ? v.at.slice(0, 16).replace("T", " ") : ""]
    .filter(Boolean).join(" · ");
  try {
    const f = await api.file(runId, `reviews/${id}.md`);
    $("review-doc").innerHTML = markdown(f.text || "", { headings: true });   // escaped by markdown()
  } catch (err) { $("review-doc").textContent = `! ${err.message}`; }
}

async function load() {
  let st;
  try { ({ state: st } = await api.state(runId)); } catch (err) { $("review-info").textContent = `! ${err.message}`; return; }
  reviews = (st.reviews || []).slice().reverse();
  const none = !reviews.length;
  $("review-empty").hidden = !none;
  $("review-doc").hidden = none;
  $("review-pick").hidden = reviews.length < 2;
  $("open-paper").hidden = !(st.status || {}).paper;
  if (none) return;
  clear($("review-pick")).append(...reviews.map((v) => el("option", { value: v.id,
    text: `${v.id}${v.decision ? ` — ${v.decision}` : ""} (${v.rating}/10)` })));
  await show(reviews.some((v) => v.id === picked) ? picked : reviews[0].id);
}

$("review-pick").addEventListener("change", (e) => { picked = e.target.value; show(picked); });
$("open-paper").addEventListener("click", () =>
  window.open(`/paper/${encodeURIComponent(runId)}`, `paper-${runId}`, "width=1000,height=1100"));
$("run-name").textContent = runId;
document.title = `Review — ${runId}`;
load().then(async () => {
  let d;
  try { d = await api.run(runId); } catch { return; }
  subscribe(runId, d.last_seq, {
    state: () => load(),
    open: () => $("live-dot").classList.add("on"),
    lost: () => $("live-dot").classList.remove("on"),
  });
});
