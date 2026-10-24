"""
WHAT:  The command line: `python -m research_agent bootstrap "<idea>"` starts a
       run and talks with you through the discussor; `--resume <run>` carries
       on where a run stopped.
WHY:   The bootstrap is a conversation. The terminal shows the discussor's
       words, one line per piece of work the team does, and a status line;
       slash commands show the research state without asking a model.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import research_backend as RB

from research_agent import state as S
from research_agent.bootstrap import config, nodes, views
from research_agent.bootstrap.session import Bootstrap

RUNS_ROOT = Path.home() / "research_runs"

HELP = """\
Commands:
  /state                 the whole research state
  /show <section> ...    one or more sections: goal constraints assumptions evidence
                         hypotheses rubric protocol plan design decisions
                         open_questions risks results status
  /approve <section> ... approve as they stand: hypotheses rubric protocol plan design
  /sessions              every model call of this run (research_backend sessions)
  /retry                 run again the step that failed
  /usage                 the providers' usage limits
  /trace on|off          print every model call's input, output and what it does between
  /help                  this
  /exit                  finish (the run stays in its folder)
Anything else is said to the discussor."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap", help="turn an idea into an approved research design")
    boot.add_argument("idea", nargs="?", help="the idea, in a sentence or a paragraph")
    boot.add_argument("--run", help=f"the run folder (default: a new one in {RUNS_ROOT})")
    boot.add_argument("--resume", metavar="RUN", help="carry on with an earlier run")
    boot.add_argument("--role", action="append", default=[], metavar="ROLE=PROVIDER[:MODEL[:EFFORT]]",
                      help="which model a role uses, e.g. planner=codex:gpt-5.6-sol:high "
                           "(all=... sets every role)")
    boot.add_argument("--skills", action="append", default=[], metavar="NODE=none|auto|SKILL,SKILL",
                      help="which skills a node gets, e.g. planner_plan=experiment-design,"
                           "statistical-power; nodes: " + ", ".join(config.NODES))
    boot.add_argument("--skills-dir", metavar="FOLDER", help="the skills folder to use")
    boot.add_argument("--progress", action="store_true",
                      help="print what each model does while it works: commands, files")
    boot.add_argument("--trace", action="store_true",
                      help="print every model call's full input and output, and what the "
                           "model does in between (implies --progress)")
    args = parser.parse_args(argv)

    if args.resume:
        folder = Path(args.resume).expanduser()
        if not (folder / "checkpoint.sqlite").exists():
            parser.error(f"{folder} is not a run folder (no checkpoint.sqlite).")
    elif args.idea:
        folder = Path(args.run).expanduser() if args.run else _new_folder(args.idea)
    else:
        parser.error("give an idea, or --resume <run>.")
    try:
        cfg = config.load(folder, roles=args.role, skills=args.skills, skills_dir=args.skills_dir)
    except ValueError as exc:
        parser.error(str(exc))
    folder.mkdir(parents=True, exist_ok=True)
    config.save(folder, cfg)
    return _converse(Bootstrap(nodes.Run.from_config(folder, cfg, progress=args.progress,
                                                     trace=args.trace)),
                     args.idea if not args.resume else None)


def _converse(boot: Bootstrap, idea: str | None) -> int:
    folder = boot.run.folder
    resume_hint = f"Carry on with:  python -m research_agent bootstrap --resume {folder}"
    print(f"Run: {folder}")
    print(f"  saved as it goes: state/research.md (the research state), rb/sessions/ (every model "
          f"call, input and output), checkpoint.sqlite (for --resume)")
    run = boot.run
    print(config.BootstrapConfig(run.roles, run.skills or {}, run.skills_dir).describe())
    _warn_signed_out(boot)
    print("Type /help for commands.\n")
    failed = False

    def advance(action) -> bool:
        """Run one step of the conversation and show where it stopped. A
        model failing stops it with nothing lost: /retry runs it again."""
        nonlocal failed
        try:
            turn = action()
        except RB.BackendCancelled:
            raise KeyboardInterrupt from None
        except RB.BackendError as exc:
            failed = True
            print(f"\n! {exc}\n  Nothing is lost: /retry runs that step again; /exit leaves.")
            return True
        failed = False
        if turn is None:
            return True
        _show_turn(boot, turn)
        return turn.waiting

    try:
        if idea is not None and not boot.started:
            going = advance(lambda: boot.start(idea, on_step=_show_step))
        else:
            last = boot.last_turn
            failed = not last.waiting and not last.outcome
            going = advance(lambda: last) if not failed else True
            if failed:
                print("The last step did not finish: /retry runs it again.")
        while going:
            try:
                text = input("you> ").strip()
            except EOFError:
                print(f"\nStopped. {resume_hint}")
                return 0
            if not text:
                continue
            command = text.split()[0] if text.startswith("/") else ""
            if command == "/retry":
                if failed:
                    going = advance(lambda: boot.retry(on_step=_show_step))
                else:
                    print("Nothing to retry.")
            elif failed and command not in READ_ONLY + ("/exit",):
                print("The last step failed: /retry it first (or /exit).")
            elif command:
                going = advance(lambda: _command(boot, text))
            else:
                going = advance(lambda: boot.answer(text, on_step=_show_step))
        return 0
    except KeyboardInterrupt:
        print(f"\nStopped. {resume_hint}")
        return 130
    finally:
        boot.close()


