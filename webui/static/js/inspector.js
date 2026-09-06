/*
  WHAT:  The right column: one node's parameters, and the prompt it will really
         be sent.
  WHY:   A node's configuration is spread across nodes.json, the plan and live
         state, and until now was never shown in one place.
  CONCEPT: Two tabs over one node -- Configure (what is in the file) and
         Context (what the model receives).

  --------------------------------------------------------------------------
  THE DISTINCTION THE CONTEXT TAB EXISTS FOR
  --------------------------------------------------------------------------
  What you write in `prompts.first` is a TEMPLATE. What the model reads is that
  template with eight kinds of placeholder substituted from live state. Those
  are very different strings, and the gap between them is where the surprises
  live -- because an unresolved placeholder renders as an EMPTY STRING rather
  than raising. `{out.planner.workstram}` (sic) validates clean, runs clean, and
  quietly sends the model a prompt with a hole in it. This tab is the only place
  that is visible.

  The same tab shows the working rules the compiler appends to a write-access
  node, which are not in nodes.json at all -- a generated agent is not allowed
  to opt out of them, so they are added at compile time. Showing only the file
  would misrepresent what runs.

  --------------------------------------------------------------------------
  THE FORM IS BUILT FROM THE SERVER'S SCHEMA
  --------------------------------------------------------------------------
  Dropdown choices -- access levels, field types, providers -- come from
  `/api/schema`, which is generated from the Pydantic models. The reference
  project we drew on hardcoded its equivalents in TypeScript, and its two
  schemas drifted a whole version apart.
*/

const el = (id) => document.getElementById(id);

export class Inspector {
  constructor({ onSave, onSelectSteps }) {
    this.onSave = onSave;
    this.onSelectSteps = onSelectSteps;
    this.configure = el('tab-configure');
    this.context = el('tab-context');
    this.schema = null;
    this.node = null;      // the raw nodes.json entry, edited in place
    this.name = null;
    this.kind = null;
    this.dirty = false;
  }

  setSchema(schema) {
    this.schema = schema;
  }

  clear(message) {
    this.name = null;
    this.node = null;
    this.configure.innerHTML =
      `<div class="empty-state"><p class="muted">${message || 'Select a node.'}</p></div>`;
    this.context.innerHTML =
      '<div class="empty-state"><p class="muted">Select a node.</p></div>';
  }

  /** `entry` is the node's nodes.json object; `kind` comes from graph.json. */
  show(name, kind, entry) {
    this.name = name;
    this.kind = kind;
    this.node = structuredClone(entry || {});
    this.dirty = false;
    this.drawConfigure();
    this.context.innerHTML =
      '<div class="empty-state"><p class="muted">Loading&hellip;</p></div>';
  }

  // ---- Configure -------------------------------------------------------

  drawConfigure() {
    const form = document.createElement('div');
    form.className = 'inspector-form';

    const head = document.createElement('div');
    head.className = 'inspector-head';
    head.innerHTML = `<strong>${this.name}</strong>`;
    const kind = document.createElement('span');
    kind.className = 'pill';
    kind.textContent = this.kind;
    head.append(kind);
    form.append(head);

    if (this.kind === 'human') {
      form.append(this.humanFields());
    } else {
      form.append(this.agentFields());
    }

    const actions = document.createElement('div');
    actions.className = 'inspector-actions';
    this.saveButton = button('Save node', 'btn tiny primary', () => this.save());
    this.saveButton.disabled = true;
    this.status = document.createElement('span');
    this.status.className = 'muted';
    actions.append(this.saveButton, this.status);
    form.append(actions);

    this.configure.replaceChildren(form);
  }

  agentFields() {
    const box = document.createElement('div');

    box.append(
      this.text('backend', 'Backend', this.node.backend || '',
                'A role name. Resolved by config.backend_for, so it can be '
                + 'anything -- override it with --backend-role <name>=codex.'),
      this.choice('access', 'Access', this.node.access || 'read_only',
                  this.schema?.node_access_levels || ['none', 'read_only', 'write'],
                  'write is the only level that can change your files.'),
      this.list('steps', 'Plan steps', this.node.steps || [],
                'Which plan steps this node is given IN FULL. Everything else '
                + 'it sees as a one-line outline.'),
      this.area('instructions', 'Instructions', this.node.instructions || '', 8,
                'The developer/system message. A write-access node also gets '
                + 'the working rules appended at compile time -- see Context.'),
    );

    const prompts = this.node.prompts || {};
    box.append(
      this.area('prompts.first', 'Prompt (first turn)', prompts.first || '', 6,
                'Used when the node has no provider thread yet, or when the '
                + 'counter in refresh_on has changed.'),
      this.area('prompts.next', 'Prompt (later turns)', prompts.next || '', 4,
                'Empty means "reuse the first prompt".'),
      this.text('thread_key', 'Thread key', this.node.thread_key || '',
                'Rendered, then used to group provider conversations. Two runs '
                + 'with the same key share a thread, and its cache.'),
      this.text('announce', 'Announce', this.node.announce || '',
                'Printed when the node starts, e.g. "coding...".'),
      this.text('record', 'Transcript line', this.node.record || '',
                'Rendered AFTER the turn, so it can quote what the node just '
                + 'produced.'),
    );

    box.append(this.outputFields());
    return box;
  }

