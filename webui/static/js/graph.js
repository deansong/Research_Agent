/*
  WHAT:  Draws the agent's topology, and lets you click it.
  WHY:   `/graph` in the terminal prints mermaid text. Readable; not clickable,
         and not something you can rewire.
  CONCEPT: Cytoscape with a dagre (layered) layout.

  --------------------------------------------------------------------------
  THE VIEW IS DERIVED, NEVER STORED
  --------------------------------------------------------------------------
  The agent folder is the single source of truth. Cytoscape's elements are
  built from `/api/.../agent`'s `view` every time it changes, and nothing is
  read back out of cytoscape except which node you clicked. Keeping a second
  copy of the graph in the drawing library is how a diagram and the thing it
  claims to describe drift apart.

  --------------------------------------------------------------------------
  THREE KINDS OF EDGE, AND WHY THE DISTINCTION MATTERS
  --------------------------------------------------------------------------
    edge     an unconditional transition, from graph.json's `edges`
    case     one arm of a branch, labelled with the value that selects it
    command  a slash command on a human node

  The last one is the interesting one. Human nodes have no outgoing edges in
  graph.json at all -- their exits are commands, which live in nodes.json. A
  picture drawn from graph.json alone shows every human node as a dead end.
  (validate.py's reachability walk reads both files for exactly this reason.)
*/

const KIND_COLOUR = {
  agent: '#35d6c4',
  human: '#d99b4a',
  end: '#5d6a80',
};

const EDGE_COLOUR = {
  edge: '#4a5a78',
  case: '#7a6bc4',
  command: '#d99b4a',
};

export class GraphPanel {
  constructor(container, { onSelect }) {
    this.container = container;
    this.onSelect = onSelect;
    this.cy = null;
    this.view = null;
    this.highlighted = new Set();
  }

  /** Replace the drawing with a fresh one built from `view`. */
  render(view) {
    this.view = view;
    this.container.replaceChildren();

    if (!view || !view.nodes?.length) {
      this.container.innerHTML =
        '<div class="empty-state"><p class="muted">No agent yet.</p></div>';
      return;
    }

    const canvas = document.createElement('div');
    canvas.className = 'cy-canvas';
    this.container.append(canvas, legend());

    this.cy = cytoscape({
      container: canvas,
      elements: [...view.nodes.map(toNode), ...view.edges.map(toEdge)],
      style: STYLE,
      layout: LAYOUT,
      // Panning and box-select are useful; dragging a node is not. Positions
      // are not stored anywhere -- the layout is recomputed from the topology
      // every time -- so a drag would be silently undone on the next render.
      autoungrabify: true,
      wheelSensitivity: 0.2,
    });

    this.cy.on('tap', 'node', (event) => {
      const id = event.target.id();
      if (id === '__end__') return;
      this.select(id);
      this.onSelect?.(id);
    });

    // Tapping the background clears the selection, which is what everyone
    // expects and what nothing does by default.
    this.cy.on('tap', (event) => {
      if (event.target === this.cy) {
        this.select(null);
        this.onSelect?.(null);
      }
    });
  }

  select(name) {
    if (!this.cy) return;
    this.cy.nodes().removeClass('selected');
    if (name) this.cy.getElementById(name).addClass('selected');
  }

  /**
   * Dim everything except these nodes.
   *
   * Used by the plan panel: click a step, and the nodes responsible for it
   * stay lit. That link -- plan step to node -- is the visible form of the
   * whole "each node sees only the steps it owns" idea, so it is worth more
   * than it costs.
   */
  highlight(names) {
    if (!this.cy) return;
    this.highlighted = new Set(names || []);
    if (!this.highlighted.size) {
      this.cy.elements().removeClass('dimmed owns');
      return;
    }
    this.cy.elements().addClass('dimmed');
    for (const name of this.highlighted) {
      this.cy.getElementById(name).removeClass('dimmed').addClass('owns');
    }
  }

  /** Re-fit after a column is dragged; cytoscape cannot notice on its own. */
  resize() {
    if (!this.cy) return;
    this.cy.resize();
    this.cy.fit(undefined, 30);
  }
}

function toNode(node) {
  const label = node.kind === 'end' ? 'END' : node.id;
  return {
    data: {
      id: node.id,
      label,
      kind: node.kind,
      sub: subtitle(node),
      warn: node.unknown_steps?.length ? '!' : '',
    },
    classes: [node.kind, node.entry ? 'entry' : '', node.unknown_steps?.length ? 'warn' : '']
      .filter(Boolean).join(' '),
  };
}

