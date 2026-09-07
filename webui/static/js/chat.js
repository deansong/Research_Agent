/*
  WHAT:  The left column -- transcript, the question being asked, the composer.
  WHY:   This is the only panel that can DRIVE the agent. Everything else looks.
  CONCEPT: The browser half of interrupt() / Command(resume=...).

  --------------------------------------------------------------------------
  WHY THERE IS NO COMMAND PARSING HERE
  --------------------------------------------------------------------------
  Typing "/plan" sends the literal string "/plan". The graph parses it, in
  agent/commands.py, exactly as it does for the terminal -- and that file says
  why in so many words: the resume value goes into the sqlite checkpoint, so it
  must be the smallest durable thing, and re-parsing inside the node keeps the
  graph safe when it is driven by something that is not the terminal.

  So the chips below are typing shortcuts, nothing more. Which commands are
  legal right now is decided server-side and arrives on the `question` event;
  a front end with its own list would drift the moment a designed agent
  invented a command, which they do constantly.
*/

const el = (id) => document.getElementById(id);

//: The heartbeat line, which is the one log line we treat as an update rather
//: than an event. Matched on its own prefix; see codex.py::_heartbeat.
const HEARTBEAT = /^\s*\.\.\.\s*working:/;

export class Chat {
  constructor({ onSend, onDetail }) {
    this.onSend = onSend;
    /** Returns the turn running right now, for the expander. */
    this.onDetail = onDetail;
    this.beat = null;
    this.detailOpen = false;
    this.log = el('chat-log');
    this.empty = el('chat-empty');
    this.input = el('composer-input');
    this.send = el('btn-send');
    this.hint = el('composer-hint');
    this.commands = el('commands');
    this.questionBox = el('chat-question');
    this.pinned = true;

    el('composer').addEventListener('submit', (event) => {
      event.preventDefault();
      this.submit();
    });

    // Enter sends, Shift+Enter is a newline -- the convention everywhere.
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.submit();
      }
    });

    // Stop auto-scrolling the moment the reader scrolls up to read something,
    // and resume when they come back to the bottom. Yanking someone away from
    // the line they are reading is the classic log-viewer annoyance.
    this.log.addEventListener('scroll', () => {
      const distance = this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight;
      this.pinned = distance < 40;
    });

    el('btn-scroll').addEventListener('click', () => {
      this.pinned = true;
      this.scroll();
    });

    this.setWaiting(false);
  }

  // ---- writing into the log -------------------------------------------

  message(role, text) {
    if (!text) return;
    const node = document.createElement('div');
    node.className = `msg ${role === 'human' ? 'human' : 'agent'}`;
    const label = document.createElement('span');
    label.className = 'msg-role';
    label.textContent = role;
    node.append(label, document.createTextNode(text));
    this.append(node);
  }

  /** A print() from inside a node, or a provider progress line. */
  logLine(text, kind) {
    // The heartbeat is special: it repeats every thirty seconds for as long
    // as a turn runs, so appending each one turns a long turn into a wall of
    // near-identical lines. One row, updated in place, that expands.
    if (!kind && HEARTBEAT.test(text)) {
      this.heartbeat(text);
      return;
    }
    const node = document.createElement('div');
    node.className = `logline ${kind || classify(text)}`;
    node.textContent = text;
    this.append(node);
  }

  /**
   * The "working: ..." row, replaced rather than repeated, with a disclosure
   * that fetches the detail behind it.
   *
   * The detail is fetched on demand rather than streamed. A chatty turn can
   * emit fifteen events a second -- one report had 14,885 in seventeen
   * minutes -- and pushing those into the DOM would cost more than it tells
   * you. So the summary streams and the detail waits to be asked for.
   */
  heartbeat(text) {
    if (!this.beat) {
      this.beat = document.createElement('div');
      this.beat.className = 'logline heartbeat';

      this.beatText = document.createElement('span');
      this.beatText.className = 'beat-text';

      this.beatButton = document.createElement('button');
      this.beatButton.type = 'button';
      this.beatButton.className = 'beat-more';
      this.beatButton.textContent = 'show detail';
      this.beatButton.addEventListener('click', () => this.toggleDetail());

      this.beatDetail = document.createElement('div');
      this.beatDetail.className = 'beat-detail';
      this.beatDetail.hidden = true;

      const head = document.createElement('div');
      head.className = 'beat-head';
      head.append(this.beatText, this.beatButton);
      this.beat.append(head, this.beatDetail);
      this.append(this.beat);
    }

    this.beatText.textContent = text.replace(/^\s*\.\.\.\s*/, '');
    // If the detail is open, it follows the summary rather than needing its
    // own timer: the two are the same turn seen at two zoom levels, and one
    // clock for both is one thing that cannot fall out of step.
    if (this.detailOpen) void this.refreshDetail();
    // Keep it visible while it is the newest thing, but do not fight a reader
    // who has scrolled up to look at something.
    this.scroll();
  }

  /** A turn has finished: stop updating that row, and let the next one start
   *  a fresh one rather than reusing a stale expander. */
  endHeartbeat() {
    if (this.beat) this.beat.classList.add('done');
    this.beat = null;
    this.detailOpen = false;
  }

  async toggleDetail() {
    this.detailOpen = !this.detailOpen;
    this.beatDetail.hidden = !this.detailOpen;
    this.beatButton.textContent = this.detailOpen ? 'hide detail' : 'show detail';
    if (this.detailOpen) await this.refreshDetail();
  }

  async refreshDetail() {
    if (!this.detailOpen || !this.onDetail) return;
    try {
      const turn = await this.onDetail();
      renderTurn(this.beatDetail, turn);
    } catch (error) {
      this.beatDetail.textContent = String(error);
    }
  }

  append(node) {
    this.empty?.remove();
    this.empty = null;
    this.log.append(node);
    this.scroll();
  }

  scroll() {
    if (this.pinned) this.log.scrollTop = this.log.scrollHeight;
  }

  clear() {
    this.log.replaceChildren();
    this.empty = null;
    this.beat = null;
    this.detailOpen = false;
    this.question(null);
  }

  // ---- the question being asked ---------------------------------------

  question(pending, commands) {
    if (!pending) {
      this.questionBox.hidden = true;
      this.commands.hidden = true;
      this.setWaiting(false);
      return;
    }

    el('question-purpose').textContent = pending.purpose || 'input';
    el('question-text').textContent = pending.question || 'Your input is required.';

    const context = el('question-context');
    context.hidden = !pending.context;
    if (pending.context) el('question-context-body').textContent = pending.context;

    this.endHeartbeat();
    this.questionBox.hidden = false;
    this.renderCommands(commands || []);
    this.setWaiting(true);
    this.input.focus();
  }

  renderCommands(commands) {
    this.commands.replaceChildren();
    this.commands.hidden = commands.length === 0;

    for (const command of commands) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'cmd';
      chip.textContent = `/${command.name}`;
      chip.title = command.summary || '';
      chip.addEventListener('click', () => {
        // A command that takes an argument gets typed in for you, cursor at
        // the end, rather than sent -- sending it bare would be an arity error.
        const takesArgument = Boolean(command.argument);
        this.input.value = `/${command.name}${takesArgument ? ' ' : ''}`;
        if (takesArgument) {
          this.input.focus();
          this.hint.textContent = `/${command.name} ${command.argument}`;
        } else {
          this.submit();
        }
      });
      this.commands.append(chip);
    }
  }

  // ---- sending ----------------------------------------------------------

  submit() {
    const text = this.input.value.trim();
    if (!text || this.send.disabled) return;
    this.message('you', text);
    this.input.value = '';
    this.hint.textContent = '';
    this.setWaiting(false);
    this.onSend(text);
  }

  setWaiting(waiting) {
    this.send.disabled = !waiting;
    this.input.disabled = !waiting;
    if (!waiting && !this.hint.textContent) {
      this.hint.textContent = '';
    }
  }

  status(text) {
    this.hint.textContent = text || '';
  }
}