  outputFields() {
    const box = document.createElement('div');
    box.className = 'subsection';
    const title = document.createElement('div');
    title.className = 'field-label';
    title.textContent = 'Output fields';
    const note = document.createElement('div');
    note.className = 'muted';
    note.textContent = 'The structured answer this node must return. A branch '
      + 'can only route on a field of type enum.';
    box.append(title, note);

    for (const [index, field] of (this.node.output || []).entries()) {
      const row = document.createElement('div');
      row.className = 'output-row';

      const name = input(field.name, (value) => {
        field.name = value; this.markDirty();
      });
      name.className = 'output-name';

      const type = document.createElement('select');
      for (const option of this.schema?.field_types
          || ['string', 'enum', 'string_list', 'integer', 'boolean']) {
        const node = document.createElement('option');
        node.value = option;
        node.textContent = option;
        type.append(node);
      }
      type.value = field.type || 'string';
      type.addEventListener('change', () => {
        field.type = type.value;
        this.markDirty();
        this.drawConfigure();
      });

      const required = document.createElement('input');
      required.type = 'checkbox';
      required.checked = Boolean(field.required);
      required.title = 'required';
      required.addEventListener('change', () => {
        field.required = required.checked; this.markDirty();
      });

      const remove = button('×', 'btn tiny ghost', () => {
        this.node.output.splice(index, 1);
        this.markDirty();
        this.drawConfigure();
      });

      row.append(name, type, required, remove);
      box.append(row);

      if (field.type === 'enum') {
        box.append(this.inlineList(
          `choices for ${field.name}`, field.choices || [],
          (values) => { field.choices = values; this.markDirty(); },
        ));
      }
    }

    box.append(button('+ field', 'btn tiny ghost', () => {
      this.node.output = this.node.output || [];
      this.node.output.push({ name: 'new_field', type: 'string' });
      this.markDirty();
      this.drawConfigure();
    }));
    return box;
  }

  humanFields() {
    const box = document.createElement('div');
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = 'A human node has exactly one setting: its commands. '
      + 'Routing is not configurable -- a plain answer goes wherever the ask '
      + 'that stopped here said it should.';
    box.append(note);

    for (const [index, command] of (this.node.commands || []).entries()) {
      const card = document.createElement('div');
      card.className = 'subsection';

      card.append(
        this.pair(`/${command.name}`, [
          input(command.name, (v) => { command.name = v; this.markDirty(); }),
          input(command.to, (v) => { command.to = v; this.markDirty(); }, 'goes to'),
        ]),
        this.text(`cmd${index}.summary`, 'Summary', command.summary || '', '',
                  (v) => { command.summary = v; this.markDirty(); }),
        this.list(`cmd${index}.purposes`, 'Valid at purposes',
                  command.purposes || [],
                  'Empty means "at every question".',
                  (v) => { command.purposes = v.length ? v : null; this.markDirty(); }),
        button('Remove command', 'btn tiny ghost', () => {
          this.node.commands.splice(index, 1);
          this.markDirty();
          this.drawConfigure();
        }),
      );
      box.append(card);
    }

    box.append(button('+ command', 'btn tiny ghost', () => {
      this.node.commands = this.node.commands || [];
      this.node.commands.push({ name: 'newcmd', to: '__end__', summary: '' });
      this.markDirty();
      this.drawConfigure();
    }));
    return box;
  }

  // ---- Context ---------------------------------------------------------

  showContext(context) {
    const box = document.createElement('div');

    if (context.kind === 'human') {
      box.innerHTML = '<p class="muted">A human node has no prompt. Its '
        + 'commands decide where a run goes next.</p>';
      box.append(pre(JSON.stringify(context.commands, null, 2)));
      this.context.replaceChildren(box);
      return;
    }

    box.append(section(
      'Prompt, as the model will receive it',
      'Placeholders resolved against the session’s current state. An '
      + 'unresolved one renders EMPTY rather than failing, so this is the only '
      + 'place a misspelt {out.node.field} is visible.',
      pre(context.prompts.first.rendered || '(empty)'),
    ));

    if (context.prompts.next.template) {
      box.append(section('Prompt on later turns', '',
                         pre(context.prompts.next.rendered || '(empty)')));
    }

    box.append(section(
      'Instructions',
      'What nodes.json says.',
      pre(context.instructions || '(none)'),
    ));

    if (context.appended_instructions) {
      box.append(section(
        'Appended at compile time',
        'Added by the loader because this node has write access, so a '
        + 'generated agent cannot opt out of it. Not in nodes.json.',
        pre(context.appended_instructions),
      ));
    }

    box.append(section(
      `Its own steps  ({my_steps})`,
      'Given in full. Everything else in the plan it sees as one line each.',
      pre(context.my_steps || '(this node owns no plan steps)'),
    ));

    box.append(section('The rest of the plan  ({plan_outline})', '',
                       pre(context.plan_outline || '(no plan)')));

    if (context.thread_key.template) {
      box.append(section(
        'Thread key',
        `${context.thread_key.template}  →  ${context.thread_key.rendered || '(empty)'}`,
        null,
      ));
    }

    if (Object.keys(context.last_output || {}).length) {
      box.append(section('Last output', 'What this node returned most recently.',
                         pre(JSON.stringify(context.last_output, null, 2))));
    }

    this.context.replaceChildren(box);
  }

