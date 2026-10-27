// The research state, live: every section as the team last wrote it, what
// changed since you looked, approvals, and -- in execution -- the step table
// with breakpoints and the go. Used by the main page's tab and by its own
// window (state.html).

import { api } from "./api.js";
import { el, clear, pill, when } from "./dom.js";

const TITLES = {
  goal: "Goal", constraints: "Constraints", assumptions: "Assumptions", evidence: "Evidence",
  hypotheses: "Hypotheses", rubric: "Rubric", protocol: "Protocol", plan: "Plan",
  design: "Design", execution: "Execution", decisions: "Decisions",
  open_questions: "Open questions", risks: "Risks", reviews: "Reviews", results: "Work done",
};
const LONG = 220;   // characters before a text folds

export class StateView {
  constructor(container, hooks = {}) {
    this.root = container;
    this.hooks = hooks;           // onSession(id), onError(err)
    this.runId = null;
    this.data = null;
    this.seen = null;             // section versions when last rendered
    this.fresh = new Set();       // sections changed since the user looked
    this.controls = { stage: "bootstrap", canApprove: false, awaiting: "", canExecute: false };
    this.openKeys = new Set();    // <details> the user opened
    this.pending = null;
  }

  setRun(id) {
    this.runId = id;
    this.data = null;
    this.seen = null;
    this.fresh.clear();
    clear(this.root);
    if (!id) this.root.append(el("div", { class: "empty", text: "No run open." }));
  }

  setControls(controls) {
    const before = JSON.stringify(this.controls);
    this.controls = { ...this.controls, ...controls };
    if (this.data && JSON.stringify(this.controls) !== before) this.render();
  }

  // Fetch and draw; called on every `state` event (debounced).
  refresh() {
    if (!this.runId) return;
    clearTimeout(this.pending);
    this.pending = setTimeout(() => this.load(), 120);
  }

  async load() {
    const id = this.runId;
    try {
      const data = await api.state(id);
      if (id !== this.runId) return;
      const versions = data.state.versions || {};
      if (this.seen) {
        for (const [s, v] of Object.entries(versions)) if (v > (this.seen[s] || 0)) this.fresh.add(s);
      }
      this.seen = { ...versions };
      this.data = data;
      this.render();
    } catch (err) {
      if (this.hooks.onError) this.hooks.onError(err);
    }
  }

  // ---- drawing -----------------------------------------------------------------

  render() {
    const st = this.data.state;
    const main = this.root.querySelector(".sv-main");
    const scroll = main ? main.scrollTop : 0;
    this.root.querySelectorAll("details[data-key]").forEach((d) => {
      if (d.open) this.openKeys.add(d.dataset.key); else this.openKeys.delete(d.dataset.key);
    });
    clear(this.root);
    const sections = this.data.sections.filter((s) => s !== "status" && !isEmpty(st[s]));
    const nav = el("nav", { class: "sv-nav" },
      el("a", { class: "sv-link", href: "#", onclick: (e) => { e.preventDefault(); this.jump("status"); }, text: "Where things stand" }),
      sections.map((s) => el("a", {
        class: `sv-link ${this.fresh.has(s) ? "fresh" : ""}`, href: "#",
        onclick: (e) => { e.preventDefault(); this.jump(s); },
      }, el("span", { text: TITLES[s] || s }), el("span", { class: "sv-count", text: count(st[s]) }))));
    const body = el("div", { class: "sv-main" }, this.statusCard(st),
      sections.map((s) => this.section(s, st)));
    this.root.append(el("div", { class: "sv-layout" }, nav, body));
    body.scrollTop = scroll;
    this.root.querySelectorAll("details[data-key]").forEach((d) => {
      if (this.openKeys.has(d.dataset.key)) d.open = true;
    });
  }

