// The stage's graph, drawn with cytoscape + dagre: who works now, and in the
// execution how each step owner's steps stand.

import { el, clear } from "./dom.js";

const KIND = {
  human: "#d99b4a", discussor: "#e0a3d0", orchestrator: "#c48ae0", worker: "#35d6c4",
  runner: "#4fb0e8", writer: "#9ad66b", validator: "#d9b84a", reviewer: "#e56b6b", code: "#7a8aa6",
};
const STATUS = { pass: "#1e3a2a", fail: "#3a1e22", stopped: "#3a1e22", running: "#173640",
  waiting: "#173640", validating: "#2f2c17", pending: "#18202f" };

const STYLE = [
  { selector: "node", style: {
    shape: "round-rectangle", width: "label", height: "label", padding: "10px",
    "background-color": (n) => STATUS[n.data("status")] || "#18202f",
    "border-width": 2, "border-color": (n) => KIND[n.data("kind")] || "#7a8aa6",
    label: "data(text)", color: "#dce3ee", "font-size": 12, "font-family": "ui-monospace, Menlo, monospace",
    "text-wrap": "wrap", "text-valign": "center", "text-halign": "center",
  } },
  { selector: "node:active", style: { "overlay-opacity": 0.12, "overlay-color": "#35d6c4" } },
  { selector: "node.active", style: { "border-width": 4, "border-color": "#35d6c4", "background-color": "#123a37" } },
  { selector: "edge", style: {
    width: 1.4, "curve-style": "bezier", "line-color": "#4a5a78", "target-arrow-color": "#4a5a78",
    "target-arrow-shape": "triangle", label: "data(label)", "font-size": 10, color: "#8b98ae",
    "text-background-color": "#0b0f17", "text-background-opacity": 0.85, "text-background-padding": 2,
  } },
];

export class GraphPanel {
  constructor(container, legend, onSelect) {
    this.onSelect = onSelect;      // a node was clicked: its id
    this.container = container;
    this.legend = legend;
    this.cy = null;
    this.active = null;
    this.view = null;
  }

  render(view) {
    this.view = view;
    // Many step owners side by side make a graph too wide to read: lay a big
    // one out left to right, so they stack, and keep only the telling labels.
    const big = view.nodes.length > 9;
    const elements = [
      ...view.nodes.map((n) => ({ data: { id: n.id, kind: n.kind, status: n.status || "",
        text: n.detail ? `${n.label}\n${n.detail}${n.status ? " · " + n.status : ""}` : n.label } })),
      ...view.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.source, target: e.target,
        label: big && !["request / go", "ask", "done", "review"].includes(e.label) && !/^run /.test(e.label || "") ? "" : big && /^run /.test(e.label || "") ? "" : e.label || "" } })),
    ];
    if (!window.cytoscape) {
      clear(this.container).append(el("div", { class: "empty", text: "The graph library did not load." }));
      return;
    }
    if (this.cy) this.cy.destroy();
    this.cy = window.cytoscape({ container: this.container, elements, style: STYLE,
      layout: { name: "dagre", rankDir: big ? "LR" : "TB", nodeSep: big ? 14 : 40, rankSep: big ? 90 : 55, fit: true, padding: 24 },
      autoungrabify: true, wheelSensitivity: 0.2 });
    if (this.active) this.highlight(this.active);
    this.cy.on("tap", "node", (e) => this.onSelect && this.onSelect(e.target.id(), e.target.data("kind")));
    clear(this.legend).append(...Object.entries(KIND)
      .filter(([k]) => view.nodes.some((n) => n.kind === k))
      .map(([k, c]) => el("span", { class: "swatch" }, el("i", { style: `border-color:${c}` }), k)));
  }

  // The node a model call or a step belongs to.
  highlight(name) {
    this.active = name;
    if (!this.cy) return;
    this.cy.nodes().removeClass("active");
    const node = name && this.cy.getElementById(name);
    if (node && node.length) node.addClass("active");
  }

  resize() {
    if (this.cy) { this.cy.resize(); this.cy.fit(undefined, 24); }
  }
}

// "researcher", "planner hypothesize", "step 5 (trainer)", "validate step 5",
// "discussor (aside)", "reviewer" -> the graph node.
export function nodeOfLabel(label) {
  const text = String(label || "");
  const owner = text.match(/^step \S+ \((.+)\)$/);
  if (owner) return owner[1];
  if (text.startsWith("validate")) return "validator";
  return text.split(/\s+/)[0];
}
