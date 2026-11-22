# mini-harness

A small coding agent built on the 12 harness patterns of Claude Code, in one Python file.

It is the companion repo of the Craft Better Software article "Steal Claude Code's Harness for Your Own Agent". Read `agent.py` top to bottom: each `PATTERN n` section matches one section of the article.

No code from the Claude Code source leak is used. Every pattern is written from scratch, from the public pattern descriptions and the Claude Code docs.

## Run it

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...        # or: ant auth login
cd your-repo
python /path/to/agent.py "fix the failing test in tests/test_cart.py"
python /path/to/agent.py --plan "add a /health endpoint"   # plan mode first
python /path/to/agent.py --consolidate                     # clean up memory
python /path/to/agent.py                                   # chat mode
```

The default model is `claude-opus-5-5`. Set `MINI_HARNESS_MODEL` to use another model.

## The 12 patterns

| # | Pattern | Where in `agent.py` |
|---|---|---|
| 1 | Persistent instruction file | `build_prompt`, `load_rules` |
| 2 | Scoped context assembly | `rules_for`, `on_file_touched` |
| 3 | Tiered memory | `memory_index`, `read_memory`, `write_memory` |
| 4 | Dream consolidation | `consolidate_memory` |
| 5 | Progressive context compaction | `maybe_compact`, `compact` |
| 6 | Explore-plan-act loop | plan check in `call_tool`, `exit_plan` |
| 7 | Context-isolated subagents | `run_subagent` |
| 8 | Fork-join parallelism | `run_tool_calls`, `new_worktree`, `run_in_worktree` |
| 9 | Progressive tool expansion | tool list in `build_prompt`, `load_tools` |
| 10 | Command risk classification | `classify` |
| 11 | Single-purpose tool design | `read_file`, `edit_file`, `grep`, `glob` |
| 12 | Deterministic lifecycle hooks | `run_hooks` |

## Configure it

Put rules in `AGENTS.md` at the repo root, and in any subfolder. Put settings in `.agent/settings.json`:

```json
{
  "permissions": {
    "deny": ["rm -rf", "git push (-f|--force)", "\\.env\\b"],
    "ask": ["^git push"],
    "allow": ["^npm test", "^git (status|diff|log)"]
  },
  "hooks": {
    "PreToolUse": ["./scripts/block-secrets.sh"],
    "PostToolUse": [],
    "Stop": ["npm test >&2 || exit 2"]
  }
}
```

A hook gets the event as JSON on stdin. Exit code 2 blocks the action, and the hook's stderr goes back to the model. A `Stop` hook that exits with 2 makes the agent continue.

## Tests

```bash
pip install pytest
pytest -q
```

One test per pattern. A fake model replaces `call_model`, so the tests make no API calls.

## Where Claude Code does more

This repo shows the mechanism of each pattern in the smallest code. Claude Code also has MCP servers, a full permission rule language, server-side compaction, a background memory process, and a terminal UI.
