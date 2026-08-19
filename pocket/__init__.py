"""pocket — a minimal, readable local-first agent.

One mechanism, one file, standard library only:

    harness   config.py session.py agent.py __main__.py   text in, text out
    loop      loop.py models.py tools.py                  reason -> act -> observe
    memory    memory.py db.py                             semantic / episodic / procedural
    context   context.py                                  offloading and compaction
    reach     mcp.py subagent.py                          other people's servers, one worker
    team      team.py                                     many workers over one shared board
    safety    permissions.py                              deny, ask, grant
    graph     graph.py                                    structure around the loop
    ops       trace.py evals.py                           trace -> eval -> gate

Structure and several core routines are re-implemented from waku-agent (MIT,
github.com/ShenSeanChen/waku-agent) and trimmed to the smallest version that
still demonstrates the mechanism. See README.md for what came from elsewhere.
"""

__version__ = "0.3.0"
