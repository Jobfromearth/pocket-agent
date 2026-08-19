# Teams — the board

`delegate` (`subagent.py`) hands **one** sub-task to **one** sub-agent. A team
(`team.py`) is what you need when a request has several steps and some of them
are independent. The idea, and the honesty about failure, are lifted from
[ClawTeam](https://github.com/HKUDS/ClawTeam); the implementation is this repo's
own and reuses machinery that was already here.

```bash
POCKET_TEAM=1 python -m pocket      # the model can call assign_team
python -m pocket team               # run a three-worker plan, no model deciding
```

## The plan is data

A team starts from a plan, not a conversation. Workers never negotiate in free
text, which is what makes a run readable afterwards:

```json
[
  {"key": "remember", "task": "Remember that the Q4 offsite is on Friday",
   "tools": "save_note"},
  {"key": "book",     "task": "Schedule a kickoff with Alex tomorrow at 9am",
   "tools": "create_event"},
  {"key": "confirm",  "task": "What is on my calendar tomorrow?",
   "tools": "list_events", "needs": "book"}
]
```

| Field | Meaning |
|---|---|
| `key` | the task's name on the board, unique within the team |
| `task` | one complete, standalone instruction |
| `tools` | the allow-list this worker gets (comma-separated or a JSON array) |
| `needs` | tasks that must finish first; their results are handed over |

An invalid plan is refused **before any worker spends a token**: unknown
dependency, duplicate key, empty instruction, a cycle, or more than `MAX_TASKS`
(8). The refusal comes back to the model as text, so it can fix the plan and try
again — the same rule tool errors follow.

## Four mechanisms, all already in the repo

- **The board** is a `tasks` table in `state.db`. `pending → running → done`, or
  `failed` / `blocked`. `sqlite3 .pocket/state.db "select key, status from tasks"`
  *is* the kanban, and `/board` prints it inside a chat.
- **The schedule** is `graph.py`. The task DAG becomes a `Graph`, and its wave
  scheduler runs every ready task in parallel. A dependency finishing **is** the
  unblock; there is no separate unblock step that can get out of sync.
- **The channel** is one-directional. A worker's system prompt carries the
  results of exactly the tasks it declared in `needs` — nothing else. There is no
  worker-to-worker chat, no shared scratchpad, no sibling visibility.
- **The blast radius** is the tool list. Each worker is one `run_loop` with
  `registry.subset(...)`, and `assign_team` / `delegate` are never in it: one
  level of fan-out, by construction.

## Failure

When a worker fails, its row says `failed` and everything downstream of it is
marked `blocked` and never runs. Nothing downstream is attempted on missing
input, because a board that says "this did not happen" is worth more than one
that quietly produced something from nothing.

```
team-20260818-2211 · 3 tasks: 1 done, 1 failed, 1 blocked
  [remember] done: Saved to memory under 'offsite': the Q4 offsite is on Friday
  [book] failed: RuntimeError: provider is down
  [confirm] blocked (needs book): not run — a task it depends on did not finish
```

## What it costs

A team is the most expensive thing this assistant can start: several loops, each
with its own iterations. So `assign_team` is `risk="ask"` — a human sees the
goal and the plan first — and it is off unless `POCKET_TEAM=1`, because every
registered tool ships in every prompt whether or not it is ever called.

## What it deliberately does not do

- **No workspaces.** ClawTeam gives each worker a git worktree on its own branch,
  with checkpoint/merge/cleanup. Workers here share one home and one database;
  the isolation you get is the tool list, not the filesystem.
- **No inboxes between peers.** Results flow down the DAG only. If two tasks need
  to talk, they are one task, or one depends on the other.
- **No re-planning.** The plan is fixed when the run starts; a worker cannot add
  tasks. Re-planning is the model's job, on the next turn, with the board in hand.
- **No cross-process swarm.** Workers are threads in this process, not other
  agents on your machine.

Each of those is a real limit, and each is the reason the mechanism fits in one
readable file.
