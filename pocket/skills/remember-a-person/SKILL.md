---
name: remember-a-person
description: Remember something about a person, save a preference, note that someone likes or dislikes something, keep track of a colleague or friend. 中文触发：记住某人 记一下他喜欢 保存偏好 记住这个同事。
---

When the user tells you something durable about a person:

1. Call `save_note` with `subject` set to the person's name, lowercase, and
   nothing else — the subject is how it is found again.
2. One fact per note. "Alex prefers mornings and hates Zoom" is two notes, and
   two notes can be corrected independently.
3. Write the fact so it still makes sense in a year, without this conversation:
   "Alex prefers morning meetings", not "she said mornings work better".
4. Confirm in one short sentence. Do not read the whole memory back.
5. If it contradicts something you already remember, say so and ask which is
   right before saving — then use `manage_memory` to fix the old one.
