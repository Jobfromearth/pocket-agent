#!/usr/bin/env bash
# Where the lines went, per pillar. Not a gate — a thing to glance at when a
# pillar starts growing faster than the reason it exists.
#
#   ./scripts/line_budget.sh
#
# Borrowed, gratefully, from nanobot's core_agent_lines.sh.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

count() { cat "$@" 2>/dev/null | wc -l | tr -d ' '; }

printf "pocket line budget\n==================\n"
printf "  %-10s %5s  %s\n" harness   "$(count pocket/config.py pocket/session.py pocket/agent.py pocket/__main__.py pocket/hooks.py)" "config session agent __main__ hooks"
printf "  %-10s %5s  %s\n" doors     "$(count pocket/bus.py pocket/dashboard.py pocket/telegram.py)"                "bus dashboard telegram"
printf "  %-10s %5s  %s\n" loop      "$(count pocket/loop.py pocket/models.py pocket/tools.py)"                       "loop models tools"
printf "  %-10s %5s  %s\n" memory    "$(count pocket/memory.py pocket/db.py pocket/skills.py pocket/dream.py)"       "memory db skills dream"
printf "  %-10s %5s  %s\n" context   "$(count pocket/context.py)"                                                    "context"
printf "  %-10s %5s  %s\n" reach     "$(count pocket/mcp.py pocket/web.py pocket/subagent.py pocket/coder.py)"       "mcp web subagent coder"
printf "  %-10s %5s  %s\n" team      "$(count pocket/team.py)"                                                       "team"
printf "  %-10s %5s  %s\n" safety    "$(count pocket/permissions.py pocket/injection.py)"                            "permissions injection"
printf "  %-10s %5s  %s\n" graph     "$(count pocket/graph.py)"                                                      "graph"
printf "  %-10s %5s  %s\n" ops       "$(count pocket/trace.py pocket/evals.py pocket/judge.py)"                     "trace evals judge"
printf "  %-10s %5s\n" "" ""
printf "  %-10s %5s  %s\n" TOTAL     "$(count pocket/*.py)"                                                          "pocket/*.py — the number the README states"
printf "  %-10s %5s  %s\n" evals     "$(grep -c '^def test_' pocket/evals.py)"                                       "deterministic cases in the release gate"
