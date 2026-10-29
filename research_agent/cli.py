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
import queue
import re
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path

import research_backend as RB

from research_agent import state as S
from research_agent.bootstrap import carry, config, nodes, views
from research_agent.bootstrap.session import Bootstrap
from research_agent.execute import config as exec_config
from research_agent import inbox as exec_inbox
from research_agent.execute import jobs as exec_jobs
from research_agent.execute import nodes as exec_nodes
from research_agent.execute.session import Execution

RUNS_ROOT = Path.home() / "research_runs"

HELP = """\
Commands:
  /state                 the whole research state
  /show <section> ...    one or more sections: goal constraints assumptions evidence
                         hypotheses rubric protocol plan design decisions
                         open_questions risks results status
  /approve [brief|design] approve the brief, or the design (hypotheses, rubric, protocol, steps, nodes)
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
    boot.add_argument("--from", dest="start_from", metavar="RUN",
                      help="start a new run from a finished one: its research state and its "
                           "reviewer's review; the designer revises the design for it")
    boot.add_argument("--role", action="append", default=[], metavar="ROLE=PROVIDER[:MODEL[:EFFORT]]",
                      help="which model a role uses, e.g. designer=codex:gpt-5.6-sol:high "
                           "(all=... sets every role)")
    boot.add_argument("--skills", action="append", default=[], metavar="NODE=none|auto|SKILL,SKILL",
                      help="which skills a node gets, e.g. designer=experiment-design,"
                           "ml-statistics; nodes: " + ", ".join(config.NODES))
    boot.add_argument("--skills-dir", metavar="FOLDER", help="the skills folder to use")
    boot.add_argument("--progress", action="store_true",
                      help="print what each model does while it works: commands, files")
    boot.add_argument("--trace", action="store_true",
                      help="print every model call's full input and output, and what the "
                           "model does in between (implies --progress)")
    exe = sub.add_parser("execute", help="carry out the approved plan with the approved "
                                         "design's nodes (starts, or carries on)")
    exe.add_argument("--run", required=True, help="the run folder its bootstrap made")
    exe.add_argument("--role", action="append", default=[], metavar="ROLE=PROVIDER[:MODEL[:EFFORT]]",
                     help="which model a role uses: " + ", ".join(exec_config.ROLES)
                          + " (all=... sets every one); the discussor is the bootstrap's")
    exe.add_argument("--wall-hours", type=float, help="stop after this many hours (0: no limit)")
    exe.add_argument("--gpu-hours", type=float, help="stop after this many GPU-hours (0: no limit)")
    exe.add_argument("--confirm", choices=exec_config.CONFIRM,
                     help="which steps wait for your go: gated (the plan's), all, none")
    exe.add_argument("--progress", action="store_true",
                     help="print what each model does while it works: commands, files")
    exe.add_argument("--trace", action="store_true",
                     help="print every model call's full input and output (implies --progress)")
    web = sub.add_parser("web", help="the web UI: every run in the runs folder, its conversation, "
                                     "research state, graph and files")
    web.add_argument("--runs", "--sessions-root", dest="runs", default=str(RUNS_ROOT),
                     help=f"the runs folder (default {RUNS_ROOT}); --sessions-root is v1's name for it")
    web.add_argument("--host", default="127.0.0.1",
                     help="where to listen (default 127.0.0.1; there is no authentication)")
    web.add_argument("--port", type=int, default=8421)
    web.add_argument("--role", action="append", default=[], metavar="ROLE=PROVIDER[:MODEL[:EFFORT]]",
                     help="override a role for every run opened (all=fake: a free demo)")
    ref = sub.add_parser("refine", help="improve a role's instructions from its calls in past "
                                        "runs: replay them, propose, compose, judge blindly, "
                                        "adopt the winner")
    ref.add_argument("--role", required=True,
                     choices=["designer", "orchestrator", "exec_orchestrator"])
    ref.add_argument("--runs", nargs="+", required=True, metavar="RUN",
                     help="past run folders (or names in --root) to learn from")
    ref.add_argument("--root", default=str(RUNS_ROOT),
                     help=f"the runs folder names are looked up in (default {RUNS_ROOT})")
    ref.add_argument("--cases", type=int, default=4, help="cases to use (default 4)")
    ref.add_argument("--k", type=int, default=1, help="replays per case and candidate")
    ref.add_argument("--model", default="",
                     help="replay on this provider[:model[:effort]] instead of each run's")
    ref.add_argument("--refiner", default="",
                     help="the proposers' and judge's provider[:model[:effort]] "
                          "(default: [roles.refiner] in bootstrap/config.toml)")
    ref.add_argument("--budget", type=int, default=40, help="most model calls allowed (40)")
    ref.add_argument("--no-adopt", action="store_true",
                     help="report only; do not write a winning prompt")
    ref.add_argument("--dry-run", action="store_true",
                     help="list the cases that would be used and the calls, then stop")
    args = parser.parse_args(argv)

    if args.command == "refine":
        return _refine(parser, args)
    if args.command == "web":
        try:
            from research_agent.web import server
        except ImportError as exc:
            parser.error(f"the web UI needs fastapi and uvicorn: pip install fastapi "
                         f"'uvicorn[standard]' ({exc})")
        server.serve(Path(args.runs), host=args.host, port=args.port, roles=args.role)
        return 0
    if args.command == "execute":
        return _execute(parser, args)
    if args.resume:
        folder = Path(args.resume).expanduser()
        if not (folder / "checkpoint.sqlite").exists():
            parser.error(f"{folder} is not a run folder (no checkpoint.sqlite).")
    elif args.start_from:
        old = Path(args.start_from).expanduser().resolve()
        folder = Path(args.run).expanduser() if args.run else carry.next_folder(old)
        if (folder / "checkpoint.sqlite").exists():
            parser.error(f"{folder} has already started; --resume it.")
        try:
            carry.start_from(old, folder)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Starting from {old}: its research state and review are carried into {folder}.")
        args.idea = args.idea or carry.first_message(old)
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
        words = words or (["design"] if S.version(st, "design") and not S.approved(st, "design")
                          else ["brief"])
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
    elif node in ("researcher", "designer"):
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


# ---- the execution stage ---------------------------------------------------------------------

EXEC_HELP = """\
Commands:
  /steps                 the step table: status, attempts, GPU-hours, last verdict
  /go                    let the step waiting for you go on
  /stop                  stop the execution: running jobs and steps
  /break [before|after] <step> [off]
                         stop for you before a step starts, or after it is checked
  /review                the reviewer's review (once every step is settled)
  /jobs                  every job: state, GPUs, hours
  /logs <job or step>    the last lines of a job's log (a step: its jobs')
  /budget                hours and GPU-hours used, and the limits
  /state, /show <section> ...   the research state, as in the bootstrap
  /sessions              every model call of this run
  /retry                 carry on from where it stopped (a failed call, Ctrl-C)
  /usage                 the providers' usage limits
  /trace on|off          print every model call's input and output
  /help                  this
  /exit                  leave (jobs keep running; carry on with execute --run)
