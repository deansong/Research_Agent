/*
  WHAT:  Editing the SHAPE of a graph -- which nodes exist, and what connects
         to what.
  WHY:   Node parameters are a form. Topology is not: every change has
         consequences somewhere else in the file, and the validator is the only
         thing that knows what they are.
  CONCEPT: Edit graph.json directly, save, and let validate_folder answer.

  --------------------------------------------------------------------------
  WHY THIS DOES NOT TRY TO BE CLEVER
  --------------------------------------------------------------------------
  A drag-to-connect canvas is the obvious design and the wrong one here, for a
  reason specific to this format: an edge is not just a pair of node names.

    - An edge INTO a human node must carry an `ask` -- purpose, question,
      resume_to -- or the run stops with nothing to show you.
    - An edge OUT of a branching node is not an edge at all. It is a case
      inside a `branches` entry, keyed on an enum output field, and every
      choice of that enum needs somewhere to go.
    - A human node's exits are not in graph.json at all. They are commands in
      nodes.json, edited in the inspector.

  So a dragged arrow would have to open a form to be meaningful anyway. This
  panel shows the form directly, and the graph beside it stays the picture.

  --------------------------------------------------------------------------
  SAVE FIRST, COMPLAIN AFTER
  --------------------------------------------------------------------------
  Every edit here goes through the ordinary save, which writes even when the
  result is invalid and returns the problems. Deleting a node leaves dangling
  edges by definition -- refusing that would mean you could never delete
  anything -- so the problems list is the feedback, not a block.
*/

const END = '__end__';

export class TopologyPanel {
  constructor(container, { getAgent, onSave, onSelect }) {
    this.container = container;
    this.getAgent = getAgent;
    this.onSave = onSave;
    this.onSelect = onSelect;
    this.status = null;
  }

  draw() {
    const agent = this.getAgent();
    this.container.replaceChildren();

    if (!agent) {
      this.container.innerHTML =
        '<div class="empty-state"><p class="muted">No agent yet.</p></div>';
      return;
    }

    const graph = agent.graph;
    this.container.append(
      this.nodesSection(graph),
      this.edgesSection(graph),
      this.branchesSection(graph, agent),
      this.entrySection(graph),
    );

    this.status = document.createElement('div');
    this.status.className = 'muted';
    this.container.append(this.status);
  }

  // ---- nodes -----------------------------------------------------------

  nodesSection(graph) {
    const box = section('Nodes');

    for (const ref of graph.nodes) {
      const row = document.createElement('div');
      row.className = 'topo-row';

      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'topo-name';
      name.textContent = ref.name;
      name.title = 'Open in the inspector';
      name.addEventListener('click', () => this.onSelect(ref.name));

      const kind = document.createElement('span');
      kind.className = 'pill';
      kind.textContent = ref.kind;

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn tiny ghost';
      remove.textContent = 'delete';
      remove.addEventListener('click', () => this.deleteNode(ref.name));

      row.append(name, kind, remove);
      box.append(row);
    }

    const add = document.createElement('div');
    add.className = 'topo-add';

    const nameInput = document.createElement('input');
    nameInput.placeholder = 'new_node';
    nameInput.title = 'Lowercase letters, digits and underscores; must start '
                    + 'with a letter.';

    const kindSelect = select(['agent', 'human'], 'agent');

    add.append(nameInput, kindSelect, buttonEl('add node', () => {
      const name = nameInput.value.trim();
      if (!name) return;
      this.addNode(name, kindSelect.value);
    }));

    box.append(add);
    return box;
  }

  addNode(name, kind) {
    const agent = this.getAgent();
    if (agent.graph.nodes.some((n) => n.name === name)) {
      this.say(`There is already a node called ${name}.`);
      return;
    }

    const graph = clone(agent.graph);
    graph.nodes.push({ name, kind });

    // A new node needs an entry in nodes.json too -- the loader cross-checks
    // that the two name sets are equal, so a node in one and not the other is
    // a config_missing error rather than a half-created node.
    const nodes = clone(agent.nodes);
    nodes[name] = kind === 'human'
      ? { commands: [{ name: 'exit', to: END, summary: 'Finish' }] }
      : {
          backend: name,
          access: 'read_only',
          output: [{ name: 'summary', type: 'string', required: true }],
          prompts: { first: 'Describe what you did.\n\n{task_brief}' },
        };

    this.commit(graph, nodes, `added ${name}`);
  }

