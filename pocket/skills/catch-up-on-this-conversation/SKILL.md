---
name: catch-up-on-this-conversation
description: What did I say earlier, what did we decide, remind me what we talked about, earlier in this conversation, go back and check. 中文触发：刚才说了什么 我们之前定了什么 回顾一下前面。
---

When you need something from earlier in this conversation:

1. If the context shows a message marked `[earlier conversation, compacted]`,
   the detail you want was summarised away. Call `read_history` rather than
   guessing at it or asking the user to repeat themselves.
2. Start with `offset=0` and a small `limit`. Increase `offset` to go further
   back; the newest messages come first.
3. Quote what you find rather than paraphrasing it, when the point is exactly
   what was said.
4. This reads the record of THIS assistant's conversations, not your memory of
   facts. For "what do you know about Alex", search memory instead.