  jump(section) {
    const target = this.root.querySelector(`#sv-${section}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    if (this.fresh.delete(section)) {
      const link = [...this.root.querySelectorAll(".sv-link")].find((a) => a.textContent.startsWith(TITLES[section]));
      if (link) link.classList.remove("fresh");
    }
  }

  statusCard(st) {
    const status = st.status || {};
    const approvals = this.data.approvable.map((s) => {
      const v = (st.versions || {})[s] || 0;
      const ok = this.data.approved[s];
      const chip = el("span", { class: `approval ${ok ? "ok" : v ? "pending" : "none"}` },
        el("b", { text: s }), ok ? " approved" : v ? ` v${v}, not approved` : " none yet");
      if (!ok && v && this.controls.stage === "bootstrap") {
        chip.append(el("button", {
          class: "btn tiny primary", text: "Approve", disabled: !this.controls.canApprove,
          title: this.controls.canApprove ? `Approve the ${s} as it stands: hypotheses, rubric, protocol, steps and nodes`
            : "Approvals go in when the team waits for you", onclick: () => this.approve([s]),
        }));
      }
      return chip;
    });
    return el("section", { class: "sv-card sv-status", id: "sv-status" },
      el("div", { class: "sv-head" }, el("h3", { text: "Where things stand" }),
        pill(status.phase || "discussing", status.phase)),
      status.summary ? el("p", { class: "sv-summary", text: status.summary }) : null,
      status.next ? el("p", { class: "dim" }, el("b", { text: "Next: " }), status.next) : null,
      el("div", { class: "approvals" }, approvals),
      this.controls.canExecute ? el("div", { class: "callout" },
        el("div", {}, el("b", { text: "The design is approved: the bootstrap is done." }),
          el("div", { class: "dim small", text: "The execution carries the plan out with the design's nodes. Steps the plan gates wait for your go." })),
        this.hooks.onExecute ? el("button", { class: "btn primary", text: "Start execution", onclick: () => this.hooks.onExecute() })
          : el("span", { class: "dim small", text: "Start it from the main page." })) : null);
  }

  section(name, st) {
    const v = (st.versions || {})[name] || 0;
    const approvable = (this.data.parts || this.data.approvable).includes(name);
    const badge = approvable ? (this.data.approved[name] ? pill("approved", "ok") : pill(`v${v} not approved`, "warn"))
      : el("span", { class: "dim small", text: `v${v}` });
    const draw = DRAW[name] || DRAW.json;
    return el("section", { class: `sv-card ${this.fresh.has(name) ? "fresh" : ""}`, id: `sv-${name}` },
      el("div", { class: "sv-head" }, el("h3", { text: TITLES[name] || name }), badge,
        this.fresh.has(name) ? el("span", { class: "new-dot", text: "updated" }) : null),
      draw.call(this, st[name], st));
  }

  async approve(sections) {
    try { await api.approve(this.runId, sections); } catch (err) { this.hooks.onError && this.hooks.onError(err); }
  }

  async toggleBreak(step, when, on) {
    try { await api.setBreak(this.runId, step, when, on); } catch (err) { this.hooks.onError && this.hooks.onError(err); }
  }

  async stepAction(step, action) {
    let why = "";
    if (action === "waive") {
      why = window.prompt(`Why does step ${step} pass anyway? (kept as its verdict)`) || "";
      if (!why.trim()) return;
    }
    try { await api.stepAction(this.runId, step, action, why); } catch (err) { this.hooks.onError && this.hooks.onError(err); }
  }

  async go() {
    try { await api.go(this.runId); } catch (err) { this.hooks.onError && this.hooks.onError(err); }
  }

  fold(key, title, text) {
    if (!text) return null;
    if (text.length <= LONG) return el("p", { class: "field" }, el("b", { text: `${title}: ` }), text);
    return el("details", { class: "fold", dataset: { key } },
      el("summary", {}, el("b", { text: `${title}: ` }), text.slice(0, LONG) + "..."),
      el("p", { class: "pre", text }));
  }
}

// ---- one renderer per section ---------------------------------------------------------

const DRAW = {
  goal(g) {
    return facts([["Question", g.question], ["Motivation", g.motivation],
      ["In scope", (g.scope_in || []).join("; ")], ["Out of scope", (g.scope_out || []).join("; ")],
      ["Deliverable", g.deliverable]]);
  },
  constraints(c) {
    return facts([["Compute", c.compute], ["Budget", c.budget], ["Deadline", c.deadline],
      ["Data", c.data], ["Models", c.models], ["Other", (c.other || []).join("; ")]]);
  },
  protocol(p) {
    return facts([["Baselines", (p.baselines || []).join("; ")], ["Metrics", (p.metrics || []).join("; ")],
      ["Statistics", p.statistics], ["Seeds", p.seeds], ["Stop criteria", p.stop_criteria]]);
  },
  assumptions(items) {
    return list(items, (a) => [idTag(a.id), pill(a.status, a.status === "checked" ? "ok" : a.status === "false" ? "bad" : "warn"), el("span", { text: a.text })]);
  },
  evidence(items) {
    return el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, ["", "Claim", "Source", "Bears on", "By"].map((h) => el("th", { text: h })))),
      el("tbody", {}, items.map((e) => el("tr", { id: `ev-${e.id}` },
        el("td", {}, idTag(e.id), el("span", {
          class: `mark ${e.verified === true ? "ok" : e.verified === false ? "bad" : "dim"}`,
          title: e.verified === true ? "source verified" : e.verified === false ? "source NOT FOUND" : "not checked",
          text: e.verified === true ? "✓" : e.verified === false ? "✗" : "?",
        })),
        el("td", { text: e.claim }), el("td", {}, source(e.source)),
        el("td", {}, (e.bears_on || []).map((b) => idTag(b))), el("td", { class: "dim", text: e.found_by || "" })))));
  },
  hypotheses(items) {
    return el("div", { class: "cards" }, items.map((h) => el("div", { class: `hcard ${h.status}` },
      el("div", { class: "sv-head" }, idTag(h.id), pill(h.status, statusKind(h.status)),
        el("b", { text: h.plain || h.statement })),
      h.plain ? this.fold(`h-${h.id}-c`, "Claim", h.statement) : null,
      this.fold(`h-${h.id}-p`, "Prediction", h.prediction),
      this.fold(`h-${h.id}-r`, "Why", h.rationale),
      this.fold(`h-${h.id}-n`, "Novelty", h.novelty),
      (h.evidence || []).length ? el("p", { class: "field" }, el("b", { text: "Evidence: " }),
        h.evidence.map((e) => el("a", {
          class: "id-link", href: "#", text: e,
          onclick: (ev) => { ev.preventDefault(); flash(this.root.querySelector(`#ev-${e}`)); },
        }))) : null)));
  },
  rubric(items) {
    return el("table", { class: "grid" },
      el("thead", {}, el("tr", {}, ["", "Criterion", "Measure", "Passes at", "Weight", "Applies to"].map((h) => el("th", { text: h })))),
      el("tbody", {}, items.map((r) => el("tr", {},
        el("td", {}, idTag(r.id)), el("td", { text: r.criterion }), el("td", { text: r.measure }),
        el("td", { text: r.threshold }), el("td", { text: String(r.weight ?? "") }),
        el("td", {}, (r.applies_to || []).map((a) => idTag(a)))))));
  },
  plan(p) {
    return el("div", {}, p.summary ? el("p", { text: p.summary }) : null,
      el("ol", { class: "steps" }, (p.steps || []).map((s) => el("li", {},
        el("div", { class: "sv-head" }, el("b", { text: `${s.id}. ${s.title}` }),
          s.gate ? pill("gate", "warn") : null,
          (s.depends_on || []).length ? el("span", { class: "dim small", text: `after ${s.depends_on.join(", ")}` }) : null,
          (s.rubric || []).map((r) => idTag(r))),
        this.fold(`p-${s.id}-d`, "What", s.detail), this.fold(`p-${s.id}-c`, "Check", s.check),
        this.fold(`p-${s.id}-$`, "Cost", s.cost)))));
  },
  design(d) {
    return el("div", {}, d.summary ? el("p", { text: d.summary }) : null,
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {}, ["Node", "Role", "Steps", "Skills", "Access"].map((h) => el("th", { text: h })))),
        el("tbody", {}, (d.nodes || []).map((n) => [
          el("tr", {}, el("td", { class: "mono", text: n.name }), el("td", {}, pill(n.role, n.role)),
            el("td", { text: (n.steps || []).join(", ") || "-" }),
            el("td", { class: "small", text: (n.skills || []).join(", ") || "-" }), el("td", { text: n.access })),
          n.instructions ? el("tr", { class: "sub" }, el("td", { colspan: "5" },
            this.fold(`d-${n.name}`, "Instructions", n.instructions))) : null]))),
      this.fold("d-notes", "Notes", d.notes));
  },
  execution(e, st) {
    const steps = e.steps || [];
    const live = this.controls.stage === "execution";
    const awaiting = this.controls.awaiting || "";
    const limits = [e.wall_hours ? `${e.wall_hours} wall hours` : "", e.gpu_hours ? `${e.gpu_hours} GPU-hours` : ""].filter(Boolean);
    return el("div", {},
      el("p", { class: "dim" }, `Started ${when(e.started) || "-"} · limits: ${limits.join(", ") || "none"}`),
      el("table", { class: "grid exec" },
        el("thead", {}, el("tr", {}, ["Step", "Owner", "Status", "Tries", "GPU-h", "Stop before / after", ""].map((h) => el("th", { text: h })))),
        el("tbody", {}, steps.map((r) => {
          const waits = awaiting === r.id || awaiting === `after:${r.id}`;
          return [
            el("tr", { class: waits ? "awaiting" : "" },
              el("td", {}, el("b", { text: `${r.id}. ` }), r.title),
              el("td", { class: "mono small", text: r.owner }),
              el("td", {}, pill(r.status, statusKind(r.status)), r.go ? el("span", { class: "dim small", text: " go given" }) : null),
              el("td", { text: String(r.attempts || 0) }),
              el("td", { text: (r.gpu_hours || 0).toFixed(1) }),
              el("td", {}, ["before", "after"].map((w) => el("label", { class: "brk", title: `Stop for you ${w} step ${r.id}` },
                el("input", { type: "checkbox", checked: !!r[`pause_${w}`], disabled: !live,
                  onchange: (ev) => this.toggleBreak(r.id, w, ev.target.checked) }), w))),
              el("td", {}, waits ? el("button", { class: "btn tiny primary", text: "Go", onclick: () => this.go() }) : null,
                live && ["fail", "stopped"].includes(r.status) ? [
                  el("button", { class: "btn tiny", text: "Another attempt", title: `Run step ${r.id} again, beyond the attempt limit`,
                    onclick: () => this.stepAction(r.id, "retry") }),
                  el("button", { class: "btn tiny", text: "Pass anyway", title: "Pass this step on your decision (your reason is kept as its verdict)",
                    onclick: () => this.stepAction(r.id, "waive") })] : null)),
            (r.verdict || r.summary || (r.problems || []).length) ? el("tr", { class: "sub" }, el("td", { colspan: "7" },
              this.fold(`x-${r.id}-v`, "Verdict", r.verdict), this.fold(`x-${r.id}-s`, "Report", r.summary),
              (r.problems || []).length ? this.fold(`x-${r.id}-p`, "Problems", r.problems.join("\n")) : null,
              (r.artifacts || []).length ? el("p", { class: "field small" }, el("b", { text: "Files: " }), r.artifacts.join(", ")) : null)) : null];
        }))));
  },
  decisions(items) {
    return list(items.slice().reverse(), (d) => [idTag(d.id), d.by ? pill(d.by, d.by === "user" ? "human" : "") : null,
      el("span", {}, el("b", { text: d.what }), d.why ? el("span", { class: "dim", text: ` — ${d.why}` }) : null)]);
  },
  open_questions(items) {
    return list(items, (q) => [idTag(q.id), pill(q.for || "user", q.for === "user" ? "human" : ""),
      pill(q.status, q.status === "answered" ? "ok" : "warn"),
      el("span", {}, q.text, q.answer ? el("div", { class: "dim small", text: `↳ ${q.answer}` }) : null)]);
  },
  risks(items) {
    return list(items, (r) => [idTag(r.id), el("span", {}, r.text,
      r.mitigation ? el("div", { class: "dim small", text: `mitigation: ${r.mitigation}` }) : null)]);
  },
  reviews(items) {
    return el("div", { class: "cards" }, items.slice().reverse().map((v) => el("div", { class: "review" },
      el("div", { class: "review-scores" },
        el("div", { class: "score big" }, el("span", { text: `${v.rating}` }), el("small", { text: "/10 rating" })),
        [["confidence", v.confidence, 5], ["soundness", v.soundness, 4], ["presentation", v.presentation, 4],
          ["contribution", v.contribution, 4]].map(([k, x, m]) => el("div", { class: "score" },
          el("span", { text: `${x}` }), el("small", { text: `/${m} ${k}` })))),
      el("div", { class: "sv-head" }, idTag(v.id), el("span", { class: "dim small", text: `of ${v.run || "this run"} · ${when(v.at)}` })),
      el("p", { text: v.summary }),
      el("h4", { text: "Requests for the next run" }),
      el("ul", { class: "requests" }, (v.requests || []).map((r) => el("li", {}, pill(r.priority, r.priority === "must" ? "bad" : r.priority === "should" ? "warn" : ""),
        el("b", { text: ` ${r.what}` }), r.why ? el("div", { class: "dim small", text: r.why }) : null))),
      [["strengths", "Strengths"], ["weaknesses", "Weaknesses"], ["questions", "Questions"]].map(([k, t]) =>
        (v[k] || []).length ? el("details", { class: "fold", dataset: { key: `v-${v.id}-${k}` } },
          el("summary", { text: `${t} (${v[k].length})` }), el("ul", {}, v[k].map((x) => el("li", { text: x })))) : null),
      this.fold(`v-${v.id}-l`, "Limitations", v.limitations))));
  },
  results(items) {
    return list(items.slice().reverse().slice(0, 80), (r) => [idTag(r.id),
      el("span", { class: "mono small", text: `${r.node}${r.job ? "/" + r.job : ""}` }),
      el("span", { class: "grow", text: r.summary }),
      r.session ? el("button", { class: "btn tiny", text: "call", title: `Open session ${r.session}`,
        onclick: () => this.hooks.onSession && this.hooks.onSession(r.session) }) : null]);
  },
  json(value) {
    return el("pre", { class: "pre small", text: JSON.stringify(value, null, 2) });
  },
};