/**
 * Colour a captured stdout line by what it obviously is.
 *
 * Deliberately shallow: these are cosmetic classes, and nothing depends on
 * getting them right. Anything load-bearing travels as a typed event instead --
 * which is the whole reason webui/events.py has a closed vocabulary.
 */
function classify(text) {
  if (text.startsWith('[tokens:')) return 'tokens';
  if (/^\[[a-z_]+\]/.test(text)) return 'node';
  if (text.startsWith('!')) return 'error';
  return '';
}


/**
 * The events of one turn, inside the expander.
 *
 * Newest LAST, matching the log above it -- an expander that reads in the
 * opposite direction to the thing it hangs off is disorienting. Capped,
 * because a long turn can hold thousands and the point is the recent shape
 * of what it is doing.
 */
function renderTurn(container, payload) {
  container.replaceChildren();

  if (!payload || !payload.running || !payload.turn) {
    container.textContent = 'Nothing running right now.';
    return;
  }

  const turn = payload.turn;
  const head = document.createElement('div');
  head.className = 'beat-detail-head';
  const parts = Object.entries(turn.counts || {}).map(([k, n]) => `${n} ${k}`);
  // Typed tokens, said separately from work done. For a node whose answer is
  // a 12,000-token document this is most of the wait, and reporting it as
  // "12000 events" -- which is what used to happen -- reads as activity.
  const streamed = turn.progress?.streamed;
  if (streamed) parts.push(`writing ${(streamed / 1000).toFixed(1)}k`);
  if (turn.progress?.elapsed) parts.unshift(`${Math.round(turn.progress.elapsed)}s`);

  head.textContent = `${turn.node} — turn ${turn.index}`
    + (parts.length ? `  ·  ${parts.join(' · ')}` : '');
  container.append(head);

  // What it is writing RIGHT NOW, above the event list. During a long answer
  // the events stop entirely -- the model is producing one document, not
  // running commands -- so without this the expander is empty at exactly the
  // moment somebody opens it to find out what is happening.
  for (const [name, text] of Object.entries(turn.progress?.live || {})) {
    if (!text) continue;
    const row = document.createElement('div');
    row.className = `beat-live beat-live-${name}`;
    row.textContent = `${name} ▸ ${text}`;
    container.append(row);
  }

  const events = (turn.events || []).filter(
    (e) => e.phase !== 'started' || e.kind === 'command',
  );
  const shown = events.slice(-120);
  if (shown.length < events.length) {
    const note = document.createElement('div');
    note.className = 'muted';
    note.textContent = `(showing the last ${shown.length} of ${events.length})`;
    container.append(note);
  }

  for (const event of shown) {
    const row = document.createElement('div');
    row.className = `beat-event beat-${event.kind}`;
    const at = event.at != null ? `${String(event.at).padStart(6)}s  ` : '';
    row.textContent = at + describeEvent(event);
    container.append(row);
  }
}

function describeEvent(event) {
  switch (event.kind) {
    case 'command': {
      const code = event.exit_code;
      const mark = event.phase === 'started' ? '$' : (code ? '!' : '$');
      const tail = code ? `  (exit ${code})` : '';
      return `${mark} ${event.command || ''}${tail}`;
    }
    case 'file_change':
      return `~ ${(event.changes || []).map((c) => c.path).join(', ')}`;
    case 'reasoning':
      return `· ${(event.summary || []).slice(-1)[0] || ''}`;
    case 'message':
      return event.is_final_json
        ? `> the final answer (${(event.text || '').length.toLocaleString()} chars)`
        : `> ${firstLine(event.text)}`;
    case 'web_search': return `? ${event.query || ''}`;
    case 'plan': return `= ${firstLine(event.text)}`;
    case 'error': return `! ${event.message || ''}`;
    default: return `* ${event.kind}`;
  }
}

function firstLine(text) {
  return String(text || '').split('\n')[0];
}
