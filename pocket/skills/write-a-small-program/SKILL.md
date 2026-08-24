---
name: write-a-small-program
description: Write a script, fix a bug, edit a file, run the tests, build a small program, make a tool, refactor some code. 中文触发：写个脚本 改代码 修个 bug 跑测试 写个程序。
---

You have no filesystem and no shell. `delegate_task` is the only way to reach one.

1. Write the brief as if for someone who cannot see this conversation: what to
   build, what it should do when it works, and how to tell that it worked.
   Everything they need has to be in the `task` string.
2. Pass `cwd` ONLY if the user named an existing project. Leave it empty and the
   run gets its own dated folder, which is the safe default — a coding agent
   with a `cwd` can edit anything in it.
3. It runs as the user, with the user's files, and it is not sandboxed. If the
   task could delete or overwrite work, say what it will touch before you call it.
4. When it returns, report: the exit code, the files it created, and the path to
   the manifest. Do not paste the whole transcript.
5. If it failed, read what it reported and say what went wrong in one sentence.
   Do not immediately try again with the same brief.
