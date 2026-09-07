/*
  WHAT:  The plan: a numbered tree you can read, edit and save.
  WHY:   Until now the supported way to change a plan was "open plan.json in
         another window before you type /approve".
  CONCEPT: An editor over the file the graph already re-reads.

  --------------------------------------------------------------------------
  WHY EDITING HERE WORKS AT ALL
  --------------------------------------------------------------------------
  Nothing special had to be added to the agent. The planner writes plan.json
  BEFORE asking for approval, on purpose, and bootstrap/nodes/human.py re-reads
  it from disk when you type /approve -- printing "using your edited plan.json"
  if it differs. So this panel is a form over a hook that already existed, and
  the moment to use it is while the question on the left says `plan_review`.

  --------------------------------------------------------------------------
  THE LINK TO THE GRAPH
  --------------------------------------------------------------------------
  Clicking a step highlights the nodes whose `steps` list owns it. That is not
  decoration: `steps` is what decides how much of the plan each node is shown
  ({my_steps} in full, {plan_outline} as one line each), so "who is responsible
  for step 3" is the single most useful question about a designed agent, and it
  is invisible in both JSON files.
*/

export class PlanPanel {
  constructor(container, { onSave, onSelectStep }) {
    this.container = container;
    this.onSave = onSave;
    this.onSelectStep = onSelectStep;
    this.plan = null;
    this.owners = new Map();   // step id -> [node names]
    this.selected = null;
    this.ownedByNode = new Set();
    this.dirty = false;
  }

  /** `owners` comes from the agent view, and may be empty before one exists. */
  render(plan, owners) {
    this.plan = plan && plan.steps ? structuredClone(plan) : null;
    this.owners = owners || new Map();
    this.dirty = false;
    this.draw();
  }

  draw() {
    this.container.replaceChildren();

    if (!this.plan) {
      this.container.innerHTML =
        '<div class="empty-state"><p class="muted">No plan yet. '
        + 'Type <code>/plan</code> when you have finished discussing.</p></div>';
      return;
    }

    const head = document.createElement('div');
    head.className = 'plan-head';

    const summary = document.createElement('textarea');
    summary.className = 'plan-summary';
    summary.rows = 2;
    summary.value = this.plan.summary || '';
    summary.addEventListener('input', () => {
      this.plan.summary = summary.value;
      this.markDirty();
    });

    head.append(summary);
    this.container.append(head);

    const list = document.createElement('ol');
    list.className = 'plan-steps';
    for (const step of this.plan.steps) list.append(this.stepRow(step));
    this.container.append(list);

    const actions = document.createElement('div');
    actions.className = 'plan-actions';

    const add = button('Add step', 'btn tiny ghost', () => {
      this.plan.steps.push({
        id: String(this.plan.steps.length + 1),
        title: 'New step', detail: '', check: '', gate: false, substeps: [],
      });
      this.markDirty();
      this.draw();
    });

    this.saveButton = button('Save plan', 'btn tiny primary', () => this.save());
    this.saveButton.disabled = !this.dirty;

    this.status = document.createElement('span');
    this.status.className = 'muted';

    actions.append(add, this.saveButton, this.status);
    this.container.append(actions);
  }

  stepRow(step, parent) {
    const item = document.createElement('li');
    item.className = 'plan-step';
    item.dataset.stepId = step.id;

    const row = document.createElement('div');
    row.className = 'plan-row';

    const id = document.createElement('input');
    id.className = 'plan-id';
    id.value = step.id;
    id.title = 'Step id. Nodes refer to steps by this, so renumbering one '
             + 'means updating any node that owns it.';
    id.addEventListener('input', () => { step.id = id.value; this.markDirty(); });

    const title = document.createElement('input');
    title.className = 'plan-title';
    title.value = step.title || '';
    title.addEventListener('input', () => { step.title = title.value; this.markDirty(); });

    const owners = this.owners.get(step.id) || [];
    const badge = document.createElement('span');
    badge.className = owners.length ? 'plan-owner' : 'plan-owner none';
    badge.textContent = owners.length ? owners.join(', ') : 'unassigned';
    badge.title = owners.length
      ? `Nodes that receive this step in full: ${owners.join(', ')}`
      : 'No node lists this step, so nothing will be told about it in detail.';

    row.append(id, title, badge);
    row.addEventListener('click', (event) => {
      if (event.target.tagName === 'INPUT') return;
      this.selectStep(step.id);
    });

    const detail = document.createElement('textarea');
    detail.className = 'plan-detail';
    detail.rows = 2;
    detail.placeholder = 'detail (optional)';
    detail.value = step.detail || '';
    detail.addEventListener('input', () => { step.detail = detail.value; this.markDirty(); });

    item.append(row, detail);

    // `check` and `gate` are editable here because they are the two fields a
    // person is best placed to fix. The planner writes a check from what it
    // can infer; you know the command that actually decides, and you know
    // which steps you want to be asked about before they run.
    if (!parent) item.append(this.checkRow(step));

    if (!parent) {
      const subs = document.createElement('ol');
      subs.className = 'plan-substeps';
      for (const sub of step.substeps || []) subs.append(this.stepRow(sub, step));
      item.append(subs);

      item.append(button('+ substep', 'btn tiny ghost', () => {
        step.substeps = step.substeps || [];
        step.substeps.push({
          id: `${step.id}.${step.substeps.length + 1}`,
          title: 'New substep', detail: '',
        });
        this.markDirty();
        this.draw();
      }));
    }

    if (this.selected === step.id) item.classList.add('selected');
    if (this.ownedByNode?.has(step.id)) item.classList.add('owned');
    return item;
  }

