"""
WHAT:  Background jobs for the execution stage: launch a command on free GPUs,
       detached, and keep a record of it -- its GPUs, its log, its exit code --
       in <run>/jobs/.
WHY:   A training run lasts hours; a model's command cannot. A step's owner
       launches its long commands through this, says so, and its turn ends.
       The graph waits on the records (no model call), counts GPU-hours from
       them -- measured, not what a model says -- and wakes the owner when
       they finish. A run stopped and resumed finds its jobs again by pid.
CONCEPT: Standard library only, and runnable as a plain script, so a model in
       any folder can call it without research_agent installed:

    python <this file> --run RUN launch --step 5 --gpus 2 --name seed17 -- python train.py --seed 17
    python <this file> --run RUN status [--step 5]
    python <this file> --run RUN kill JOB_ID

`launch` prints the job's id; the command runs in the current folder with
CUDA_VISIBLE_DEVICES set to the GPUs it was given.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: A GPU with less memory in use than this (MiB), and not given to a running
#: job of this run, is free.
FREE_MIB = 1000


class JobError(RuntimeError):
    """A job that cannot be launched, said in words."""


def folder(run: str | os.PathLike) -> Path:
    path = Path(run).expanduser().resolve() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- launching --------------------------------------------------------------------------


def launch(run, step: str, command: str | list[str], *, gpus: int = 0, name: str = "",
           cwd: str | os.PathLike | None = None) -> dict:
    """Start `command` detached; return its record."""
    jobs = folder(run)
    text = command if isinstance(command, str) else shlex.join(command)
    if not text.strip():
        raise JobError("No command to launch.")
    chosen = pick_gpus(run, gpus) if gpus else []
    taken = len(list(jobs.glob(f"s{step}-*.json")))
    job_id = f"s{step}-{taken + 1:03d}"
    log, exit_file = jobs / f"{job_id}.log", jobs / f"{job_id}.exit"
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in chosen) if gpus else ""
    with open(log, "ab") as out:
        # The exit code goes to a file, so it is known after this process
        # (and the agent that launched it) is gone.
        proc = subprocess.Popen(
            ["/bin/sh", "-c", f"{text}\necho $? > {shlex.quote(str(exit_file))}"],
            cwd=str(cwd or os.getcwd()), env=env, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    record = {"id": job_id, "step": str(step), "name": name, "command": text,
              "cwd": str(cwd or os.getcwd()), "gpus": chosen, "pid": proc.pid,
              "log": str(log), "started": time.time(), "started_at": _iso(time.time())}
    _write(jobs / f"{job_id}.json", record)
    return record


def pick_gpus(run, count: int) -> list[int]:
    """`count` free GPUs: little memory in use, and not given to a running job
    of this run (it may not have allocated yet)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise JobError(f"Cannot see the GPUs (nvidia-smi: {exc}).") from exc
    reserved = {g for job in records(run) if status(job)["state"] == "running" for g in job["gpus"]}
    free = [int(index) for index, used in (line.split(",") for line in out.strip().splitlines())
            if int(used) < FREE_MIB and int(index) not in reserved]
    if len(free) < count:
        raise JobError(f"{count} GPUs asked for, {len(free)} free ({free}); "
                       f"in use by this run's jobs: {sorted(reserved) or 'none'}.")
    return free[:count]


# ---- looking at them --------------------------------------------------------------------


def records(run, step: str | None = None) -> list[dict]:
    jobs = folder(run)
    found = [json.loads(p.read_text()) for p in sorted(jobs.glob("s*-*.json"))]
    return [j for j in found if step is None or j["step"] == str(step)]


def get(run, job_id: str) -> dict | None:
    path = folder(run) / f"{job_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def status(job: dict) -> dict:
    """{state: running | finished | lost, exit, hours, gpu_hours}. `lost`: the
    process is gone without leaving an exit code (killed, or the machine
    restarted)."""
    exit_file = Path(job["log"]).with_suffix(".exit")
    if exit_file.exists():
        text = exit_file.read_text().strip()
        state, code, end = "finished", int(text) if text.lstrip("-").isdigit() else None, \
            exit_file.stat().st_mtime
    elif _alive(job["pid"]):
        state, code, end = "running", None, time.time()
    else:
        state, code, end = "lost", None, Path(job["log"]).stat().st_mtime \
            if Path(job["log"]).exists() else job["started"]
    hours = max(0.0, end - job["started"]) / 3600
    return {"state": state, "exit": code, "hours": hours, "gpu_hours": hours * len(job["gpus"])}


def gpu_hours(run, step: str | None = None) -> float:
    return sum(status(j)["gpu_hours"] for j in records(run, step))


def tail(job: dict, lines: int = 30) -> str:
    path = Path(job["log"])
    if not path.exists():
        return ""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 64_000))
        text = handle.read().decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def describe(job: dict) -> str:
    s = status(job)
    code = f", exit {s['exit']}" if s["exit"] is not None else ""
    gpus = f" on GPUs {','.join(map(str, job['gpus']))}" if job["gpus"] else ""
    return (f"{job['id']} [{s['state']}{code}] {job.get('name') or ''} {s['hours']:.2f} h"
            f"{gpus}, {s['gpu_hours']:.2f} GPU-h: {job['command'][:120]}")


def kill(run, job_id: str) -> bool:
    """Stop a job and everything it started. True if it was running."""
    job = get(run, job_id)
    if not job or status(job)["state"] != "running":
        return False
    try:
        os.killpg(job["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A finished child of this process stays a zombie until it is reaped.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _write(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


# ---- the command line -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobs.py", description=__doc__.split("CONCEPT")[0])
    parser.add_argument("--run", required=True, help="the run folder")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("launch", help="start a command detached; prints the job id")
    go.add_argument("--step", required=True)
    go.add_argument("--gpus", type=int, default=0, help="how many free GPUs it needs")
    go.add_argument("--name", default="")
    go.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command")
    look = sub.add_parser("status", help="every job, or one step's")
    look.add_argument("--step")
    stop = sub.add_parser("kill", help="stop a job")
    stop.add_argument("job_id")
    args = parser.parse_args(argv)

    if args.command == "launch":
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        try:
            job = launch(args.run, args.step, command, gpus=args.gpus, name=args.name)
        except JobError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(job["id"])
        print(f"log: {job['log']}  gpus: {job['gpus'] or 'none'}", file=sys.stderr)
    elif args.command == "status":
        for job in records(args.run, args.step):
            print(describe(job))
    else:
        print("killed" if kill(args.run, args.job_id) else "not running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
