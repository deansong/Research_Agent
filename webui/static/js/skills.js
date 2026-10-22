/*
  WHAT:  The skills control every node gets: a dropdown -- no skills,
         auto-discover, or selected skills -- and, for selected, a tick per
         skill in the repository's skills/ folder.
  WHY:   Which shared know-how a node is given is a per-node choice, for the
         design agent's own nodes (Design agent tab) and a designed agent's
         (Configure) alike. One control, so they cannot drift apart.
  CONCEPT: A choice is {mode, skills}; see agent/skills/__init__.py.
*/

export const SKILL_MODES = [
  ['none', 'No skills'],
  ['auto', 'Auto-discover'],
  ['selected', 'Selected skills'],
];

const HINT = {
  none: 'The node gets no skills.',
  auto: 'The node is shown every skill\'s name and description, and reads the ones its task needs.',
  selected: 'The ticked skills are given to the node in full.',
};

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/**
 * `choice` {mode, skills}; `available` [{name, description, path}];
 * `onChange(choice)` with a fresh object after every change.
 */
export function skillPicker(choice, available, onChange, { disabled = false } = {}) {
  const current = { mode: choice?.mode || 'none', skills: [...(choice?.skills || [])] };
  const box = make('div', 'skill-picker');

  const select = make('select', 'skill-mode');
  for (const [value, label] of SKILL_MODES) {
    const option = make('option', '', label);
    option.value = value;
    if (value === current.mode) option.selected = true;
    select.append(option);
  }
  select.value = current.mode;
  select.disabled = disabled;
  const hint = make('div', 'muted skill-hint');
  const list = make('div', 'skill-list');

  const emit = () => onChange?.({ mode: current.mode,
                                  skills: current.mode === 'selected' ? [...current.skills] : [] });

  function draw() {
    hint.textContent = HINT[current.mode] || '';
    list.replaceChildren();
    if (current.mode === 'auto') {
      list.append(make('div', 'muted', available.length
        ? `Shown to it: ${available.map((s) => s.name).join(', ')}`
        : 'There are no skills in the skills folder yet.'));
      return;
    }
    if (current.mode !== 'selected') return;
    for (const skill of available) {
      const row = make('label', 'skill-row');
      const tick = make('input');
      tick.type = 'checkbox';
      tick.checked = current.skills.includes(skill.name);
      tick.disabled = disabled;
      tick.addEventListener('change', () => {
        current.skills = tick.checked
          ? [...current.skills, skill.name]
          : current.skills.filter((n) => n !== skill.name);
        emit();
      });
      row.append(tick, make('span', 'skill-name', skill.name),
                 make('span', 'muted skill-desc', skill.description));
      list.append(row);
    }
    // Chosen once, gone from the folder since: said, not silently dropped.
    for (const name of current.skills.filter((n) => !available.some((s) => s.name === n))) {
      list.append(make('div', 'skill-missing', `${name} -- not in the skills folder`));
    }
    if (!available.length) list.append(make('div', 'muted', 'There are no skills yet.'));
  }

  select.addEventListener('change', () => {
    current.mode = select.value;
    draw();
    emit();
  });
  draw();
  box.append(select, hint, list);
  return box;
}
