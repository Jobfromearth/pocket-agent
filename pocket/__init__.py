"""pocket — a minimal, readable re-implementation of a local-first agent.

Four pillars, one file each, nothing hidden:

    harness   config.py + session.py + __main__.py   text in, text out
    loop      loop.py                                reason -> act -> observe
    memory    memory.py + db.py                      semantic / episodic / procedural
    ops       trace.py + evals.py                    trace -> eval -> gate

Structure and several core routines are re-implemented from pocket-agent (MIT,
github.com/ShenSeanChen/pocket-agent) and trimmed to the smallest version that
still demonstrates the mechanism.
"""

__version__ = "0.1.0"
