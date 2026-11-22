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

An approval question (plan mode, an `ask` command) reads one line from stdin. `y` approves; anything else, or the end of input, is a no. So `echo y | python agent.py --plan "..."` approves the plan, and `</dev/null` declines every question.

### Without an API key: the claude CLI backend

If the `claude` CLI is logged in (for example with a Claude subscription), the agent can use it as the model:

```bash
cd your-repo
MINI_HARNESS_BACKEND=claude-cli python /path/to/agent.py "fix the failing test"
```

`claude_cli.py` runs one `claude -p` call per model call, with the prompt on stdin and the repo as the working folder:

- `--tools ""` turns off all Claude Code tools. The mini-harness runs every tool, so the permission rules, hooks and plan mode stay in force.
- `--system-prompt` replaces Claude Code's system prompt with the mini-harness prompt. `--setting-sources ""`, `--strict-mcp-config` and `--disable-slash-commands` keep out user settings, hooks, CLAUDE.md files, MCP servers and skills.
- `--max-turns 1`, `--output-format json`, `--no-session-persistence`.

The tool protocol is text. The tool schemas go in the system prompt, and the conversation goes on stdin as `<user>`, `<assistant>` and `<tool_result>` entries. The model ends its answer with a fenced `tool_calls` block that holds a JSON array of `{"name": ..., "input": {...}}`. The adapter turns it into blocks with the same fields as SDK blocks (`type`, `id`, `name`, `input`, `text`). A block that is not valid JSON gets one retry with the parse error; a second failure raises an error.

Environment variables: `MINI_HARNESS_CLI_TIMEOUT` (seconds per call, default 300), `MINI_HARNESS_CLAUDE_BIN` (path to `claude`). The child process gets the parent environment without the `CLAUDE*` and `CMUX*` variables, so a run inside a Claude Code session does not attach to that session.

This backend is slower than the SDK (each call starts a new `claude` process, about 3 to 10 seconds) and has no native tool use or thinking-block handling. Use the SDK backend when you have an API key.

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

One test per pattern. A fake model replaces `call_model`, so the tests make no API calls. `tests/test_claude_cli.py` tests the CLI backend's prompt rendering and reply parsing without running `claude`.

## Where Claude Code does more

This repo shows the mechanism of each pattern in the smallest code. Claude Code also has MCP servers, a full permission rule language, server-side compaction, a background memory process, and a terminal UI.