function subtitle(node) {
  if (node.kind === 'human') {
    return node.commands?.length ? node.commands.map((c) => `/${c}`).join(' ') : 'human';
  }
  if (node.kind === 'end') return '';
  const bits = [node.backend];
  if (node.access === 'write') bits.push('WRITE');
  if (node.steps?.length) bits.push(`steps ${node.steps.join(',')}`);
  return bits.filter(Boolean).join(' · ');
}

function toEdge(edge, index) {
  return {
    data: {
      id: `e${index}-${edge.from}-${edge.to}-${edge.kind}`,
      source: edge.from,
      target: edge.to,
      label: edge.label || '',
      kind: edge.kind,
    },
    classes: [edge.kind, edge.asks ? 'asks' : ''].filter(Boolean).join(' '),
  };
}

const STYLE = [
  {
    selector: 'node',
    style: {
      'background-color': '#18202f',
      'border-width': 2,
      'border-color': (n) => KIND_COLOUR[n.data('kind')] || '#4a5a78',
      shape: 'round-rectangle',
      width: 'label',
      height: 'label',
      padding: '10px',
      label: 'data(label)',
      color: '#dce3ee',
      'font-family': 'ui-monospace, Menlo, monospace',
      'font-size': 12,
      'text-valign': 'center',
      'text-halign': 'center',
      'text-wrap': 'wrap',
    },
  },
  {
    // The subtitle rides as a second label so a node can show what it IS --
    // its backend, whether it writes, which plan steps it owns -- without
    // clicking. That is the question people actually have.
    selector: 'node[sub]',
    style: {
      label: (n) => (n.data('sub') ? `${n.data('label')}\n${n.data('sub')}` : n.data('label')),
      'line-height': 1.5,
    },
  },
  { selector: 'node.end', style: { shape: 'ellipse', 'font-size': 10, color: '#8b98ae' } },
  { selector: 'node.entry', style: { 'border-width': 3, 'border-style': 'double' } },
  { selector: 'node.warn', style: { 'border-color': '#d9b84a' } },
  {
    selector: 'node.selected',
    style: { 'background-color': '#233046', 'border-color': '#35d6c4', 'border-width': 3 },
  },
  { selector: 'node.owns', style: { 'background-color': '#1e3a36' } },
  { selector: '.dimmed', style: { opacity: 0.22 } },
  {
    selector: 'edge',
    style: {
      width: 1.6,
      'line-color': (e) => EDGE_COLOUR[e.data('kind')] || '#4a5a78',
      'target-arrow-color': (e) => EDGE_COLOUR[e.data('kind')] || '#4a5a78',
      'target-arrow-shape': 'triangle',
      'arrow-scale': 0.9,
      'curve-style': 'bezier',
      label: 'data(label)',
      'font-size': 10,
      'font-family': 'ui-monospace, Menlo, monospace',
      color: '#8b98ae',
      'text-background-color': '#0b0f17',
      'text-background-opacity': 0.9,
      'text-background-padding': 2,
    },
  },
  { selector: 'edge.case', style: { 'line-style': 'dashed' } },
  { selector: 'edge.command', style: { 'line-style': 'dotted' } },
  // An edge that asks a human something is thicker: those are the points where
  // the run stops and waits for you, which is worth spotting at a glance.
  { selector: 'edge.asks', style: { width: 2.6 } },
];

const LAYOUT = {
  name: 'dagre',
  rankDir: 'TB',
  nodeSep: 45,
  rankSep: 60,
  fit: true,
  padding: 30,
};

function legend() {
  const box = document.createElement('div');
  box.className = 'legend';
  const entries = [
    ['agent', KIND_COLOUR.agent, 'node'],
    ['human', KIND_COLOUR.human, 'node'],
    ['edge', EDGE_COLOUR.edge, 'solid'],
    ['branch case', EDGE_COLOUR.case, 'dashed'],
    ['/command', EDGE_COLOUR.command, 'dotted'],
  ];
  for (const [label, colour, shape] of entries) {
    const item = document.createElement('span');
    item.className = 'legend-item';
    const swatch = document.createElement('i');
    swatch.style.borderColor = colour;
    swatch.className = `swatch ${shape}`;
    item.append(swatch, document.createTextNode(label));
    box.append(item);
  }
  return box;
}
