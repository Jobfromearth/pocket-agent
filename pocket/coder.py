"""Delegating code — the one sub-task that leaves this process, and why.

`delegate` (subagent.py) hands a sub-task to one more `run_loop` with a smaller
registry. It can do anything this assistant can do, which is the limit: pocket
has no file tools and no shell, so a task that has to read a repository, edit
three files and run the tests has nowhere to go.

Two ways out of that. Build a filesystem and a shell — and then owe a sandbox,
because the moment a tool executes an arbitrary string a deny list stops being
enough. Or hand the job to an agent that already has all of it. This file is the
second one, and the reasoning is worth stating plainly: a coding agent is a
serious piece of software, it is not this repo's argument, and rewriting it
badly would cost the thing this repo is actually for.

    pi        the default (github.com/earendil-works/pi): read / bash / edit /
              write, a JSON event stream, and a headless `-p` mode
    anything  POCKET_CODER is the whole command, with {task} where the
              instruction goes. `claude -p {task}`, `codex exec {task}`, your
              own script — the workspace, the manifest and the gate are ours
              either way, and which binary runs is a config line

A delegated run is the slowest thing this assistant can start, so its output is
read as it arrives rather than at the end: every line becomes a `coder_progress`
event on the same bus everything else uses, and the terminal and the dashboard
show it moving. That is deliberately NOT asynchrony — the turn still waits, and
the loop still has exactly two exits. It is the difference between "this is
taking a while" and "this has died", which is most of what asynchrony was going
to buy.

A run that goes silent is a different problem, and the timeout is a watchdog
thread rather than a check between lines: a process that prints nothing would
never reach a check between lines.

What is NOT claimed: this is not a sandbox. The delegate runs as you, with your
files, in the directory it was given. The gate is `risk="ask"` and a human
reading the task before it starts. That is the same bargain waku makes with the
same tool, and saying so is better than a sandbox nobody implemented.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pocket.tools import Tool

DEFAULT_COMMAND = "pi -p {task} -a --no-session --mode json"
TIMEOUT = 900.0
REPLY_PREVIEW = 1500


def command_template() -> list[str]:
    """Split first, substitute after. Building a shell string around a
    model-written task and splitting THAT is how you get argument injection."""
    return shlex.split(os.getenv("POCKET_CODER", DEFAULT_COMMAND))


def argv(task: str) -> list[str]:
    return [task if part == "{task}" else part.replace("{task}", task)
            for part in command_template()]


def run_command(args: list[str], cwd: Path, timeout: float,
                on_line: Callable[[str], None] | None = None) -> tuple[int, str, str]:
    """The seam the eval suite replaces, so the suite never needs pi installed.

    stdout is read line by line so a long run can report progress while it runs.
    The timeout is a watchdog thread and not a check between lines, because a
    process that has hung prints nothing and would never reach such a check —
    which is exactly the case the timeout exists for."""
    process = subprocess.Popen(
        args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, bufsize=1)
    expired: list[bool] = []
    watchdog = threading.Timer(timeout, lambda: (expired.append(True), process.kill()))
    watchdog.start()
    lines: list[str] = []
    try:
        for line in process.stdout:
            lines.append(line)
            if on_line:
                on_line(line.rstrip())
        code = process.wait()
        stderr = process.stderr.read() or ""
    finally:
        watchdog.cancel()
        process.stdout.close()
        process.stderr.close()
    if expired:
        raise subprocess.TimeoutExpired(args, timeout)
    return code, "".join(lines), stderr


def progress(line: str) -> dict | None:
    """One line of a coder's stream, reduced to something worth putting on the
    bus. A raw event stream is for `events.jsonl`; a human wants to know it is
    alive and roughly what it is doing."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {"note": line[:160]}
    if not isinstance(event, dict):
        return None
    detail = event.get("tool") or event.get("name") or event.get("status") or ""
    return {"event": str(event.get("type") or "event"), "detail": str(detail)[:120]}