Anything else is said to the discussor -- also while the team works: it
answers on the side, and the work goes on."""

#: Execution commands that only show things, allowed while a failed step waits.
EXEC_READ_ONLY = READ_ONLY + ("/steps", "/jobs", "/logs", "/budget", "/review")


def _execute(parser, args) -> int:
    folder = Path(args.run).expanduser()
    if not (folder / "state" / "research.json").exists():
        parser.error(f"{folder} is not a run folder (no state/research.json).")
    limits = {"wall_hours": args.wall_hours, "gpu_hours": args.gpu_hours,
              "confirm": args.confirm}
    try:
        cfg = exec_config.load(folder, roles=args.role, limits=limits)
        discussor = config.load(folder).roles["discussor"]
        run = exec_nodes.ExecRun.from_config(folder, cfg, discussor, progress=args.progress,
                                             trace=args.trace)
        ex = Execution(run)
    except ValueError as exc:
        parser.error(str(exc))
    exec_config.save(folder, cfg)
    return _converse_exec(ex, cfg)


class _Typed:
    """What the user types. With a terminal, it is read on a thread of its
    own, so the user can write while the team works: such a line is handled
    at once (`while_working`); others wait for the conversation (`next`)."""

    def __init__(self, while_working, concurrent: bool):
        self.while_working = while_working
        self.concurrent = concurrent
        self.busy = threading.Event()
        self.lines: queue.Queue = queue.Queue()
        if concurrent:
            threading.Thread(target=self._read, daemon=True).start()

    def next(self) -> str | None:
        """The next line for the conversation; None at the end of input."""
        if not self.concurrent:
            try:
                return input("you> ")
            except EOFError:
                return None
        return self.lines.get()

    def _read(self) -> None:
        while True:
            try:
                line = input("you> ")
            except EOFError:
                self.lines.put(None)
                return
            if self.busy.is_set():
                try:
                    self.while_working(line.strip())
                except Exception as exc:          # a thread must not die of it
                    print(f"! {exc}", flush=True)
            else:
                self.lines.put(line)


def _converse_exec(ex: Execution, cfg, *, concurrent: bool | None = None) -> int:
    folder = ex.run.folder
    resume_hint = f"Carry on with:  python -m research_agent execute --run {folder}"
    print(f"Run: {folder}")
    print("  saved as it goes: state/research.md, work/ (the code, a git repository), "
          "artifacts/step_NN/, jobs/ (records and logs), reviews/, rb/sessions/")
    print(f"Discussor={ex.run.roles['discussor']} (the bootstrap's conversation)\n{cfg.describe()}")
    print("Nodes: " + ", ".join(f"{name} (steps {', '.join(n['steps'])}; "
                                f"{exec_nodes.access_for(n)} access)"
                                for name, n in ex.design.owners.items()))
    _warn_signed_out(ex)
    concurrent = sys.stdin.isatty() if concurrent is None else concurrent
    print("Type /help for commands." + (" You can write at any time, also while the team works."
                                        if concurrent else "") + "\n")
    typed = _Typed(lambda text: _while_working(ex, text), concurrent)
    failed = False

    def advance(action) -> bool:
        nonlocal failed
        typed.busy.set()
        try:
            turn = action()
        except RB.BackendCancelled:
            raise KeyboardInterrupt from None
        except RB.BackendError as exc:
            failed = True
            print(f"\n! {exc}\n  Nothing is lost: /retry carries on; /exit leaves.")
            return True
        finally:
            typed.busy.clear()
        failed = False
        if turn is None:
            return True
        _show_exec_turn(ex, turn)
        return turn.waiting

    try:
        if not ex.started:
            going = advance(lambda: ex.start(on_step=_show_exec_step))
        elif ex.waiting_for_user:
            last = ex.last_turn
            going = advance(lambda: last)
        else:
            print("Carrying on from where it stopped...")
            going = advance(lambda: ex.resume(on_step=_show_exec_step))
        while going:
            text = typed.next()
            if text is None:
                print(f"\nStopped. {resume_hint}")
                return 0
            text = text.strip()
            if not text:
                continue
            command = text.split()[0] if text.startswith("/") else ""
            if command == "/retry":
                going = advance(lambda: ex.resume(on_step=_show_exec_step)) if failed \
                    else (print("Nothing to retry.") or True)
            elif failed and command not in EXEC_READ_ONLY + ("/exit", "/break"):
                print("The last step failed: /retry it first (or /exit).")
            elif command:
                going = advance(lambda: _exec_command(ex, text))
            else:
                going = advance(lambda: ex.answer(text, on_step=_show_exec_step))
        return 0
    except KeyboardInterrupt:
        print(f"\nStopped; running jobs go on. {resume_hint}")
        return 130
    finally:
        ex.close()


def _while_working(ex: Execution, text: str) -> None:
    """A line typed while the team works: the work goes on."""
    if not text:
        return
    command = text.split()[0] if text.startswith("/") else ""
    if command == "/stop":
        stopped = ex.stop_now()
        print(f"Stopping: {stopped} running jobs stopped; the team stops at its next step.",
              flush=True)
    elif command in EXEC_READ_ONLY + ("/break",):
        _exec_command(ex, text)
    elif command:
        print(f"{command} waits until the team stops for you. Meanwhile: /steps, /jobs, /logs, "
              f"/budget, /break, /stop, or just ask.", flush=True)
    else:
        print("  > discussor: answering on the side; the team works on...", flush=True)
        out = ex.aside(text)
        print()
        for paragraph in out.reply.split("\n"):
            print(textwrap.fill(paragraph, 100) if paragraph.strip() else "")
        noted = ([f"breakpoint {'on' if b.on else 'off'} {b.when} step {b.step}"
                  for b in out.breakpoints] + [f"request: {r}" for r in out.requests]
                 + (["stop"] if out.stop else []))
        if noted:
            print(f"[for the team's next step: {'; '.join(noted)}]")
        print(flush=True)


def _exec_command(ex: Execution, text: str):
    name, *words = text[1:].split()
    folder = ex.run.folder
    st = S.Store(folder).read()        # the state as the team last wrote it
    if name == "help":
        print(EXEC_HELP)
    elif name == "steps":
        print(S.render(st, ["execution"]) or "(no steps yet)")
    elif name == "state":
        print(S.render(st))
    elif name == "show":
        unknown = [w for w in words if w not in S.SECTIONS]
        if not words or unknown:
            print(f"Show which? {', '.join(S.SECTIONS)}")
        else:
            print(S.render(st, words) or "(nothing there yet)")
    elif name == "go":
        if not ex.awaiting:
            print("Nothing is waiting for your go.")
        else:
            return ex.answer("", go=True, on_step=_show_exec_step)
    elif name == "stop":
        return ex.answer("", stop=True, on_step=_show_exec_step)
    elif name == "break":
        when = words[0] if words[:1] in (["before"], ["after"]) else "before"
        rest = words[1:] if words[:1] in (["before"], ["after"]) else words
        on = not (rest[:1] == ["off"])
        steps = [w for w in rest if w != "off"]
        known = {s["id"] for s in (st.get("plan") or {}).get("steps") or []}
        if len(steps) != 1 or steps[0] not in known:
            print("/break [before|after] <step> [off] -- e.g. /break after 4")
        else:
            exec_inbox.post(folder, "breakpoint", step=steps[0], when=when, on=on)
            print(f"Breakpoint {'set' if on else 'removed'}: {when} step {steps[0]} "
                  f"(from the team's next step).")
    elif name == "jobs":
        print("\n".join(exec_jobs.describe(j) for j in exec_jobs.records(folder)) or "(no jobs)")
    elif name == "logs":
        if not words:
            print("/logs <job id or step id>")
        else:
            found = [j for j in [exec_jobs.get(folder, words[0])] if j] \
                or exec_jobs.records(folder, words[0])
            for job in found:
                print(f"---- {exec_jobs.describe(job)}\n{exec_jobs.tail(job, 40)}")
            if not found:
                print(f"No job or step {words[0]} with jobs.")
    elif name == "budget":
        from research_agent.execute.graph import budget

        print(budget(ex.run, st)[0])
    elif name == "review":
        print(S.render(st, ["reviews"]) or "(no review yet: the reviewer reviews once every "
                                           "step has passed or been skipped)")
    elif name == "sessions":
        print(RB.list_sessions(home=ex.run.rb_home))
    elif name == "usage":
        print(RB.Listing(RB.usage(p) for p in sorted({c.provider for c in ex.run.roles.values()})))
    elif name == "trace":
        if words[:1] not in (["on"], ["off"]):
            print(f"/trace on or /trace off (it is {'on' if ex.run.trace else 'off'}).")
        else:
            ex.run.trace = words[0] == "on"
            print(f"Tracing is {words[0]}.")
    elif name == "exit":
        return ex.exit()
    else:
        print(f"No command /{name}. /help lists them.")
    return None


def _show_exec_step(node: str, update: dict) -> None:
    """One line per piece of work, as it finishes."""
    d = update.get("decision") or {}
    if node == "orchestrator":
        if d.get("action") not in (None, "ask_user"):
            print(f"  . orchestrator -> {d['action']} {d.get('step', '')}".rstrip()
                  + f": {_short(d.get('reason', ''))}", flush=True)
    elif node == "validator" or (node not in ("discussor", "human", "wait") and update.get("last")):
        print(f"  . {_short(update.get('last', '').splitlines()[0] if update.get('last') else '', 160)}",
              flush=True)
    elif node not in ("discussor", "human", "wait"):
        owner = node.removeprefix("node_")
        for sid, r in exec_nodes.records(update.get("research") or {}).items():
            if r.get("owner") == owner and r["status"] in ("waiting", "validating"):
                print(f"  . step {sid} ({owner}): {r['status']} -- {_short(r['summary'], 140)}",
                      flush=True)


def _show_exec_turn(ex: Execution, turn) -> None:
    if turn.reply:
        print()
        for paragraph in turn.reply.split("\n"):
            print(textwrap.fill(paragraph, 100) if paragraph.strip() else "")
        print()
    st = ex.research
    counts: dict[str, int] = {}
    for r in exec_nodes.records(st).values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    gpu = exec_jobs.gpu_hours(ex.run.folder)
    line = (f"[{(st.get('status') or {}).get('phase') or 'executing'} | "
            + ", ".join(f"{n} {s}" for s, n in counts.items()) + f" | {gpu:.2f} GPU-h")
    if ex.awaiting:
        line += f" | step {ex.awaiting} waits for your go (/go)"
    print(line + "]")
    if turn.outcome == "done":
        print(f"\nDone: every step passed or was skipped. Results: {ex.run.folder / 'artifacts'}")
    if turn.outcome == "exit":
        print(f"Left. The run is in {ex.run.folder}; running jobs go on.")


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


def _refine(parser, args) -> int:
    from research_agent.refine import cases as refine_cases
    from research_agent.refine import refine as refining

    root = Path(args.root).expanduser()
    runs = [Path(r).expanduser() if Path(r).expanduser().exists() else root / r for r in args.runs]
    missing = [str(r) for r in runs if not (r / "rb").is_dir()]
    if missing:
        parser.error(f"not run folders: {', '.join(missing)}")
    if args.dry_run:
        found = refine_cases.collect(runs, args.role, args.cases)
        for c in found:
            print(f"  {c.id}: {len(c.problems)} checks failed; "
                  f"{len(refine_cases.TROUBLE.findall(c.aftermath))} signs of trouble after")
        print(f"Up to {refining.estimate(len(found), args.k)} model calls "
              f"(budget {args.budget}).")
        return 0
    try:
        refining.refine(args.role, runs, n_cases=args.cases, k=args.k, model=args.model,
                        refiner=args.refiner, budget=args.budget, root=root,
                        adopt=not args.no_adopt)
    except ValueError as exc:
        print(f"Cannot refine: {exc}", file=sys.stderr)
        return 1
    return 0

