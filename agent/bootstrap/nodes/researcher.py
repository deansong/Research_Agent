"""
WHAT:  The research agent: answers the discussor's questions from the
       literature and the Hugging Face Hub, before the human is asked.
WHY:   The discussor suggests an answer for every question it asks -- which
       dataset, which baseline, how many seeds -- and without looking those
       up it guesses. Grounded suggestions are the ones a person can accept
       with /plan.
CONCEPT: A node between the discussor and itself:
           discussor --(research questions)--> researcher --> discussor --> human
       At most one round per human turn (the discussor that reads a report
       does not ask for another) and MAX_ROUNDS in all. A failed round is
       reported to the discussor and the discussion carries on: research is
       a help, not a gate.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent import activity
from agent.backends.base import Access, BackendError
from agent.bootstrap.prompts import RESEARCHER_INSTRUCTIONS
from agent.bootstrap.schemas import ResearchReport
from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section
from agent.telemetry import record_usage, usage_to_dict

#: Research rounds per session. Each is a few minutes of web searching.
MAX_ROUNDS = 3

#: Where reports go, inside the session folder (so the Files tab shows them).
REPORTS = "research"


def make_researcher(backend, paths, instructions: str = RESEARCHER_INSTRUCTIONS,
                    skills: str = ""):
    """`skills` is the text its skills choice renders to (agent/skills; by
    default the research skill: the paper and Hugging Face lookups it runs);
    `instructions` the persona. Both chosen per session -- see
    agent/bootstrap/settings.py."""

    def researcher(state: BootstrapState):
        research = dict(state.get("research") or {})
        questions = [q for q in research.get("questions") or [] if str(q).strip()]
        number = int(research.get("rounds", 0)) + 1
        print(f"\n[researcher] looking up {len(questions)} question(s)...")

        providers = dict(state.get("providers", {}))
        role_threads = dict(providers.get("role_threads", {}))
        thread_id = role_threads.get("researcher")
        out_dir = Path(paths.artifacts) / REPORTS
        prompt = (
            f"The human wants:\n\n{state.get('user_request', '')}\n\n"
            f"What the discussor has settled so far:\n"
            f"{(state.get('discussion') or {}).get('requirements') or '(nothing yet)'}\n\n"
            f"RESEARCH QUESTIONS (round {number}):\n"
            + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            + f"\n\nSave anything you download under {out_dir}; change nothing "
              f"else." + (f"\n\n{skills}" if skills else "")
        )

        sent = activity.exchange(thread_id=thread_id, instructions=instructions,
                                 prompt=prompt)
        activity.arm_progress(backend, state.get("session_dir", ""), "researcher",
                              sent=sent)
        started = time.monotonic()
        try:
            run = backend.run_structured(
                thread_id=thread_id,
                repo_path=state["repo_path"],
                # WRITE because antigravity has no read-only level and running
                # the paper lookups is a command; the session works in its own
                # workspace, and the persona keeps it out of the project.
                access=Access.WRITE,
                developer_instructions=instructions,
                prompt=prompt,
                output_model=ResearchReport,
            )
        except BackendError as exc:
            activity.disarm_progress(backend, state.get("session_dir", ""), "researcher")
            print(f"[researcher] failed: {type(exc).__name__}: {str(exc)[:300]}")
            report = {"round": number, "questions": questions, "failed": True,
                      "summary": f"The research agent failed ({type(exc).__name__}): "
                                 f"{str(exc)[:600]}",
                      "findings": [], "suggestions": "", "unanswered": "all of it"}
            return _done(state, research, report, providers, role_threads, None,
                         state.get("usage_by_role", {}))

        activity.disarm_progress(backend, state.get("session_dir", ""), "researcher")
        data = run.data
        activity.write(state.get("session_dir", ""), "researcher", run.events, sent=sent,
                       summary=activity.scalars(data),
                       usage=usage_to_dict(run.usage) if run.usage else None,
                       backend=backend, thread_id=run.thread_id,
                       output=data.model_dump(mode="json"))
        activity.record_visit(state.get("session_dir", ""), "researcher", kind="design",
                              provider=getattr(backend, "name", ""),
                              model=getattr(backend, "model", "") or "",
                              thread=run.thread_id,
                              seconds=round(time.monotonic() - started, 1))
        report = {"round": number, "questions": questions,
                  **data.model_dump(mode="json")}
        report["file"] = _save(paths, report)
        print(f"[researcher] {data.summary.strip()[:400]}")
        return _done(state, research, report, providers, role_threads, run.thread_id,
                     record_usage(state.get("usage_by_role", {}), "researcher", run.usage))

    return researcher


def _done(state, research, report, providers, role_threads, thread_id, usage) -> dict:
    if thread_id:
        role_threads["researcher"] = thread_id
    providers["role_threads"] = role_threads
    return {
        "transcript": [{"role": "researcher", "text": report_text(report)}],
        "providers": providers,
        "usage_by_role": usage,
        "research": merge_section(research, questions=[], rounds=report["round"],
                                  reports=list(research.get("reports") or []) + [report],
                                  fresh=True),
    }


def report_text(report: dict, *, limit: int | None = None) -> str:
    """A report as text -- for the transcript, the discussor's prompt and the
    file under the session's research/."""
    lines = [f"Research round {report.get('round')}: "
             f"{str(report.get('summary', '')).strip()}"]
    for finding in (report.get("findings") or [])[:limit]:
        lines.append(f"- {finding.get('finding', '').strip()} "
                     f"[{finding.get('source', '').strip()}]"
                     + (f" ({finding.get('bears_on').strip()})"
                        if finding.get("bears_on") else ""))
    if str(report.get("suggestions", "")).strip():
        lines.append(f"Suggested in light of this: {report['suggestions'].strip()}")
    if str(report.get("unanswered", "")).strip():
        lines.append(f"Not found: {report['unanswered'].strip()}")
    return "\n".join(lines)


def _save(paths, report: dict) -> str:
    """Write the round to <session>/research/round-N.md, for people to read."""
    try:
        folder = Path(paths.session) / REPORTS
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"round-{report['round']}.md"
        questions = "\n".join(f"{i}. {q}" for i, q in enumerate(report.get("questions") or [], 1))
        target.write_text(f"# Research round {report['round']}\n\n## Questions\n\n"
                          f"{questions}\n\n## Findings\n\n{report_text(report)}\n")
        return str(target)
    except OSError:
        return ""
