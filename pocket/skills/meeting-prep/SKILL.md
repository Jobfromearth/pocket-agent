---
name: meeting-prep
description: Prepare me for a meeting, who am I meeting, what should I know before this call, background on the person I am seeing. 中文触发：会前准备 我要见谁 这个人是谁 见面前该知道什么。
---

When the user is about to meet someone:

1. `list_events` for the day to find the meeting, its time and its attendees.
2. Search memory for each attendee by name — preferences, what they work on,
   what was agreed last time.
3. If the user asked about someone you have nothing on, and the meeting is with
   a company or a public person, `search_web` once for recent context. Do not
   search for a private individual you only know from their calendar.
4. Report in this order: when and who, what you already know about them, what
   was agreed last time, and what is still open.
5. If you know nothing about them, say that plainly and offer to remember what
   the user tells you afterwards.