  /** The step's own check, and whether it stops for a person. */
  checkRow(step) {
    const wrap = document.createElement('div');
    wrap.className = 'plan-check-row';

    const check = document.createElement('input');
    check.className = 'plan-check';
    check.placeholder = 'check: how would you tell this step worked?';
    check.title = 'Concrete enough that somebody else could apply it. The '
                + 'designer turns this into a node whose only job is to apply '
                + 'it, so a vague check becomes a node that rubber-stamps.';
    check.value = step.check || '';
    check.addEventListener('input', () => { step.check = check.value; this.markDirty(); });

    const gateLabel = document.createElement('label');
    gateLabel.className = 'plan-gate';
    gateLabel.title = 'Stop and ask a person before the run continues past '
                    + 'this step. Reserve it for the expensive and the '
                    + 'irreversible -- each gate halts the whole run until '
                    + 'somebody comes back to it.';

    const gate = document.createElement('input');
    gate.type = 'checkbox';
    gate.checked = Boolean(step.gate);
    gate.addEventListener('change', () => { step.gate = gate.checked; this.markDirty(); });

    gateLabel.append(gate, document.createTextNode('human gate'));
    wrap.append(check, gateLabel);
    return wrap;
  }

  selectStep(id) {
    this.selected = this.selected === id ? null : id;
    this.paint();
    this.onSelectStep?.(this.selected, this.owners.get(this.selected) || []);
  }

  /**
   * Highlight the steps a NODE owns -- the other direction of the same link.
   *
   * Selecting a node in the graph should show you which parts of the plan it
   * will be told about, exactly as selecting a step shows you which nodes are
   * told about it. Without both directions you can only ask the question one
   * way round, which is the half that happens to be easier to build.
   */
  highlightForNode(nodeName) {
    this.ownedByNode = new Set();
    if (nodeName) {
      for (const [stepId, owners] of this.owners) {
        if (owners.includes(nodeName)) this.ownedByNode.add(stepId);
      }
    }
    this.paint();
  }

  paint() {
    const owned = this.ownedByNode || new Set();
    for (const el of this.container.querySelectorAll('.plan-step')) {
      const id = el.dataset.stepId;
      el.classList.toggle('selected', id === this.selected);
      el.classList.toggle('owned', owned.has(id));
    }
  }

  markDirty() {
    this.dirty = true;
    if (this.saveButton) this.saveButton.disabled = false;
    if (this.status) this.status.textContent = 'unsaved';
  }

  async save() {
    if (!this.plan) return;
    this.saveButton.disabled = true;
    this.status.textContent = 'saving...';
    try {
      const result = await this.onSave(this.plan);
      this.dirty = false;
      const warnings = (result?.problems || []).length;
      this.status.textContent = warnings ? `saved, ${warnings} warning(s)` : 'saved';
    } catch (error) {
      // Say it. A silent failure leaves you believing an edit landed that did
      // not, and the next /approve then re-reads the OLD plan from disk.
      this.status.textContent = error.message || 'save failed';
      this.saveButton.disabled = false;
      throw error;
    }
  }
}

/**
 * step id -> the nodes that own it, from the agent view.
 *
 * Owning a top-level step means owning all its substeps -- that is what
 * `steps_for()` does on the server -- so the map records that too, or the UI
 * would claim a substep is unassigned when its parent covers it.
 */
export function ownersByStep(view, plan) {
  const owners = new Map();
  if (!view) return owners;

  const children = new Map();
  for (const step of plan?.steps || []) {
    children.set(step.id, (step.substeps || []).map((s) => s.id));
  }

  const add = (id, node) => {
    if (!owners.has(id)) owners.set(id, []);
    if (!owners.get(id).includes(node)) owners.get(id).push(node);
  };

  for (const node of view.nodes || []) {
    for (const id of node.steps || []) {
      add(id, node.id);
      for (const child of children.get(id) || []) add(child, node.id);
    }
  }
  return owners;
}

function button(text, className, onClick) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = className;
  element.textContent = text;
  element.addEventListener('click', onClick);
  return element;
}
