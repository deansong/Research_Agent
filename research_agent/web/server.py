"""
WHAT:  The web UI's server: FastAPI on uvicorn, as v1's. A JSON API over the
       runs in a runs folder, a Server-Sent Events stream per open run, and
       the static page.
WHY:   The terminal shows one thing at a time. The page shows the
       conversation, the research state, the graph and the files at once --
       and the discussor can be talked to while the team works.
CONCEPT: `python -m research_agent web` serves http://127.0.0.1:8421. There is
       no authentication: it binds to localhost; reach it from elsewhere over
       an SSH tunnel (ssh -N -L 8421:localhost:8421 <host>).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from research_agent import state as S
from research_agent.bootstrap import carry
from research_agent.execute import jobs as J
from research_agent.web import host as H

#: Where deleted runs go, inside the runs root: move one back to restore it.
TRASH = ".trash"

STATIC = Path(__file__).with_name("static")
RUNS_ROOT = Path.home() / "research_runs"
POLL_SECONDS = 0.25
PING_SECONDS = 15.0
#: Files the page shows as text, and how much of one.
TEXT_LIMIT = 2_000_000
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".py", ".sh", ".log", ".yaml", ".yml",
                 ".toml", ".csv", ".tsv", ".cfg", ".ini", ".exit", ".tex", ".html", ".js",
                 ".css", ""}

#: Files the page shows as themselves (the paper, figures). Not HTML or SVG:
#: they could run scripts on the page's origin.
RAW_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
_NOT_RUN_FILES = ("rb", "activity", "__pycache__")


def _papers(folder: Path, st: dict) -> list[dict]:
    """The run's own PDFs, not the references the researcher downloaded;
    those a writer's step lists as artifacts first (the paper), then the
    newest."""
    owners = {n["name"]: n.get("role") for n in (st.get("design") or {}).get("nodes") or []}
    by_writer: dict[Path, str] = {}
    for rec in (st.get("execution") or {}).get("steps") or []:
        if owners.get(rec.get("owner")) != "writer":
            continue
        for a in rec.get("artifacts") or []:
            # Owners name their files from their workspace (<run>/work).
            for path in (Path(os.path.normpath(folder / "work" / a)), folder / a):
                if path.suffix.lower() == ".pdf" and path.is_file():
                    by_writer[path.resolve()] = rec.get("id", "")
                    break
    found = []
    for path in folder.rglob("*.pdf"):
        rel = path.relative_to(folder)
        if rel.parts[0] in _NOT_RUN_FILES or ".git" in rel.parts or not path.is_file() \
                or rel.parts[:2] == ("research", "papers"):      # the researcher's references
            continue
        step = by_writer.get(path.resolve(), "")
        found.append({"path": rel.as_posix(), "size": path.stat().st_size,
                      "modified": path.stat().st_mtime, "step": step, "paper": bool(step)})
    main = (folder / "paper" / "main.pdf").resolve()
    for p in found:
        if (folder / p["path"]).resolve() == main:
            p.update(step="paper", paper=True, main=True)
    found.sort(key=lambda p: (not p.get("main"), not p["paper"], -p["modified"]))
    return found[:200]


# ---- request bodies -------------------------------------------------------------------


class NewRun(BaseModel):
    idea: str = ""
    start_from: str = ""
    """A run id (in the runs folder) or a folder: start the next run from it."""
    name: str = ""
    start: bool = True
    """False: create the run with its idea, and wait for Start -- so models,
    backups, skills and sources can be set first."""
    model_config = {"extra": "forbid"}


class Message(BaseModel):
    text: str
    model_config = {"extra": "forbid"}


class Approve(BaseModel):
    sections: list[str]
    model_config = {"extra": "forbid"}


class Breakpoint(BaseModel):
    step: str
    when: str = "before"
    on: bool = True
    model_config = {"extra": "forbid"}


class StepAction(BaseModel):
    step: str
    action: str
    why: str = ""
    model_config = {"extra": "forbid"}


class SetSkills(BaseModel):
    key: str
    mode: str
    skills: list[str] = []
    model_config = {"extra": "forbid"}


class Autopilot(BaseModel):
    on: bool
    max_decisions: int | None = None
    wall_hours: float | None = None
    gpu_hours: float | None = None
    model_config = {"extra": "forbid"}


class Sources(BaseModel):
    sources: list[str]
    model_config = {"extra": "forbid"}


class SetModel(BaseModel):
    group: str
    role: str
    provider: str
    model: str = ""
    effort: str = ""
    backup: list[str] | None = None
    """Backups in order, "provider[:model[:effort]]"; None keeps them."""
    model_config = {"extra": "forbid"}


class RefineStart(BaseModel):
    role: str
    runs: list[str]
    cases: int = Field(4, ge=1, le=12)
    k: int = Field(1, ge=1, le=3)
    model_config = {"extra": "forbid"}


class StartExecution(BaseModel):
    wall_hours: float | None = Field(None, ge=0)
    gpu_hours: float | None = Field(None, ge=0)
    confirm: str | None = None
    model_config = {"extra": "forbid"}


# ---- the app ------------------------------------------------------------------------------


def create_app(runs_root: Path = RUNS_ROOT, *, roles=(), watch: bool = True) -> FastAPI:
    """`roles`: --role overrides for every run this server opens (all=fake
    for a free demo or tests)."""
    root = Path(runs_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    hosts: dict[str, H.RunHost] = {}
    lock = threading.Lock()
    app = FastAPI(title="research agent")
    app.state.hosts = hosts

    def folder_of(run_id: str) -> Path:
        if not re.fullmatch(r"[\w.\-]+", run_id or "") or run_id.startswith("."):
            raise _bad(404, "no_run", f"No run {run_id!r}.")
        folder = root / run_id
        if not (folder / "state" / "research.json").exists():
            raise _bad(404, "no_run", f"No run {run_id!r} in {root}.")
        return folder

    def host_of(run_id: str) -> H.RunHost:
        with lock:
            if run_id not in hosts:
                try:
                    hosts[run_id] = H.RunHost(folder_of(run_id), roles=roles, watch=watch)
                except ValueError as exc:
                    raise _bad(400, "cannot_open", str(exc))
            return hosts[run_id]

    def act(fn):
        try:
            return fn()
        except H.Busy as exc:
            raise _bad(409, "busy", str(exc))
        except ValueError as exc:
            raise _bad(400, "bad_request", str(exc))

    # ---- runs ---------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "runs_root": str(root), "open": sorted(hosts)}

    @app.get("/api/runs")
    def list_runs():
        found = [H.run_summary(f, hosts) for f in root.iterdir() if f.is_dir()]
        return sorted((r for r in found if r), key=lambda r: -r["updated"])

    @app.post("/api/runs")
    def new_run(body: NewRun):
        if body.start_from:
            old = Path(body.start_from).expanduser()
            old = old if old.is_absolute() else root / body.start_from
            folder = root / body.name if body.name else carry.next_folder(old)
            try:
                carry.start_from(old, folder)
            except ValueError as exc:
                raise _bad(400, "bad_run", str(exc))
            idea = body.idea.strip() or carry.first_message(old)
        else:
            idea = body.idea.strip()
            if not idea:
                raise _bad(400, "no_idea", "Give the idea to start from.")
            slug = "-".join(re.findall(r"[a-z0-9]+", idea.lower())[:6]) or "run"
            folder = root / (body.name or f"{datetime.now():%Y%m%d-%H%M}-{slug}")
            if folder.exists():
                raise _bad(409, "exists", f"{folder} exists already.")
            S.Store(folder).write(S.new())
        host = host_of(folder.name)
        if body.start:
            act(lambda: host.message(idea))
        else:
            host.hold(idea)
        return host.detail()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        host = host_of(run_id)
        return {**host.detail(), "transcript": H.transcript(host.folder),
                "last_seq": host.bus.last_seq}

    @app.delete("/api/runs/{run_id}")
    def close_run(run_id: str):
        with lock:
            host = hosts.get(run_id)
            if host:
                act(host.close)
                del hosts[run_id]
        return {"ok": True}

    @app.post("/api/runs/{run_id}/trash")
    def trash_run(run_id: str):
        """Delete a run: its folder moves to <runs root>/.trash, from where it
        can be moved back. Not while it works or its jobs still run."""
        folder = folder_of(run_id)
        running = [j["id"] for j in J.records(folder) if J.status(j)["state"] == "running"]
        if running:
            raise _bad(409, "busy", f"Jobs still run ({', '.join(running)}): stop the run first.")
        with lock:
            host = hosts.get(run_id)
            if host:
                act(host.close)
                del hosts[run_id]
        bin_ = root / TRASH
        bin_.mkdir(exist_ok=True)
        to = bin_ / f"{run_id}-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.move(str(folder), str(to))
        return {"ok": True, "moved_to": str(to)}

    # ---- refining the roles' prompts (research_agent/refine) --------------------------

    refining: dict = {"thread": None, "lines": [], "report": None, "error": ""}
    # --role all=X (a demo, the tests) runs the refinement on X too.
    every = next((r.split("=", 1)[1] for r in roles if r.startswith("all=")), "")

    @app.get("/api/refine")
    def refine_status():
        t = refining["thread"]
        return {"running": bool(t and t.is_alive()), "lines": refining["lines"][-200:],
                "report": refining["report"], "error": refining["error"]}

    @app.post("/api/refine")
    def refine_start(body: RefineStart):
        from research_agent.refine import refine as refining_mod

        if refining["thread"] and refining["thread"].is_alive():
            raise _bad(409, "busy", "A refinement is running already.")
        if body.role not in refining_mod.PROMPTS:
            raise _bad(400, "bad_request", f"No role {body.role!r} to refine.")
        runs = [folder_of(r) for r in body.runs]
        if not runs:
            raise _bad(400, "bad_request", "Choose the past runs to learn from.")
        refining.update(lines=[], report=None, error="")

        def work():
            try:
                r = refining_mod.refine(
                    body.role, runs, n_cases=body.cases, k=body.k, model=every, refiner=every,
                    budget=max(40, refining_mod.estimate(body.cases, body.k)), root=root,
                    say=refining["lines"].append)
                refining["report"] = {"winner": r.winner, "wins": r.wins, "adopted": r.adopted,
                                      "backup": r.backup, "folder": str(r.folder),
                                      "report": str(r.folder / "report.md")}
            except Exception as exc:  # noqa: BLE001 -- shown on the page
                refining["error"] = str(exc)[:2000]

        refining["thread"] = threading.Thread(target=work, daemon=True, name="refine")
        refining["thread"].start()
        return {"ok": True}

    # ---- talking and steering -------------------------------------------------------

    @app.post("/api/runs/{run_id}/message")
    def message(run_id: str, body: Message):
        host = host_of(run_id)
        return {"ok": True, "as": act(lambda: host.message(body.text))}

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: Approve):
        host = host_of(run_id)
        act(lambda: host.approve(body.sections))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/go")
    def go(run_id: str):
        act(host_of(run_id).go)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str):
        return {"ok": True, "as": act(host_of(run_id).stop)}

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str):
        return {"ok": True, "as": act(host_of(run_id).pause)}

    @app.post("/api/runs/{run_id}/start")
    def start(run_id: str):
        """Start a run created with start=false, from its idea."""
        act(host_of(run_id).start_held)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        act(host_of(run_id).resume)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/break")
    def set_break(run_id: str, body: Breakpoint):
        act(lambda: host_of(run_id).breakpoint(body.step, body.when, body.on))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/step")
    def step_action(run_id: str, body: StepAction):
        host = host_of(run_id)
        return {"ok": True, "when": act(lambda: host.step_action(body.step, body.action, body.why))}

    @app.post("/api/runs/{run_id}/review")
    def review(run_id: str):
        act(host_of(run_id).review)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/paper")
    def write_paper(run_id: str):
        act(lambda: host_of(run_id).review(paper=True))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/execute")
    def execute(run_id: str, body: StartExecution):
        host = host_of(run_id)
        limits = {k: v for k, v in body.model_dump().items() if v is not None}
        act(lambda: host.execute(limits=limits))
        return host.detail()

    # ---- the research state -------------------------------------------------------

    @app.get("/api/providers")
    def providers(refresh: bool = False):
        """Each provider's models and effort levels, for the pickers (cached
        ten minutes; refresh=true asks the providers again)."""
        return _providers(refresh)

    @app.get("/api/runs/{run_id}/nodes/{node}")
    def node_info(run_id: str, node: str):
        return act(lambda: host_of(run_id).node_info(node))

    @app.get("/api/runs/{run_id}/skills")
    def skills_catalogue(run_id: str):
        """The skills in the run's skills folder: name and description."""
        return {"skills": act(host_of(run_id).catalogue)}

    @app.put("/api/runs/{run_id}/nodes/{node}/skills")
    def set_node_skills(run_id: str, node: str, body: SetSkills):
        host = host_of(run_id)
        return act(lambda: host.set_skills(node, body.key, body.mode, body.skills))

    @app.get("/api/runs/{run_id}/autopilot")
    def get_autopilot(run_id: str):
        """Autopilot: on or off, decisions in a row allowed, the limits."""
        return act(host_of(run_id).autopilot_settings)

    @app.put("/api/runs/{run_id}/autopilot")
    def put_autopilot(run_id: str, body: Autopilot):
        host = host_of(run_id)
        return act(lambda: host.set_autopilot(body.on, max_decisions=body.max_decisions,
                                              wall_hours=body.wall_hours, gpu_hours=body.gpu_hours))

    @app.get("/api/runs/{run_id}/research")
    def research(run_id: str):
        """The literature sources the research may search, and which have keys."""
        return act(host_of(run_id).research_sources)

    @app.put("/api/runs/{run_id}/research")
    def set_research(run_id: str, body: Sources):
        host = host_of(run_id)
        return act(lambda: host.set_research_sources(body.sources))

    @app.get("/api/runs/{run_id}/models")
    def models(run_id: str):
        return act(host_of(run_id).models)

    @app.put("/api/runs/{run_id}/models")
    def set_model(run_id: str, body: SetModel):
        host = host_of(run_id)
        return act(lambda: host.set_model(body.group, body.role, body.provider, body.model,
                                          body.effort, body.backup))

    @app.get("/api/runs/{run_id}/state")
    def get_state(run_id: str):
        st = S.Store(folder_of(run_id)).read()
        return {"state": st, "sections": list(S.SECTIONS), "approvable": list(S.APPROVABLE),
                "parts": ["brief", *S.PARTS],
                "approved": {s: S.approved(st, s) for s in ("brief", *S.PARTS)},
                "markdown": S.render(st)}

    @app.get("/api/runs/{run_id}/history")
    def history(run_id: str):
        folder = folder_of(run_id) / "state" / "history"
        out = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                h = json.loads(path.read_text())
            except ValueError:
                continue
            out.append({"n": int(path.stem), "at": h.get("at"), "by": h.get("by"),
                        "changed": h.get("changed") or [], "ops": len(h.get("ops") or [])})
        return out

    @app.get("/api/runs/{run_id}/history/{n}")
    def history_entry(run_id: str, n: int):
        path = folder_of(run_id) / "state" / "history" / f"{n:04d}.json"
        if not path.exists():
            raise _bad(404, "no_entry", f"No change {n}.")
        return json.loads(path.read_text())

    @app.get("/api/runs/{run_id}/graph")
    def graph_view(run_id: str, view: str = ""):
        """The graph of the run's stage -- or, with view=execution, the
        execution graph the design builds, before the execution starts."""
        st = S.Store(folder_of(run_id)).read()
        stage = host_of(run_id).stage
        if view == "execution" and (st.get("design") or {}).get("nodes"):
            stage = "execution"
        out = _graph(st, stage)
        out["can_preview"] = bool((st.get("design") or {}).get("nodes"))
        out["preview"] = stage != host_of(run_id).stage
        return out

    # ---- model calls, files, jobs ---------------------------------------------------

    @app.get("/api/runs/{run_id}/sessions")
    def sessions(run_id: str):
        folder = folder_of(run_id) / "rb" / "sessions"
        out = []
        for path in sorted(folder.glob("*/session.json")) if folder.is_dir() else []:
            try:
                rec = json.loads(path.read_text())
            except ValueError:
                continue
            out.append({"id": rec.get("id"), "provider": rec.get("provider"),
                        "model": rec.get("model"), "turns": len(rec.get("turns") or []),
                        "updated": rec.get("updated_at") or rec.get("created_at")})
        return sorted(out, key=lambda s: s["updated"] or "", reverse=True)

    @app.get("/api/runs/{run_id}/sessions/{session_id}")
    def session(run_id: str, session_id: str):
        if not re.fullmatch(r"[\w.\-]+", session_id):
            raise _bad(404, "no_session", f"No session {session_id!r}.")
        path = folder_of(run_id) / "rb" / "sessions" / session_id / "session.json"
        if not path.exists():
            raise _bad(404, "no_session", f"No session {session_id!r}.")
        rec = json.loads(path.read_text())
        return {k: rec.get(k) for k in ("id", "provider", "model", "effort", "access", "skills",
                                        "instructions", "turns", "workdir")}

    @app.get("/api/runs/{run_id}/live")
    def live(run_id: str, node: str = ""):
        """The calls under way -- of one graph node, or all -- as they are now."""
        return {"node": node, "running": host_of(run_id).running(node)}

    @app.get("/api/runs/{run_id}/activity")
    def node_activity(run_id: str, node: str = "", limit: int = 30):
        """A node's finished calls, newest first, with every event."""
        return {"node": node, "calls": H.activity(folder_of(run_id), node, max(1, min(limit, 200)))}

    @app.get("/api/runs/{run_id}/files")
    def files(run_id: str):
        folder = folder_of(run_id)
        out = []
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if not path.is_file() or rel.parts[0] in ("rb", "activity", "__pycache__") \
                    or ".git" in rel.parts or rel.name.startswith(".") \
                    or rel.name.startswith("checkpoint.sqlite") or rel.suffix in (".lock", ".tmp"):
                continue
            out.append({"path": rel.as_posix(), "size": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                        "readable": path.suffix.lower() in TEXT_SUFFIXES
                        and path.stat().st_size <= TEXT_LIMIT})
            if len(out) >= 3000:
                break
        return {"files": out}

    @app.get("/api/runs/{run_id}/file")
    def file(run_id: str, path: str):
        folder = folder_of(run_id)
        target = (folder / path).resolve()
        if not target.is_relative_to(folder) or not target.is_file():
            raise _bad(404, "no_file", f"No file {path!r} in the run.")
        if target.suffix.lower() not in TEXT_SUFFIXES or target.stat().st_size > TEXT_LIMIT:
            return {"path": path, "size": target.stat().st_size, "text": None,
                    "skipped": "not a text file, or too big to show"}
        return {"path": path, "size": target.stat().st_size,
                "text": target.read_text(errors="replace"), "skipped": ""}

    @app.get("/api/runs/{run_id}/papers")
    def papers(run_id: str):
        """The run's PDFs, the writers' first, newest first."""
        folder = folder_of(run_id)
        return {"papers": _papers(folder, S.Store(folder).read())}

    @app.get("/api/runs/{run_id}/raw")
    def raw(run_id: str, path: str):
        """A PDF or an image of the run, as itself, for the browser to show."""
        folder = folder_of(run_id)
        target = (folder / path).resolve()
        if not target.is_relative_to(folder) or not target.is_file():
            raise _bad(404, "no_file", f"No file {path!r} in the run.")
        kind = RAW_TYPES.get(target.suffix.lower())
        if not kind:
            raise _bad(415, "not_shown", f"{path!r} is not a PDF or an image.")
        return FileResponse(target, media_type=kind, content_disposition_type="inline",
                            filename=target.name, headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs/{run_id}/jobs")
    def list_jobs(run_id: str):
        folder = folder_of(run_id)
        if not (folder / "jobs").is_dir():
            return []
        return [{**j, **J.status(j)} for j in J.records(folder)]

    @app.get("/api/runs/{run_id}/jobs/{job_id}/log")
    def job_log(run_id: str, job_id: str, lines: int = 80):
        job = J.get(folder_of(run_id), job_id) if re.fullmatch(r"[\w\-]+", job_id) else None
        if job is None:
            raise _bad(404, "no_job", f"No job {job_id!r}.")
        return {"id": job_id, "text": J.tail(job, max(1, min(lines, 2000)))}

    # ---- the live feed ----------------------------------------------------------------

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after_seq: int = 0, follow: bool = True):
        host = host_of(run_id)
        header = request.headers.get("last-event-id", "")
        start = max(after_seq, int(header) if header.isdigit() else 0)

        async def stream():
            sent = start
            yield f": stream open at seq {host.bus.last_seq}\n\n"
            if sent and sent < host.bus.first_seq - 1:
                yield ": history truncated; reload the page for the full run\n\n"
                sent = 0
            quiet = 0.0
            while True:
                for event in host.bus.replay(sent):
                    yield event.sse()
                    sent = event.seq
                    quiet = 0.0
                if not follow or await request.is_disconnected():
                    return
                await asyncio.sleep(POLL_SECONDS)
                quiet += POLL_SECONDS
                if quiet >= PING_SECONDS:
                    quiet = 0.0
                    yield ": ping\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no"})

    # ---- the page ---------------------------------------------------------------------

    @app.get("/node/{run_id}/{node}")
    def node_window(run_id: str, node: str):
        folder_of(run_id)
        return FileResponse(STATIC / "node.html", headers={"Cache-Control": "no-store"})

    @app.get("/paper/{run_id}")
    def paper_window(run_id: str):
        folder_of(run_id)
        return FileResponse(STATIC / "paper.html", headers={"Cache-Control": "no-store"})

    @app.get("/state/{run_id}")
    def state_window(run_id: str):
        folder_of(run_id)
        return FileResponse(STATIC / "state.html", headers={"Cache-Control": "no-store"})

    app.mount("/", _NoCacheStatic(directory=STATIC, html=True), name="static")
    return app