// ---- helpers -----------------------------------------------------------------------------

function isEmpty(value) {
  if (value == null) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") {
    if ("steps" in value && "started" in value) return !(value.steps || []).length;
    if ("nodes" in value) return !(value.nodes || []).length && !value.summary;
    if ("steps" in value) return !(value.steps || []).length && !value.summary;
    return Object.values(value).every((v) => isEmpty(v) || v === "" || v === 0);
  }
  return value === "";
}

function count(value) {
  if (Array.isArray(value)) return String(value.length);
  if (value && Array.isArray(value.steps)) return String(value.steps.length);
  if (value && Array.isArray(value.nodes)) return String(value.nodes.length);
  return "";
}

function facts(rows) {
  return el("dl", { class: "facts" }, rows.filter(([, v]) => v).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
}

function list(items, row) {
  return el("ul", { class: "rows" }, items.map((x) => el("li", {}, row(x))));
}

function idTag(id) {
  return el("span", { class: "id", text: id });
}

function source(text) {
  const arxiv = String(text).match(/^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$/i);
  const url = arxiv ? `https://arxiv.org/abs/${arxiv[1]}` : /^https?:\/\//.test(text) ? text : null;
  return url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { text });
}

function statusKind(status) {
  return { pass: "ok", supported: "ok", selected: "ok", skipped: "dim", fail: "bad", refuted: "bad",
    stopped: "bad", dropped: "dim", running: "live", waiting: "live", validating: "live",
    proposed: "warn", pending: "" }[status] || "";
}

function flash(node) {
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.remove("flash");
  void node.offsetWidth;
  node.classList.add("flash");
}
