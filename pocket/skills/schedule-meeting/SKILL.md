---
name: schedule-meeting
description: How to schedule a meeting, catch-up or call with someone, including how to pick a time when the user does not give one. 中文触发：安排会议 约个时间 订个会 和某人见面。
---

When the user asks to schedule, book, or set up a meeting:

1. Check memory for that person's stated preference (mornings, evenings, no Fridays).
   If a preference exists, honour it instead of asking.
2. Default to 09:00 local time when no time is given and no preference is known.
3. Default the duration to one hour.
4. Call `create_event` exactly once, then confirm in one sentence: who, when, where it was saved.
5. Never invent attendees the user did not mention.