  deleteNode(name) {
    const agent = this.getAgent();
    const graph = clone(agent.graph);
    const nodes = clone(agent.nodes);

    graph.nodes = graph.nodes.filter((n) => n.name !== name);
    delete nodes[name];

    // Edges FROM the node go with it -- they cannot mean anything any more.
    // Edges TO it are deliberately left dangling, so the validator names them
    // as unknown_target and you can see what you have broken rather than
    // having the tool quietly rewire your graph.
    graph.edges = (graph.edges || []).filter((e) => e.from !== name);
    graph.branches = (graph.branches || []).filter((b) => b.from !== name);

    this.commit(graph, nodes, `deleted ${name}`);
  }

  // ---- edges -----------------------------------------------------------

  edgesSection(graph) {
    const box = section('Edges', 'Unconditional transitions. A node with a '
                                + 'branch cannot also have one of these.');

    for (const [index, edge] of (graph.edges || []).entries()) {
      const row = document.createElement('div');
      row.className = 'topo-row';

      const from = select(this.nodeNames(), edge.from);
      const arrow = document.createElement('span');
      arrow.className = 'muted';
      arrow.textContent = '→';
      const to = select([...this.nodeNames(), END], edge.to);

      const change = () => {
        const next = clone(this.getAgent().graph);
        next.edges[index] = { ...next.edges[index], from: from.value, to: to.value };
        this.commit(next, this.getAgent().nodes, 'rewired an edge');
      };
      from.addEventListener('change', change);
      to.addEventListener('change', change);

      const ask = document.createElement('span');
      ask.className = edge.ask ? 'pill' : 'muted';
      ask.textContent = edge.ask ? `asks: ${edge.ask.purpose}` : 'no ask';
      ask.title = edge.ask
        ? 'This transition stops and asks the human. Edit the wording in the '
          + 'JSON tab.'
        : 'An edge into a human node MUST have an ask, or the run stops with '
          + 'nothing to show.';

      row.append(from, arrow, to, ask, buttonEl('delete', () => {
        const next = clone(this.getAgent().graph);
        next.edges.splice(index, 1);
        this.commit(next, this.getAgent().nodes, 'deleted an edge');
      }));
      box.append(row);
    }

    const add = document.createElement('div');
    add.className = 'topo-add';
    const from = select(this.nodeNames());
    const to = select([...this.nodeNames(), END]);
    add.append(from, to, buttonEl('add edge', () => {
      const next = clone(this.getAgent().graph);
      next.edges = next.edges || [];
      next.edges.push(this.newEdge(from.value, to.value));
      this.commit(next, this.getAgent().nodes, 'added an edge');
    }));
    box.append(add);
    return box;
  }

  /**
   * A new edge, with an `ask` pre-filled when it targets a human node.
   *
   * Without this the graph validates (ask_missing is an error, so actually it
   * does not) -- and more to the point, the question would be blank. There is
   * a specific bug behind the fallback sentence: a designed agent once used
   * only `{out.node.question}` as its question, that field came back empty,
   * and the human was shown a prompt saying nothing at all.
   */
  newEdge(from, to) {
    const target = this.getAgent().graph.nodes.find((n) => n.name === to);
    if (target?.kind !== 'human') return { from, to };
    return {
      from,
      to,
      ask: {
        purpose: 'review',
        resume_to: from,
        question: `The ${from} node finished. What next?`,
        context: '',
      },
    };
  }

  // ---- branches --------------------------------------------------------

