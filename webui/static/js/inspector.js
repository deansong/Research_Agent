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

import { renderTranscript } from './transcript.js';

const el = (id) => document.getElementById(id);

export class Inspector {
  /**
   * `getBackends()` -> role -> {provider, model, nodes, ...} for the agent on
   * screen; `onSetRole(role, {provider, model})` saves a role's backend for
   * the session; `models(refresh)` -> GET /api/models.
   */
  constructor({ onSave, onSelectSteps, getBackends, onSetRole, models }) {
    this.onSave = onSave;
    this.onSelectSteps = onSelectSteps;
    this.getBackends = getBackends;
    this.onSetRole = onSetRole;
    this.models = models;
    this.modelLists = null;   // provider -> [{id, label, default}], once loaded
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
      this.text('backend', 'Backend (role)', this.node.backend || '',
                'A role name. Nodes that share a role share its model; give '
                + 'this node a new name to set its model on its own.'),
      this.runsOn(),
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

    box.append(nodeSummary(context));

    box.append(section(
      'Prompt it will receive NEXT',
      'The template resolved against the session’s state AS IT IS NOW -- not '
      + 'what any past turn was sent, which is in Conversation. An unresolved '
      + 'placeholder renders EMPTY rather than failing, so this is the only '
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

    // What it PRODUCED, before the templates. For a research node this is
    // where the experiment actually lives -- which model, which dataset,
    // which config -- and it used to be the last thing on the tab, as a JSON
    // blob, under four sections of prompt.
    if (Object.keys(context.last_output || {}).length) {
      box.append(section(
        'What it produced',
        'Its structured output from the most recent turn. Later nodes read '
        + 'these as {out.' + context.name + '.<field>}.',
        outputTable(context.last_output),
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

    this.context.replaceChildren(box);
  }

  // ---- form helpers ----------------------------------------------------

  /**
   * Which provider and model this node's ROLE runs on, changeable here.
   *
   * There was no way to do this once a session had an agent: the New dialog
   * chooses models before the session exists, and the Design agent tab only
   * covers the design phase's own nodes. Saved with the session; applied at
   * the next Start, because a run builds its backends when it starts.
   */
  runsOn() {
    const role = this.node.backend || '';
    const resolved = this.getBackends?.()?.[role];
    const box = document.createElement('div');
    box.className = 'runs-on';
    if (!role || !this.onSetRole) return box;

    const providers = (this.schema?.providers || ['codex', 'claude_code', 'antigravity'])
      .filter((p) => p !== 'api');
    const pickProvider = (value) => {
      const select = document.createElement('select');
      for (const name of providers) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.append(option);
      }
      select.value = value;
      return select;
    };
    // A model dropdown that follows a provider dropdown: that provider's own
    // list, with `currentFor(provider)` kept selectable even if unlisted.
    const pickModel = (providerSelect, currentFor) => {
      const select = document.createElement('select');
      const refill = () => {
        select.replaceChildren();
        const offered = this.modelLists?.[providerSelect.value] || [];
        const current = currentFor(providerSelect.value);
        const blank = document.createElement('option');
        blank.value = '';
        blank.textContent = '(provider default)';
        select.append(blank);
        if (current && !offered.some((m) => m.id === current)) {
          const kept = document.createElement('option');
          kept.value = current;
          kept.textContent = current;
          select.append(kept);
        }
        for (const m of offered) {
          const option = document.createElement('option');
          option.value = m.id;
          option.textContent = m.id + (m.default ? '  (default)' : '');
          select.append(option);
        }
        select.value = current;
      };
      refill();
      providerSelect.addEventListener('change', refill);
      select.refill = refill;
      return select;
    };

    const provider = pickProvider(resolved?.provider || 'codex');

    const model = document.createElement('select');
    const fill = () => {
      model.replaceChildren();
      const offered = this.modelLists?.[provider.value] || [];
      const current = provider.value === resolved?.provider ? (resolved?.model || '') : '';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '(provider default)';
      model.append(blank);
      if (current && !offered.some((m) => m.id === current)) {
        const kept = document.createElement('option');
        kept.value = current;
        kept.textContent = current;
        model.append(kept);
      }
      for (const m of offered) {
        const option = document.createElement('option');
        option.value = m.id;
        option.textContent = m.id + (m.default ? '  (default)' : '');
        model.append(option);
      }
      model.value = current;
    };
    // GPU: Codex runs commands in a sandbox that hides the GPUs; this runs
    // the role's write turns without it. Only offered for Codex -- the other
    // providers have no sandbox to switch off.
    const gpu = document.createElement('input');
    gpu.type = 'checkbox';
    gpu.checked = Boolean(resolved?.gpu);
    const gpuLabel = document.createElement('label');
    gpuLabel.className = 'runs-on-gpu';
    gpuLabel.append(gpu, ' GPU — run its write turns without Codex\'s sandbox, '
      + 'which hides the GPUs');
    const syncGpu = () => { gpuLabel.hidden = provider.value !== 'codex'; };

    // Delegation: only when ticked. A delegate is a different model, so it is
    // never chosen for you -- see agent/backends/delegate.py.
    const chosen = resolved?.delegate || null;
    const delegateOn = document.createElement('input');
    delegateOn.type = 'checkbox';
    delegateOn.checked = Boolean(chosen);
    const delegateProvider = pickProvider(chosen?.provider || 'antigravity');
    const delegateModel = pickModel(delegateProvider,
      (p) => (chosen && p === chosen.provider ? chosen.model || '' : ''));
    const syncDelegate = () => {
      delegateProvider.disabled = !delegateOn.checked;
      delegateModel.disabled = !delegateOn.checked;
    };
    delegateOn.addEventListener('change', syncDelegate);
    syncDelegate();
    const delegateLabel = document.createElement('label');
    delegateLabel.className = 'runs-on-delegate';
    delegateLabel.append(delegateOn, ' If its provider runs out of credits, delegate to');
    const delegateRow = document.createElement('div');
    delegateRow.className = 'runs-on-row';
    delegateRow.append(delegateProvider, delegateModel);

    fill();
    syncGpu();
    provider.addEventListener('change', () => { fill(); syncGpu(); });
    if (!this.modelLists && this.models) {
      this.models(false).then((listed) => {
        this.modelLists = Object.fromEntries((listed.providers || [])
          .map((p) => [p.provider, p.models || []]));
        fill();
        delegateModel.refill();
      }).catch(() => { /* typed model ids still work: the default stays */ });
    }

    const status = document.createElement('span');
    status.className = 'muted';
    const apply = button('Use for this role', 'btn tiny', async () => {
      apply.disabled = true;
      try {
        const result = await this.onSetRole(role, {
          provider: provider.value, model: model.value,
          gpu: provider.value === 'codex' && gpu.checked,
          delegate: delegateOn.checked
            ? { provider: delegateProvider.value, model: delegateModel.value } : null,
        });
        status.textContent = result.detail || 'saved';
      } catch (error) {
        status.textContent = String(error);
      } finally {
        apply.disabled = false;
      }
    });

    const row = document.createElement('div');
    row.className = 'runs-on-row';
    row.append(provider, model, apply);
    const shared = resolved?.nodes?.length > 1
      ? `Role "${role}" is used by ${resolved.nodes.join(', ')} -- this changes all of them.`
      : `Role "${role}".`;
    const where = resolved?.session_choice ? ' Chosen for this session.' : '';
    return this.field('Runs on', `${shared}${where} Used when the run starts; `
                      + 'a run already going needs Pause, then Start.',
                      (() => { const wrap = document.createElement('div');
                               wrap.append(row, gpuLabel, delegateLabel, delegateRow, status);
                               return wrap; })());
  }

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

/**
 * The header of the Context tab: what this node IS, in one glance.
 *
 * The role name was the only backend fact on the page, and it cannot tell you
 * what is about to read the prompt below. "checker" resolving to a cheap
 * model rather than a careful one is the difference between a verifier and a
 * rubber stamp, and it is invisible until you go and read three config files.
 */
function nodeSummary(context) {
  const resolved = context.resolved || {};
  const model = resolved.model
    ? `${resolved.provider}/${resolved.model}`
    : '(unresolved)';

  const rows = {
    'runs on': `${context.backend || '(no role)'}  →  ${model}`,
    access: context.access || '',
  };
  // The TITLES where we have them: "1.3 Base-model baseline" says what this
  // node is for, and "steps 1.3" does not. This is the line that connects a
  // node name the designer invented back to the plan a person approved.
  if (context.step_titles?.length) {
    rows['plan steps'] = context.step_titles.join('\n');
  } else if (context.steps?.length) {
    rows['plan steps'] = context.steps.join(', ');
  }
  if (context.output?.length) {
    rows['must return'] = context.output.map((f) => f.name).join(', ');
  }

  const box = document.createElement('div');
  box.className = 'subsection node-summary';
  const list = document.createElement('dl');
  list.className = 'facts';
  for (const [key, value] of Object.entries(rows)) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    dd.title = value;
    if (value.includes('\n')) dd.style.setProperty('white-space', 'pre-wrap');
    list.append(dt, dd);
  }
  box.append(list);

  // Said out loud, because a role nobody configured silently takes the
  // default -- which is how an experiment ends up on a model nobody chose.
  if (resolved.model && resolved.configured === false) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = `Nothing configures "${context.backend}", so it falls `
      + `back to the default. Set it in <repo>/.agent/config.json.`;
    box.append(note);
  }
  return box;
}

/** A node's structured output as fields, not as a JSON blob. */
function outputTable(output) {
  const list = document.createElement('dl');
  list.className = 'facts output-facts';
  for (const [key, value] of Object.entries(output)) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    // Objects and lists still want JSON; strings must NOT be quoted, because
    // a path or a config summary is what somebody is here to read.
    dd.textContent = typeof value === 'string'
      ? value
      : JSON.stringify(value, null, 2);
    list.append(dt, dd);
  }
  return list;
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

// ---------------------------------------------------------------------------
// Activity: what the provider actually did
// ---------------------------------------------------------------------------

/*
  This is the panel the whole activity record exists for. The terminal reports
  "313 events, last one 7s ago", which tells you a turn is alive and nothing
  about it. Every one of those events is here instead: the commands, their exit
  codes, their OUTPUT -- which the terminal never shows at all -- the files
  touched, and the model's own account of what it was doing.

  Rendered newest turn first, because a node in a retry loop has many and the
  last one is why you came looking.
*/


export function renderActivity(container, payload, onPick) {
  container.replaceChildren();

  // Every node that has a record, selectable -- INCLUDING the design phase.
  // discussor, planner and designer do their own turns and are not in the
  // generated agent's graph, so the inspector could never select them: six
  // turns of the conversation that produced the whole agent sat on disk with
  // no way to reach them. This row is that way.
  if (payload?.nodes_with_activity?.length && onPick) {
    container.append(nodePicker(payload.nodes_with_activity, payload.node, onPick));
  }

  const turns = payload?.turns || [];
  if (!turns.length) {
    const note = document.createElement('div');
    note.className = 'empty-state';
    const text = document.createElement('p');
    text.className = 'muted';
    text.textContent = payload?.nodes_with_activity?.length
      ? 'Nothing recorded for this node yet. Pick one above that has run.'
      : 'Nothing recorded yet. Events appear once a node has run against a '
        + 'real provider (the fake backend emits a sample turn).';
    note.append(text);
    container.append(note);
    return;
  }

  // Oldest first is the wrong default for a tail and the right one for a
  // CONVERSATION: turn 1 is the question turn 2 is answering. The server
  // sends newest first because the "still working" line wants the newest.
  const ordered = [...turns].sort((a, b) => (a.index || 0) - (b.index || 0));

  for (const turn of ordered) {
    const box = document.createElement('div');
    box.className = 'turn';

    const head = document.createElement('div');
    head.className = 'turn-head';
    const counts = Object.entries(turn.counts || {})
      .map(([kind, n]) => `${n} ${kind}`).join(' · ');
    head.innerHTML = `<strong>turn ${turn.index}</strong>`;
    head.append(muted(`${turn.started}${counts ? '  —  ' + counts : ''}`));
    box.append(head);
    // Which model actually ran it, and in which conversation: the check that
    // a node meant for a cheap model ran on one, and the key to every earlier
    // turn that was in its context (same conversation = same id).
    if (turn.provider || turn.model || turn.thread_id) {
      const ran = document.createElement('div');
      ran.className = 'turn-model';
      const thread = turn.thread_id ? `  ·  conversation ${String(turn.thread_id).slice(0, 12)}` : '';
      ran.textContent = `ran on ${turn.provider || '?'} / ${turn.model || '(provider default)'}${thread}`;
      box.append(ran);
    }

    // ---- what it was ASKED -------------------------------------------
    // First, because it is the cause of everything below it. Collapsed: a
    // prompt runs to thousands of characters and the events are usually what
    // you came for -- but re-rendering the template in the Context tab shows
    // TODAY's state, not what this turn actually read, so this is the only
    // place the real question exists.
    if (turn.sent?.prompt) {
      box.append(exchange(
        `asked  (prompts.${turn.sent.template || '?'})`,
        turn.sent.prompt,
        'sent',
      ));
    }

    // What it did, as a transcript: its words, each command with the end of
    // its output (the whole of it a click away), each edit and search --
    // the provider's bookkeeping left out. See transcript.js.
    renderTranscript(box, turn.events || [], { full: true });

    // ---- what it RETURNED --------------------------------------------
    // The structured answer, which is what the next node actually reads.
    // It was recorded all along and shown nowhere.
    // `output` is the whole answer, lists included; older records only have
    // the scalar `summary`.
    const returned = turn.output && Object.keys(turn.output).length ? turn.output : turn.summary;
    if (returned && Object.keys(returned).length) {
      const body = Object.entries(returned)
        .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value, null, 1)}`)
        .join('\n');
      box.append(exchange('returned', body, 'returned'));
    }

    container.append(box);
  }
}

/** Every node with a record, as chips. The only route to the design phase. */
function nodePicker(names, current, onPick) {
  const row = document.createElement('div');
  row.className = 'node-picker';

  const label = document.createElement('span');
  label.className = 'muted';
  label.textContent = 'node:';
  row.append(label);

  for (const name of names) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = `cmd${name === current ? ' active' : ''}`;
    chip.textContent = name;
    chip.addEventListener('click', () => onPick(name));
    row.append(chip);
  }
  return row;
}


/**
 * One side of the exchange -- what went in, or what came out -- collapsed.
 *
 * A <details> rather than a pre: the prompt is the biggest thing on the page
 * and it is not what you look at first. Open it when the events do not
 * explain themselves.
 */
function exchange(label, body, kind) {
  const box = document.createElement('details');
  box.className = `exchange exchange-${kind}`;
  const summary = document.createElement('summary');
  summary.textContent = `${label}  (${body.length.toLocaleString()} chars)`;
  const pre = document.createElement('pre');
  pre.className = 'exchange-body';
  pre.textContent = body;
  box.append(summary, pre);
  return box;
}


/** Pretty-print JSON, or hand back the text unchanged. */
function muted(text) {
  const node = document.createElement('span');
  node.className = 'muted';
  node.textContent = text;
  return node;
}
