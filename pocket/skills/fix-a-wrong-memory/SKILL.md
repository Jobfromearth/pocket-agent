---
name: fix-a-wrong-memory
description: Something you remembered is wrong, out of date, forget that, that changed, stop remembering, correct what you know about me. 中文触发：记错了 改一下记忆 忘掉 过时了 别再记着。
---

When the user says a remembered fact is wrong:

1. `manage_memory` with `action="search"` and a word from the fact, to get its id.
   You cannot correct or forget anything without an id.
2. If the fact is now WRONG, use `action="correct"` with that id and the new
   content. Correcting keeps the subject and the history.
3. If the fact should simply not be remembered any more, use `action="forget"`.
   It stops being retrieved and stops appearing in MEMORY.md; the row stays in
   the database, so this is reversible by a human and not by you.
4. If the search returns several candidates, show them with their ids and ask
   which one — never guess at an id.
5. If what changed came from a consolidation run rather than something the user
   said, tell them `/dream-log` shows those runs and `/dream-restore` undoes one
   whole run at a time.
