/*
  WHAT:  A turn as a transcript -- what the model said, each command it ran
         with the end of its output, each edit and search -- the way a coding
         assistant's own terminal shows it.
  WHY:   The stream was mostly bookkeeping. Measured on a real Codex turn: 26
         of 47 events were token-usage, moderation and diff notices, each
         drawn as a line ("* thread token usage updated"), around 6 commands,
         1 edit and 3 messages -- and the messages were raw JSON. You could not
         tell from it which step had gone wrong.
  CONCEPT: Keep only the kinds that say what the model DID; pair a command's
         start with its end; show a message's prose, not its JSON. Used by the
         live working row (chat.js) and the Activity tab (inspector.js).
*/

//: The kinds that say what the model did. Everything else -- token usage,
//: moderation metadata, diff and turn notices, approval reviews -- is the
//: provider's bookkeeping and is left out.
const MEANINGFUL = new Set(['command', 'tool', 'file_change', 'reasoning', 'message',
                            'web_search', 'plan', 'error']);

//: Where a model's JSON message keeps its prose, most telling first.
const PROSE_KEYS = ['summary', 'reason', 'reply', 'detail', 'problem', 'finding', 'message',
                    'text', 'decision'];

const OUTPUT_LINES = 4;
const MESSAGE_CHARS = 700;
const COMMAND_CHARS = 320;