def read_events(stdout: str) -> tuple[str, list[dict]]:
    """pi's `--mode json` is one JSON object per line. Anything that does not
    parse is a plain-mode agent talking, and its stdout IS the reply — so a
    coder that ignores the flag still works instead of coming back empty."""
    events, text = [], []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            text.append(line)
            continue
        events.append(event)
        if event.get("type") in ("message_update", "text", "content_block_delta"):
            text.append(str(event.get("text") or event.get("delta") or ""))
        elif event.get("type") == "message_end" and event.get("text"):
            text.append(str(event["text"]))
    return "".join(text) if events else "\n".join(text), events


def new_workspace(home: Path, task: str) -> Path:
    """A dated folder per run, not a temp dir: the point of delegating a coding
    task is the files it leaves behind, and a temp dir throws them away."""
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-") or "task"
    folder = home / "workspace" / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_manifest(folder: Path, **fields) -> Path:
    """What ran, where, and what came of it. A delegated run you cannot read
    afterwards is a delegated run you cannot trust."""
    path = folder / "manifest.json"
    path.write_text(json.dumps(fields, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return path


def make_coder_tool(home: Path, runner: Callable[..., tuple[int, str, str]] = run_command,
                    timeout: float = TIMEOUT, notify=None) -> Tool:
    say = notify or (lambda kind, event: None)

    def delegate_task(task: str, cwd: str = "") -> str:
        args = argv(task)
        if not args:
            return "Error: POCKET_CODER is empty, so there is no coding agent to call."
        workspace = Path(cwd).expanduser() if cwd else new_workspace(home, task)
        if not workspace.is_dir():
            return f"Error: '{workspace}' is not a directory."
        before = {p for p in workspace.rglob("*") if p.is_file()}
        started = datetime.now()
        seen = [0]

        def on_line(line: str) -> None:
            step = progress(line)
            if step:
                seen[0] += 1
                say("coder_progress", {"coder": args[0], "line": seen[0], **step})

        say("coder_start", {"coder": args[0], "cwd": str(workspace), "task": task[:160]})
        try:
            code, stdout, stderr = runner(args, workspace, timeout, on_line)
        except FileNotFoundError:
            return (f"Error: '{args[0]}' is not on PATH. Install pi "
                    f"(github.com/earendil-works/pi), or point POCKET_CODER at a coding "
                    f"agent you do have, e.g. POCKET_CODER='claude -p {{task}}'.")
        except subprocess.TimeoutExpired:
            return f"Error: {args[0]} did not finish within {int(timeout)}s in {workspace}."
        except OSError as exc:
            return f"Error running {args[0]}: {type(exc).__name__}: {exc}"

        reply, events = read_events(stdout)
        created = sorted(str(p.relative_to(workspace))
                         for p in workspace.rglob("*") if p.is_file() and p not in before)
        if events:
            (workspace / "events.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8")
        manifest = write_manifest(
            workspace, task=task, command=args, cwd=str(workspace),
            started=started.isoformat(timespec="seconds"),
            finished=datetime.now().isoformat(timespec="seconds"),
            exit_code=code, created=created, reply=reply, stderr=stderr[-2000:])

        say("coder_end", {"coder": args[0], "exit_code": code, "created": created,
                          "cwd": str(workspace)})
        head = (f"{args[0]} finished with exit code {code} in {workspace}.\n"
                f"Files it created: {', '.join(created) or 'none'}\n"
                f"Manifest: {manifest}")
        if code != 0 and not reply:
            return f"{head}\nIt reported: {stderr[-800:] or '(nothing on stderr)'}"
        return f"{head}\n\n{reply[:REPLY_PREVIEW]}"

    return Tool(
        name="delegate_task",
        description=("Hand a CODING task to a specialist agent that can read, write and run "
                     "files — fixing tests, editing several files, writing a program. Use it "
                     "ONLY when the job has to touch the filesystem or a shell, because you "
                     "cannot: for anything doable with your own tools, use `delegate`. Pass "
                     "`cwd` to work inside an existing project; leave it empty and it gets a "
                     "fresh dated folder."),
        input_schema={"type": "object", "properties": {
            "task": {"type": "string",
                     "description": "the complete instruction, standalone, as you would "
                                    "brief someone who cannot see this conversation"},
            "cwd": {"type": "string",
                    "description": "an existing project directory, or empty for a new one"}},
            "required": ["task"]},
        fn=delegate_task,
        risk="ask",           # it runs as you, on your files: a human reads it first
        origin="coder")
