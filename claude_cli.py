"""Model backend that runs `claude -p` instead of the Anthropic SDK.

Use it when there is no API key but the claude CLI is logged in:

    MINI_HARNESS_BACKEND=claude-cli python agent.py "fix the failing test"

The child `claude` has no tools of its own (--tools ""), our system prompt
replaces its system prompt, and it answers one turn. The tool protocol is text:
the model ends its answer with a ```tool_calls block that holds a JSON array of
{"name": ..., "input": {...}}. parse_reply turns that into blocks with the same
fields as SDK content blocks (type, id, name, input, text), so the agent loop
does not change.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

TIMEOUT = int(os.environ.get("MINI_HARNESS_CLI_TIMEOUT", "300"))  # seconds per call
FENCE = "```tool_calls"

PROTOCOL = f"""# How to answer
You cannot call tools natively. To call tools, end your answer with one block:

{FENCE}
[{{"name": "read_file", "input": {{"path": "app.py"}}}}]
```

The block holds a JSON array of objects with "name" (a tool from the tool list
below) and "input" (an object that matches the tool's input_schema). Put
nothing after the block. The harness runs the calls and sends the results back
as <tool_result> entries. When the task is done, answer with plain text and no
block."""


class ParseError(ValueError):
    pass


def call_claude_cli(system: str, messages: list, tools: list, model: str, cwd: Path):
    """One model call through `claude -p`. Returns an SDK-shaped response."""
    system_prompt = "\n\n".join([system, PROTOCOL, tool_section(tools)])
    prompt = render(messages)
    text = run_claude(system_prompt, prompt, model, cwd)
    try:
        return parse_reply(text)
    except ParseError as e:  # one retry, with the parse error as a correction
        retry = (f"{prompt}\n\n<assistant>\n{text}\n</assistant>\n\n<user>\n"
                 f"Your tool_calls block is not valid: {e}. Send the whole answer "
                 f"again with a valid block.\n</user>")
        return parse_reply(run_claude(system_prompt, retry, model, cwd))


def tool_section(tools: list) -> str:
    if not tools:
        return "# Tools\nNo tools in this call. Answer with plain text only."
    return "# Tool schemas (you can call only these)\n" + "\n".join(
        json.dumps(t) for t in tools)


def run_claude(system_prompt: str, prompt: str, model: str, cwd: Path) -> str:
    cmd = [os.environ.get("MINI_HARNESS_CLAUDE_BIN") or shutil.which("claude") or "claude",
           "-p", "--output-format", "json", "--model", model,
           "--tools", "",  # no built-in tools: the harness runs every tool
           "--system-prompt", system_prompt,  # replaces Claude Code's prompt
           "--max-turns", "1", "--no-session-persistence",
           "--setting-sources", "",  # no user/project settings, hooks or CLAUDE.md
           "--strict-mcp-config", "--disable-slash-commands"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       cwd=cwd, env=child_env(), timeout=TIMEOUT)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): "
                           f"{(r.stderr or r.stdout)[-2000:]}") from None
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {out.get('result') or out}")
    return out.get("result") or ""


def child_env() -> dict:
    """The parent env minus the variables that tie a process to a Claude Code
    session (CLAUDECODE, CLAUDE_CODE_*, terminal-wrapper CMUX_*)."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "CMUX"))}


# ── transcript in, blocks out ──────────────────────────────────────────

def get(b, key, default=None):
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


def render(messages: list) -> str:
    """The conversation as tagged text. The model writes the next assistant turn."""
    out = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append(f"<{role}>\n{content}\n</{role}>")
            continue
        texts, calls, results = [], [], []
        for b in content:
            kind = get(b, "type")
            if kind == "text":
                texts.append(get(b, "text"))
            elif kind == "tool_use":
                calls.append({"id": get(b, "id"), "name": get(b, "name"),
                              "input": get(b, "input")})
            elif kind == "tool_result":
                results.append(f'<tool_result id="{get(b, "tool_use_id")}">\n'
                               f'{get(b, "content")}\n</tool_result>')
        body = "\n".join(texts)
        if calls:
            body += f"\n{FENCE}\n{json.dumps(calls)}\n```"
        out.append(f"<{role}>\n" + "\n".join(results + [body.strip()]).strip()
                   + f"\n</{role}>")
    out.append("Write the next <assistant> turn now: text, and a tool_calls block "
               "if you need tools. Do not write the tags.")
    return "\n\n".join(out)


def parse_reply(text: str):
    """Split a reply into a text block and tool_use blocks. Raises ParseError."""
    start = text.find(FENCE)
    if start == -1:
        return NS(content=[NS(type="text", text=text.strip())], stop_reason="end_turn")
    body = text[start + len(FENCE):].strip()
    if body.endswith("```"):
        body = body[:-3]
    try:
        calls = json.loads(body)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON error: {e}") from None
    if isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list) or not calls:
        raise ParseError("the block must hold a non-empty JSON array")
    blocks = []
    prose = text[:start].strip()
    if prose:
        blocks.append(NS(type="text", text=prose))
    for c in calls:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            raise ParseError(f'every call needs a "name" string: {json.dumps(c)[:200]}')
        inp = c.get("input", {})
        if not isinstance(inp, dict):
            raise ParseError(f'"input" must be an object in the call to {c["name"]}')
        blocks.append(NS(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:12]}",
                         name=c["name"], input=inp))
    return NS(content=blocks, stop_reason="tool_use")
