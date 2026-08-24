---
name: split-a-multi-step-request
description: The user asked for several things at once, a request with multiple steps, do these three things, plan and then execute, several tasks in one message. 中文触发：好几件事 分几步 多个任务 先做这个再做那个。
---

When one request contains several pieces of work:

1. First ask whether you can just do them in sequence yourself. Two tool calls
   are cheaper than a team, always.
2. If there are two or more pieces that are genuinely independent, or that have
   a clear order, use `assign_team`. Give each task a short `key`, the smallest
   `tools` list that can finish it, and `needs` naming the tasks it must wait for.
3. Tasks with no `needs` run at the same time. That is the only reason to use a
   team rather than doing it yourself, so if everything depends on the thing
   before it, do it yourself instead.
4. A worker sees ONLY the results of the tasks it named in `needs`. If a worker
   needs something, it has to be in its own instruction or in a dependency.
5. If the plan comes back refused — a cycle, an unknown dependency, too many
   tasks — read the reason, fix the plan, and try once. Do not send the same
   plan twice.