#: Commands that only show things, allowed while a failed step waits for /retry.
READ_ONLY = ("/help", "/state", "/show", "/sessions", "/usage", "/trace")


def _command(boot: Bootstrap, text: str):
    """Run a slash command. Returns a new Turn when the conversation moved on,
    else None."""
    name, *words = text[1:].split()
    st = boot.research
    if name == "help":
        print(HELP)
    elif name == "state":
        print(S.render(st) or "(the research state is empty)")
    elif name == "show":
        unknown = [w for w in words if w not in S.SECTIONS]
        if not words or unknown:
            print(f"Show which? {', '.join(S.SECTIONS)}")
        else:
            print(S.render(st, words) or "(nothing there yet)")
    elif name == "approve":
        bad = [w for w in words if w not in S.APPROVABLE]
        missing = [w for w in words if w in S.APPROVABLE and not S.version(st, w)]
        if not words or bad:
            print(f"Approve which? {', '.join(S.APPROVABLE)}")
        elif missing:
            print(f"There is no {', '.join(missing)} to approve yet.")
        else:
            return boot.answer("", approve=words, on_step=_show_step)
    elif name == "sessions":
        print(RB.list_sessions(home=boot.run.rb_home))
    elif name == "usage":
        print(RB.Listing(RB.usage(p) for p in sorted({c.provider for c in boot.run.roles.values()})))
    elif name == "trace":
        if words[:1] not in (["on"], ["off"]):
            print(f"/trace on or /trace off (it is {'on' if boot.run.trace else 'off'}).")
        else:
            boot.run.trace = words[0] == "on"
            print(f"Tracing is {words[0]}.")
    elif name == "exit":
        return boot.exit()
    else:
        print(f"No command /{name}. /help lists them.")
    return None


def _show_step(node: str, update: dict) -> None:
    """One line per piece of work, as it finishes."""
    if node == "orchestrator":
        d = update.get("decision") or {}
        if d.get("action") not in (None, "ask_user", "ready"):
            print(f"  . orchestrator -> {d['action']}: {_short(d.get('reason', ''))}", flush=True)
    elif node in ("researcher", "planner", "designer"):
        print(f"  . {_short(update.get('last', node), 160)}", flush=True)


def _show_turn(boot: Bootstrap, turn) -> None:
    if turn.reply:
        print()
        for paragraph in turn.reply.split("\n"):
            print(textwrap.fill(paragraph, 100) if paragraph.strip() else "")
        print()
    st = boot.research
    hypotheses = [h for h in st.get("hypotheses") or [] if h.get("status") != "dropped"]
    steps = len((st.get("plan") or {}).get("steps") or [])
    print(f"[{(st.get('status') or {}).get('phase') or 'discussing'} | "
          f"{len(st.get('evidence') or [])} evidence, {len(hypotheses)} hypotheses, "
          f"{steps} plan steps | {views.approvals_line(st)}]")
    if turn.outcome == "ready":
        print(f"\nReady: the research design is in {boot.run.folder / 'state' / 'research.md'}. "
              f"Keep talking to change it, or /exit.")
    if turn.outcome == "exit":
        print(f"Finished. The run is in {boot.run.folder}.")


def _warn_signed_out(boot: Bootstrap) -> None:
    for provider in sorted({c.provider for c in boot.run.roles.values()}):
        status = RB.status(provider)
        if status.logged_in is False or not status.installed:
            print(f"! {status}")


def _new_folder(idea: str) -> Path:
    slug = "-".join(re.findall(r"[a-z0-9]+", idea.lower())[:6]) or "run"
    return RUNS_ROOT / f"{datetime.now():%Y%m%d-%H%M}-{slug}"


def _short(text: str, width: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


if __name__ == "__main__":
    sys.exit(main())