/** The command as typed: Codex wraps every one in `/bin/bash -lc "..."`. */
export function shellCommand(command) {
  const text = String(command || '').trim();
  const wrapped = text.match(/^(?:\/(?:usr\/)?bin\/)?(?:ba|z)?sh\s+-l?c\s+(["'])([\s\S]*)\1$/);
  if (!wrapped) return text;
  return wrapped[1] === '"' ? wrapped[2].replace(/\\(["\\$`])/g, '$1') : wrapped[2];
}

/** A path as the session sees it: relative to its workspace. */
export function shortPath(path) {
  return String(path || '').replace(/^.*?\/workspace\//, '');
}

export function isNoise(event) {
  return !MEANINGFUL.has(event?.kind);
}

/** A message's prose: its text, or -- when it is JSON -- its first prose
 *  field. Works on a message still being typed, too. */
export function messageText(text) {
  const raw = String(text || '').trim();
  if (!raw.startsWith('{')) return raw;
  try {
    const value = JSON.parse(raw);
    // A checker's verdict ("next", "retry", "blocked") is the one word that
    // says whether the step went wrong: lead with it.
    // An orchestrator's `action` the same: dispatch, validate, finish.
    const verdict = ['verdict', 'action', 'status'].map((k) => value[k])
      .find((v) => typeof v === 'string' && v && v !== 'none' && v.length <= 24);
    const target = ['target', 'subject'].map((k) => value[k])
      .find((v) => typeof v === 'string' && v && v !== 'none');
    const lead = verdict ? `[${verdict}${target ? ` ${target}` : ''}] ` : '';
    for (const key of PROSE_KEYS) {
      if (typeof value[key] === 'string' && value[key].trim()) return lead + value[key].trim();
    }
    const first = Object.values(value)
      .find((v) => typeof v === 'string' && v.trim() && v !== verdict);
    return first ? lead + first.trim() : (lead.trim() || raw);
  } catch {
    // Still being typed: the value of the first prose key so far.
    for (const key of PROSE_KEYS) {
      const found = raw.match(new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)`));
      if (found) {
        try { return JSON.parse(`"${found[1]}"`); } catch { return found[1]; }
      }
    }
    // Or of whatever string field it is typing.
    const any = raw.match(/"[\w-]+"\s*:\s*"((?:[^"\\]|\\.)*)/);
    if (any) {
      try { return JSON.parse(`"${any[1]}"`); } catch { return any[1]; }
    }
    return raw;
  }
}

/** The events worth showing, with each command's start and end made one
 *  entry: `running` until its end arrives. */
export function transcriptEntries(events) {
  const out = [];
  for (const event of events || []) {
    if (isNoise(event)) continue;
    const ranCommand = event.kind === 'command' || event.kind === 'tool';
    // A tool can end in 'error' (agy: a refused path) rather than 'completed'.
    if (ranCommand && (event.phase === 'completed' || event.phase === 'error')) {
      const open = [...out].reverse().find((e) => e.running && e.kind === event.kind
                                               && e.command === event.command);
      if (open) {
        Object.assign(open, event, { running: false });
        continue;
      }
    }
    if (ranCommand && event.phase === 'started') {
      out.push({ ...event, running: true });
      continue;
    }
    // Everything else is only worth a row once it has content.
    if (event.phase === 'started' && event.kind !== 'error') continue;
    out.push({ ...event });
  }
  return out;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function tail(text, lines) {
  const all = String(text || '').replace(/\s+$/, '').split('\n');
  const kept = all.slice(-lines);
  return (all.length > lines ? `… ${all.length - lines} more lines\n` : '') + kept.join('\n');
}

/**
 * One entry as a transcript row: a marker, a headline, and underneath (⎿)
 * the end of a command's output. `full` adds the whole output, collapsed --
 * for the Activity tab, where there is room.
 */
export function transcriptRow(entry, { full = false } = {}) {
  const row = el('div', `tx tx-${entry.kind}${entry.running ? ' tx-running' : ''}`);
  const head = el('div', 'tx-head');
  let mark = '⏺';
  let text = '';

  switch (entry.kind) {
    case 'message':
      // Not labelled "answer": with a structured reply EVERY message is JSON,
      // its progress notes included, so the flag says nothing about which
      // one is the last word.
      mark = '●';
      text = messageText(entry.text);
      if (text.length > MESSAGE_CHARS) text = `${text.slice(0, MESSAGE_CHARS)}…`;
      break;
    case 'reasoning':
      mark = '✻';
      text = (entry.summary || []).slice(-1)[0] || 'thinking';
      break;
    case 'command': {
      const typed = shellCommand(entry.command);
      text = `$ ${typed.length > COMMAND_CHARS && !full
        ? `${typed.slice(0, COMMAND_CHARS)}…` : typed}`;
      break;
    }
    case 'tool':
      text = entry.command || 'tool';
      break;
    case 'file_change':
      text = `edited ${(entry.changes || []).map((c) => shortPath(c.path)).join(', ') || 'files'}`;
      break;
    case 'web_search':
      text = `searched: ${entry.query || ''}`;
      break;
    case 'plan':
      mark = '☐';
      text = entry.text || '';
      break;
    case 'error':
      mark = '✗';
      text = entry.message || 'error';
      break;
    default:
      text = entry.kind;
  }
  head.append(el('span', 'tx-mark', mark), el('span', 'tx-text', text));
  if (entry.at != null) head.append(el('span', 'tx-at', `${entry.at}s`));
  row.append(head);

  if (entry.kind === 'command' || entry.kind === 'tool') {
    const failed = entry.exit_code != null && entry.exit_code !== 0;
    const said = entry.running ? 'running…'
      : [entry.output ? tail(entry.output, OUTPUT_LINES) : '',
         failed ? `exit ${entry.exit_code}` : ''].filter(Boolean).join('\n');
    if (said) row.append(el('pre', `tx-out${failed ? ' tx-fail' : ''}`, said));
    if (full && entry.output && String(entry.output).split('\n').length > OUTPUT_LINES) {
      const more = el('details', 'tx-more');
      more.append(el('summary', '', 'whole output'), el('pre', 'tx-out', entry.output));
      row.append(more);
    }
  }
  if (full && entry.kind === 'message' && entry.text && messageText(entry.text) !== entry.text) {
    const raw = el('details', 'tx-more');
    raw.append(el('summary', '', 'as sent'), el('pre', 'tx-out', entry.text));
    row.append(raw);
  }
  return row;
}

/** A whole list of events, newest last; `limit` keeps the newest N rows. */
export function renderTranscript(container, events, { limit = 0, full = false } = {}) {
  let entries = transcriptEntries(events);
  const hidden = limit && entries.length > limit ? entries.length - limit : 0;
  if (hidden) entries = entries.slice(-limit);
  if (hidden) container.append(el('div', 'tx-hidden', `… ${hidden} earlier step(s)`));
  for (const entry of entries) container.append(transcriptRow(entry, { full }));
  return entries.length;
}
