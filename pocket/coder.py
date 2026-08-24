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


def run_command(args: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
    """The seam the eval suite replaces, so the suite never needs pi installed."""
    finished = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, check=False)
    return finished.returncode, finished.stdout or "", finished.stderr or ""


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
                    timeout: float = TIMEOUT) -> Tool:
    def delegate_task(task: str, cwd: str = "") -> str:
        args = argv(task)
        if not args:
            return "Error: POCKET_CODER is empty, so there is no coding agent to call."
        workspace = Path(cwd).expanduser() if cwd else new_workspace(home, task)
        if not workspace.is_dir():
            return f"Error: '{workspace}' is not a directory."
        before = {p for p in workspace.rglob("*") if p.is_file()}
        started = datetime.now()
        try:
            code, stdout, stderr = runner(args, workspace, timeout)
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
