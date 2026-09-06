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

export class Chat {
  constructor({ onSend }) {
    this.onSend = onSend;
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
    const node = document.createElement('div');
    node.className = `logline ${kind || classify(text)}`;
    node.textContent = text;
    this.append(node);
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
