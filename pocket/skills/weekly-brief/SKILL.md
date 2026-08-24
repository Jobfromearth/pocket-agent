---
name: weekly-brief
description: Brief me on my week or my day, catch me up, tell me what to focus on, morning briefing, what is coming up. 中文触发：简报 我这周 我今天 有什么安排 帮我梳理一下。
---

When the user asks to be briefed or caught up:

1. Call `list_events` for today. If they asked about the week rather than the day,
   call it once per day for the next seven days — there is no range argument.
2. Ask memory about the people who appear in those events, so a name in the
   calendar arrives with what you know about them attached.
3. Lead with the ONE to THREE things that actually matter: a deadline, a meeting
   that needs preparing, something they told you was important.
4. Then go day by day, in order, with times and attendees. Skip empty days.
5. End with a single sentence naming what to do first.

If the calendar is empty, say so in one line and do not pad it out. A brief that
invents importance is worse than a short one.

(The shape of this brief — a lead, then chronological, then one focus line — is
borrowed from waku-agent's weekly-brief skill, MIT.)