  branchesSection(graph, agent) {
    const box = section(
      'Branches',
      'A conditional exit. It routes on one of the node’s own enum output '
      + 'fields, so every choice of that enum needs a case or a default.',
    );

    for (const [index, branch] of (graph.branches || []).entries()) {
      const card = document.createElement('div');
      card.className = 'subsection';

      const head = document.createElement('div');
      head.className = 'topo-row';
      head.append(strong(branch.from), muted(`routes on ${branch.route_on}`));
      head.append(buttonEl('delete', () => {
        const next = clone(this.getAgent().graph);
        next.branches.splice(index, 1);
        this.commit(next, this.getAgent().nodes, 'deleted a branch');
      }));
      card.append(head);

      // Which enum values exist, so a missing case is visible right here
      // rather than only in the problems list.
      const field = (agent.nodes[branch.from]?.output || [])
        .find((f) => f.name === branch.route_on);
      const covered = new Set((branch.cases || []).map((c) => c.when));
      const missing = (field?.choices || []).filter((c) => !covered.has(c));

      for (const [caseIndex, entry] of (branch.cases || []).entries()) {
        const row = document.createElement('div');
        row.className = 'topo-row';
        const when = select(field?.choices || [entry.when], entry.when);
        const to = select([...this.nodeNames(), END], entry.to);
        const change = () => {
          const next = clone(this.getAgent().graph);
          next.branches[index].cases[caseIndex] = {
            ...next.branches[index].cases[caseIndex],
            when: when.value, to: to.value,
          };
          this.commit(next, this.getAgent().nodes, 'rewired a case');
        };
        when.addEventListener('change', change);
        to.addEventListener('change', change);
        row.append(when, muted('→'), to);
        card.append(row);
      }

      const fallback = document.createElement('div');
      fallback.className = 'topo-row';
      const dflt = select([...this.nodeNames(), END], branch.default || END);
      dflt.addEventListener('change', () => {
        const next = clone(this.getAgent().graph);
        next.branches[index].default = dflt.value;
        this.commit(next, this.getAgent().nodes, 'changed a branch default');
      });
      fallback.append(muted('otherwise →'), dflt);
      card.append(fallback);

      if (missing.length) {
        card.append(warn(`no case for: ${missing.join(', ')} — they will take `
                       + `the default (${branch.default || END}).`));
      }

      box.append(card);
    }
    return box;
  }

  // ---- entry -----------------------------------------------------------

  entrySection(graph) {
    const box = section('Entry', 'Where a run starts.');
    const row = document.createElement('div');
    row.className = 'topo-row';
    const entry = select(this.nodeNames(), graph.entry);
    entry.addEventListener('change', () => {
      const next = clone(this.getAgent().graph);
      next.entry = entry.value;
      this.commit(next, this.getAgent().nodes, 'changed the entry node');
    });
    row.append(entry);
    box.append(row);
    return box;
  }

  // ---- saving ----------------------------------------------------------

  nodeNames() {
    return (this.getAgent()?.graph.nodes || []).map((n) => n.name);
  }

  async commit(graph, nodes, what) {
    this.say(`saving (${what})...`);
    try {
      const result = await this.onSave({ graph, nodes });
      const blocking = (result?.problems || []).filter((p) => !p.warning);
      this.say(blocking.length
        ? `saved — ${blocking.length} problem(s); see the Problems tab`
        : `saved (${what})`);
    } catch (error) {
      this.say(error.message || 'save failed');
    }
  }

  say(text) {
    if (this.status) this.status.textContent = text;
  }
}

// ---------------------------------------------------------------------------

function clone(value) {
  return structuredClone(value);
}

function section(title, hint) {
  const box = document.createElement('div');
  box.className = 'subsection';
  const heading = document.createElement('h3');
  heading.textContent = title;
  box.append(heading);
  if (hint) box.append(muted(hint));
  return box;
}

function select(options, value) {
  const element = document.createElement('select');
  for (const option of options) {
    const node = document.createElement('option');
    node.value = option;
    node.textContent = option;
    element.append(node);
  }
  if (value !== undefined) element.value = value;
  return element;
}

function buttonEl(text, onClick) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = 'btn tiny ghost';
  element.textContent = text;
  element.addEventListener('click', onClick);
  return element;
}

function strong(text) {
  const element = document.createElement('strong');
  element.textContent = text;
  return element;
}

function muted(text) {
  const element = document.createElement('span');
  element.className = 'muted';
  element.textContent = text;
  return element;
}

function warn(text) {
  const element = document.createElement('div');
  element.className = 'topo-warn';
  element.textContent = text;
  return element;
}