  // ---- form helpers ----------------------------------------------------

  field(label, hint, control) {
    // A div, not a <label>. Some of these controls contain BUTTONS -- the ×
    // on a chip, for one -- and a button inside a label makes clicking it
    // also activate the label, which steals focus to a different control. The
    // caption is a span either way, so nothing is lost.
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const name = document.createElement('span');
    name.className = 'field-label';
    name.textContent = label;
    wrap.append(name, control);
    if (hint) {
      const note = document.createElement('span');
      note.className = 'muted';
      note.textContent = hint;
      wrap.append(note);
    }
    return wrap;
  }

  text(key, label, value, hint, onChange) {
    const control = input(value, onChange || ((v) => { this.set(key, v); }));
    return this.field(label, hint, control);
  }

  area(key, label, value, rows, hint) {
    const control = document.createElement('textarea');
    control.rows = rows;
    control.value = value;
    control.addEventListener('input', () => this.set(key, control.value));
    return this.field(label, hint, control);
  }

  choice(key, label, value, options, hint) {
    const control = document.createElement('select');
    for (const option of options) {
      const node = document.createElement('option');
      node.value = option;
      node.textContent = option;
      control.append(node);
    }
    control.value = value;
    control.addEventListener('change', () => this.set(key, control.value));
    return this.field(label, hint, control);
  }

  /**
   * A list of short strings, one chip each.
   *
   * NOT a comma-joined text box, which is what the reference UI does -- that
   * breaks on any value containing a comma and re-splits the whole list on
   * every keystroke.
   */
  list(key, label, values, hint, onChange) {
    const control = this.inlineList(label, values,
                                    onChange || ((v) => this.set(key, v)));
    return this.field(label, hint, control);
  }

  inlineList(label, values, onChange) {
    const box = document.createElement('div');
    box.className = 'chips';
    const current = [...values];

    const redraw = () => {
      box.replaceChildren();
      for (const [index, value] of current.entries()) {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.textContent = value;
        chip.append(button('×', 'chip-x', () => {
          current.splice(index, 1);
          onChange([...current]);
          redraw();
        }));
        box.append(chip);
      }
      const add = document.createElement('input');
      add.className = 'chip-add';
      add.placeholder = '+';
      add.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const value = add.value.trim();
        if (!value) return;
        current.push(value);
        onChange([...current]);
        redraw();
      });
      box.append(add);
    };

    redraw();
    return box;
  }

  pair(label, controls) {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const name = document.createElement('span');
    name.className = 'field-label';
    name.textContent = label;
    wrap.append(name, ...controls);
    return wrap;
  }

  set(key, value) {
    if (key.startsWith('prompts.')) {
      this.node.prompts = this.node.prompts || {};
      this.node.prompts[key.slice('prompts.'.length)] = value;
    } else {
      this.node[key] = value;
    }
    this.markDirty();
    if (key === 'steps') this.onSelectSteps?.(value);
  }

  markDirty() {
    this.dirty = true;
    if (this.saveButton) this.saveButton.disabled = false;
    if (this.status) this.status.textContent = 'unsaved';
  }

  async save() {
    this.saveButton.disabled = true;
    this.status.textContent = 'saving...';
    try {
      const result = await this.onSave(this.name, this.node);
      this.dirty = false;
      const blocking = (result?.problems || []).filter((p) => !p.warning).length;
      this.status.textContent = blocking
        ? `saved, but the agent has ${blocking} problem(s)`
        : 'saved';
    } catch (error) {
      this.status.textContent = error.message || 'save failed';
      this.saveButton.disabled = false;
      throw error;
    }
  }
}

function input(value, onChange, placeholder) {
  const control = document.createElement('input');
  control.value = value ?? '';
  if (placeholder) control.placeholder = placeholder;
  control.addEventListener('input', () => onChange(control.value));
  return control;
}

function button(text, className, onClick) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = className;
  element.textContent = text;
  element.addEventListener('click', onClick);
  return element;
}

function pre(text) {
  const element = document.createElement('pre');
  element.className = 'rendered';
  element.textContent = text;
  return element;
}

function section(title, hint, body) {
  const box = document.createElement('div');
  box.className = 'subsection';
  const heading = document.createElement('h3');
  heading.textContent = title;
  box.append(heading);
  if (hint) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = hint;
    box.append(note);
  }
  if (body) box.append(body);
  return box;
}