class _NoCacheStatic(StaticFiles):
    """No build step and no hashed names: never let the browser keep an old file."""

    def is_not_modified(self, *args, **kwargs) -> bool:
        return False

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


def _bad(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ---- the graph, for drawing ---------------------------------------------------------------


def _graph(st: dict, stage: str) -> dict:
    """Nodes and edges of the stage's graph, with what each node is doing."""
    nodes = [{"id": "human", "label": "you", "kind": "human"},
             {"id": "discussor", "label": "discussor", "kind": "discussor"},
             {"id": "orchestrator", "label": "orchestrator", "kind": "orchestrator"}]
    edges = [("human", "discussor", ""), ("discussor", "human", ""),
             ("discussor", "orchestrator", "request / go"), ("orchestrator", "discussor", "ask")]
    if stage == "bootstrap":
        for name, label in (("researcher", "research"), ("designer", "design")):
            nodes.append({"id": name, "label": name, "kind": "worker"})
            edges += [("orchestrator", name, label), (name, "orchestrator", "")]
        return {"stage": stage, "nodes": nodes,
                "edges": [{"source": a, "target": b, "label": c} for a, b, c in edges]}
    records = {r["id"]: r for r in (st.get("execution") or {}).get("steps") or []}
    for n in (st.get("design") or {}).get("nodes") or []:
        if n["role"] == "validator" or (n["role"] == "orchestrator" and not n.get("steps")):
            continue
        name = f"node_{n['name']}" if n["name"] in ("discussor", "orchestrator", "validator",
                                                    "wait", "human", "paper", "reviewer") \
            else n["name"]
        statuses = [records.get(s, {}).get("status", "pending") for s in n.get("steps") or []]
        nodes.append({"id": name, "label": n["name"],
                      "kind": "worker" if n["role"] == "orchestrator" else n["role"],
                      "detail": f"steps {', '.join(n.get('steps') or [])}",
                      "status": _overall(statuses)})
        edges += [("orchestrator", name, "run " + ",".join(n.get("steps") or [])),
                  (name, "validator", "done"), (name, "wait", "jobs")]
    nodes += [{"id": "wait", "label": "wait (jobs)", "kind": "code"},
              {"id": "validator", "label": "validator", "kind": "validator"},
              {"id": "paper", "label": "paper", "kind": "writer"},
              {"id": "reviewer", "label": "reviewer", "kind": "reviewer"}]
    edges += [("validator", "orchestrator", "verdict"), ("orchestrator", "paper", "done"),
              ("paper", "reviewer", "paper.pdf"),
              ("reviewer", "discussor", "review")]
    return {"stage": stage, "nodes": nodes,
            "edges": [{"source": a, "target": b, "label": c} for a, b, c in edges]}


_PROVIDERS: dict = {"at": 0.0, "value": None}


def _providers(refresh: bool = False) -> list[dict]:
    """research_backend's providers with their models and efforts, asked at
    most every ten minutes (Codex's list is a call to its app server)."""
    import time

    import research_backend as RB

    if not refresh and _PROVIDERS["value"] is not None and time.time() - _PROVIDERS["at"] < 600:
        return _PROVIDERS["value"]
    out = []
    for name in RB.list_providers():
        if name == "fake":
            continue
        entry = {"provider": name, "models": [], "efforts": [], "model_efforts": {},
                 "default": "", "error": ""}
        try:
            listed = list(RB.list_models(name, refresh=refresh))
            entry["models"] = [m.id for m in listed]
            # Codex's levels differ by model (one has no "ultra").
            entry["model_efforts"] = {m.id: list(m.efforts) for m in listed if m.efforts}
            entry["default"] = next((m.id for m in listed if m.default), "")
            entry["efforts"] = list(RB.ResearchBackend(name).list_efforts(name))
        except Exception as exc:                 # a provider not set up here
            entry["error"] = str(exc)[:200]
        out.append(entry)
    _PROVIDERS.update(at=time.time(), value=out)
    return out


def _overall(statuses: list[str]) -> str:
    for s in ("running", "waiting", "validating", "fail", "stopped"):
        if s in statuses:
            return s
    if statuses and all(s in ("pass", "skipped") for s in statuses):
        return "pass"
    return "pending"


def serve(runs_root: Path = RUNS_ROOT, *, host: str = "127.0.0.1", port: int = 8421,
          roles=()) -> None:
    import uvicorn

    app = create_app(runs_root, roles=roles)
    print(f"Research agent UI: http://{host}:{port}  (runs in {Path(runs_root).expanduser()})")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("! There is no authentication: anyone who can reach this port can drive your runs.")
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=5)
