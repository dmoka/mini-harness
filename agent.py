"""mini-harness: a small coding agent built on the 12 harness patterns of Claude Code.

Read it top to bottom. Each "PATTERN n" section matches one section of the
Craft Better Software article "Steal Claude Code's Harness for Your Own Agent".
No code from the Claude Code leak is used: every pattern is written from scratch.

    python agent.py "fix the failing test in tests/test_cart.py"
    python agent.py --plan "add a /health endpoint"
    python agent.py --consolidate
    python agent.py                      # chat mode
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

MODEL = os.environ.get("MINI_HARNESS_MODEL", "claude-opus-5-5")
ROOT = Path.cwd().resolve()
AGENT_DIR = ".agent"  # settings.json and memory/ live here
RULES_FILE = "AGENTS.md"
CHILD = bool(os.environ.get("MINI_HARNESS_CHILD"))  # a worktree agent (pattern 8)

BASE_PROMPT = (
    "You are a coding agent working in the repository in the current folder. "
    "Use the tools to read, search and change code, and to run commands. "
    "When the task is done, answer with a short report and no tool call."
)


# ── THE BASE AGENT ──────────────────────────────────────────────────────
# A model call in a loop. The model asks for tool calls; the harness runs them.

_client = None


def call_model(system: str, messages: list, tools: list):
    """One model call. The tests replace this function with a fake."""
    global _client
    import anthropic

    _client = _client or anthropic.Anthropic()
    return _client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=messages,
        tools=tools,
        # Compaction (5), plan mode (6) and load_tools (9) change the history,
        # the system prompt or the tool list. drop_block makes the API drop the
        # old thinking blocks that this invalidates, instead of failing.
        thinking={"type": "adaptive",
                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        fallbacks="default",  # a refused request is retried on a fallback model
        betas=["thinking-binding-controls-2026-08-01",
               "server-side-fallback-2026-07-01"],
    )


@dataclass
class Session:
    messages: list = field(default_factory=list)
    active: list = field(default_factory=lambda: list(CORE_TOOLS))  # pattern 9
    mode: str = "act"  # "plan" or "act" (pattern 6)
    system_extra: str = ""  # e.g. the subagent prompt (pattern 7)
    allowed_paths: list | None = None  # write scope (pattern 4)
    pending: list = field(default_factory=list)  # text for the next message (pattern 2)
    loaded_rules: set = field(default_factory=set)


_current: contextvars.ContextVar[Session] = contextvars.ContextVar("session")


def run_loop(s: Session, task: str) -> str:
    token = _current.set(s)
    try:
        s.messages.append({"role": "user", "content": task})
        stop_blocks = 0
        while True:
            maybe_compact(s)  # pattern 5
            tools = [TOOLS[n].full_schema for n in s.active]
            response = call_model(build_prompt(s), s.messages, tools)
            s.messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                text = final_text(response)
                if response.stop_reason == "end_turn" and stop_blocks < 3:
                    blocked = run_hooks("Stop", {"final_text": text})  # pattern 12
                    if blocked:
                        stop_blocks += 1
                        s.messages.append({"role": "user", "content": blocked})
                        continue
                return text
            results = run_tool_calls(calls, s)  # pattern 8
            s.messages.append({"role": "user", "content": results + take_pending(s)})
    finally:
        _current.reset(token)


def run_tool_calls(calls: list, s: Session) -> list:
    def one(b):
        out = call_tool(b.name, dict(b.input), s)
        return {"type": "tool_result", "tool_use_id": b.id, "content": out}

    # Pattern 8, the fork: several subagent calls in one answer run at the same time.
    # The join is the next model call, which gets all reports together.
    if len(calls) > 1 and all(b.name == "run_subagent" for b in calls):
        with ThreadPoolExecutor() as pool:
            return list(pool.map(one, calls))
    return [one(b) for b in calls]


def call_tool(name: str, args: dict, s: Session) -> str:
    token = _current.set(s)
    try:
        print(f"  -> {name}({json.dumps(args)[:120]})", file=sys.stderr)
        if name not in s.active:
            return f"Error: {name} is not loaded. Call load_tools first."
        if s.mode == "plan" and name in WRITE_TOOLS:  # pattern 6
            return "Error: plan mode. Only read tools work. Call exit_plan with your plan."
        if name == "bash":  # pattern 10
            verdict = classify(args.get("command", ""))
            if verdict == "deny":
                return "Error: the permission rules deny this command."
            if verdict == "ask" and not ask_human(f"Run `{args['command']}`?"):
                return "Error: the user declined this command."
        blocked = run_hooks("PreToolUse", {"tool_name": name, "tool_input": args})
        if blocked:  # pattern 12
            return f"Blocked by hook: {blocked}"
        try:
            result = str(TOOLS[name].fn(**args))
        except (ToolError, OSError, TypeError, re.error) as e:  # pattern 11
            return f"Error: {e}"
        run_hooks("PostToolUse", {"tool_name": name, "tool_input": args,
                                  "tool_response": result[:2000]})
        return result
    finally:
        _current.reset(token)


def final_text(response) -> str:
    if response.stop_reason == "refusal":
        return "The model refused this request."
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def ask_human(question: str) -> bool:
    if CHILD or not sys.stdin.isatty():
        return False  # nobody can answer, so the safe answer is no
    return input(f"{question} [y/N] ").strip().lower() == "y"


# ── PATTERN 11: SINGLE-PURPOSE TOOL DESIGN ──────────────────────────────
# Small typed tools that check their input. A failed check returns an error text,
# and the model reads it as its next input.

class ToolError(Exception):
    pass


@dataclass
class Tool:
    fn: object
    one_line: str
    schema: dict

    @property
    def name(self) -> str:
        return self.fn.__name__

    @property
    def full_schema(self) -> dict:
        return {"name": self.name, "description": self.one_line,
                "input_schema": self.schema}


TOOLS: dict[str, Tool] = {}
WRITE_TOOLS = {"edit_file", "delete_file", "bash", "write_memory"}


def tool(one_line: str, **params: str):
    """Register a tool. params: name="string"; a trailing "?" makes it optional."""
    def wrap(fn):
        props, required = {}, []
        for key, typ in params.items():
            if not typ.endswith("?"):
                required.append(key)
            typ = typ.rstrip("?")
            props[key] = ({"type": "array", "items": {"type": "string"}}
                          if typ == "array" else {"type": typ})
        TOOLS[fn.__name__] = Tool(fn, one_line, {
            "type": "object", "properties": props, "required": required})
        return fn
    return wrap


def safe_path(path: str, write: bool = False) -> Path:
    p = (ROOT / path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ToolError(f"{path} is outside the repository.")
    s = _current.get(None)
    if write and s and s.allowed_paths is not None:  # pattern 4
        if not any(p == a or a in p.parents for a in s.allowed_paths):
            raise ToolError("This agent can only write inside: "
                            + ", ".join(str(a.relative_to(ROOT)) for a in s.allowed_paths))
    return p


@tool("Read a file. Returns numbered lines.", path="string")
def read_file(path):
    p = safe_path(path)
    on_file_touched(p)  # pattern 2
    lines = p.read_text().splitlines()
    return "\n".join(f"{i:>4}  {line}" for i, line in enumerate(lines, 1)) or "(empty file)"


@tool("Replace one exact string in a file. It must match exactly once. "
      "An empty 'old' creates a new file.", path="string", old="string", new="string")
def edit_file(path, old, new):
    p = safe_path(path, write=True)
    on_file_touched(p)  # pattern 2
    if old == "":
        if p.exists():
            raise ToolError(f"{path} exists. Put the text to replace in 'old'.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new)
        return f"Created {path}"
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        raise ToolError(f"'old' matches {n} times. Add more context.")
    p.write_text(text.replace(old, new))
    return f"Edited {path}"


@tool("Delete a file.", path="string")
def delete_file(path):
    safe_path(path, write=True).unlink()
    return f"Deleted {path}"


SKIP = {".git", ".venv", "node_modules", "__pycache__", AGENT_DIR}


def repo_files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not SKIP & set(p.relative_to(ROOT).parts):
            yield p


@tool("Search file contents with a regex. Returns path:line: text, max 100 matches.",
      pattern="string")
def grep(pattern):
    rx, out = re.compile(pattern), []
    for p in repo_files():
        try:
            lines = p.read_text().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                out.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:200]}")
                if len(out) == 100:
                    return "\n".join(out)
    return "\n".join(out) or "No matches."


@tool("List files that match a glob pattern, e.g. src/**/*.py.", pattern="string")
def glob(pattern):
    hits = [str(p.relative_to(ROOT)) for p in ROOT.glob(pattern)
            if p.is_file() and not SKIP & set(p.relative_to(ROOT).parts)]
    return "\n".join(sorted(hits)[:200]) or "No files."


@tool("Run a shell command in the repository. Returns the exit code and output.",
      command="string")
def bash(command):
    r = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True,
                       text=True, timeout=600)
    return f"exit {r.returncode}\n{(r.stdout + r.stderr)[-10000:]}"


# ── PATTERN 1: PERSISTENT INSTRUCTION FILE ──────────────────────────────
# The harness reads AGENTS.md from disk and puts it in the system prompt of every
# call. The model never opens the file: the text is part of its input.

def load_rules(path: Path, depth: int = 0) -> str:
    """Read a rules file. A line "@other.md" loads that file too, max 5 levels."""
    out = []
    for line in path.read_text().splitlines():
        target = path.parent / line[1:].strip()
        if line.startswith("@") and depth < 5 and target.is_file():
            out.append(load_rules(target, depth + 1))
        else:
            out.append(line)
    return "\n".join(out)


def build_prompt(s: Session) -> str:
    parts = [BASE_PROMPT, s.system_extra]
    root_rules = ROOT / RULES_FILE
    if root_rules.exists():  # pattern 1
        s.loaded_rules.add(root_rules)
        parts.append(f"# Rules from {RULES_FILE}\n{load_rules(root_rules)}")
    index = memory_index()  # pattern 3
    if index:
        parts.append("# Memory index (open a topic with read_memory)\n" + "\n".join(index))
    parts.append("# All tools (load_tools adds the ones you need)\n" + "\n".join(
        f"- {t.name}: {t.one_line}" for t in TOOLS.values()))  # pattern 9
    if s.mode == "plan":  # pattern 6
        parts.append("# Plan mode\nRead the code, then call exit_plan with a step-by-step plan.")
    return "\n\n".join(p for p in parts if p)


# ── PATTERN 2: SCOPED CONTEXT ASSEMBLY ──────────────────────────────────
# A folder's AGENTS.md loads the first time a tool call touches a file in it.
# The harness adds it as a text block after the tool results.

def rules_for(path: Path) -> list[Path]:
    files, folder = [], path.parent
    while folder == ROOT or ROOT in folder.parents:
        if (folder / RULES_FILE).exists():
            files.append(folder / RULES_FILE)
        folder = folder.parent
    return list(reversed(files))  # root first, closest folder last


def on_file_touched(path: Path):
    s = _current.get(None)
    for f in rules_for(path) if s else []:
        if f not in s.loaded_rules:
            s.loaded_rules.add(f)
            s.pending.append({"type": "text", "text":
                              f"# Rules from {f.relative_to(ROOT)}\n{load_rules(f)}"})


def take_pending(s: Session) -> list:
    items, s.pending = s.pending, []
    return items


# ── PATTERN 3: TIERED MEMORY ────────────────────────────────────────────
# Tier 1: the MEMORY.md index, in every system prompt, max 200 lines.
# Tier 2: one file per topic, read only when the model asks for it.

def memory_dir() -> Path:
    return ROOT / AGENT_DIR / "memory"


def memory_index() -> list[str]:
    f = memory_dir() / "MEMORY.md"
    return f.read_text().splitlines()[:200] if f.exists() else []  # hard cap


def topic(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "note"


@tool("Read one memory topic file by name, e.g. read_memory('deploy').", name="string")
def read_memory(name):
    return (memory_dir() / f"{topic(name)}.md").read_text()


@tool("Save a fact: writes the topic file and one line in the MEMORY.md index.",
      name="string", summary="string", body="string")
def write_memory(name, summary, body):
    d, name = memory_dir(), topic(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body)
    index = d / "MEMORY.md"
    lines = index.read_text().splitlines() if index.exists() else []
    lines = [x for x in lines if not x.startswith(f"- {name}:")] + [f"- {name}: {summary}"]
    index.write_text("\n".join(lines) + "\n")
    return f"Saved memory {name}"


# ── PATTERN 4: DREAM CONSOLIDATION ──────────────────────────────────────
# A separate agent run cleans the memory folder. It can write only there,
# and one git commit per run makes every deletion recoverable.

CONSOLIDATE_PROMPT = (
    f"You clean up the memory folder {AGENT_DIR}/memory. Merge duplicate notes. "
    "Delete facts that a newer note contradicts. Keep MEMORY.md under 150 lines, "
    "one line per topic file, in the form '- name: summary'."
)


def consolidate_memory() -> str:
    s = Session(active=["glob", "read_file", "edit_file", "delete_file"],
                system_extra=CONSOLIDATE_PROMPT, allowed_paths=[memory_dir()])
    report = run_loop(s, "Consolidate the memory folder now.")
    git("add", "-A", str(memory_dir()))
    git("commit", "-q", "-m", "memory: consolidation")
    return report


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)


# ── PATTERN 5: PROGRESSIVE CONTEXT COMPACTION ───────────────────────────
# Near the limit, old tool outputs become one-line stubs, and a summary replaces
# the old messages. The last messages stay as they are.

COMPACT_AT = 400_000  # characters, about 100K tokens


def maybe_compact(s: Session):
    if len(str(s.messages)) > COMPACT_AT:
        s.messages[:] = compact(s.messages)
        s.loaded_rules.clear()  # rules load again from disk: a summary can drop a rule


def compact(messages: list, keep_last: int = 10) -> list:
    cut = max(0, len(messages) - keep_last)
    while cut < len(messages) and messages[cut]["role"] != "assistant":
        cut += 1  # the kept part starts with an assistant turn
    old, recent = messages[:cut], messages[cut:]
    if not old:
        return messages
    for m in old:  # tool outputs first: the largest part, rarely needed again
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    b["content"] = f"[tool output removed, {len(str(b['content']))} chars]"
    summary = final_text(call_model(
        "Summarize this coding session for the agent that continues it. "
        "Keep decisions, file paths, failing test names and open steps.",
        [{"role": "user", "content": transcript(old)}], []))
    return [{"role": "user", "content": f"Summary of the earlier session:\n{summary}"}] + recent


def transcript(messages: list) -> str:
    def get(b, key):
        return b.get(key) if isinstance(b, dict) else getattr(b, key, None)

    lines = []
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [
            {"type": "text", "text": m["content"]}]
        for b in blocks:
            kind = get(b, "type")
            if kind == "text":
                lines.append(f"{m['role']}: {get(b, 'text')}")
            elif kind == "tool_use":
                lines.append(f"tool call: {get(b, 'name')}({json.dumps(get(b, 'input'))})")
            elif kind == "tool_result":
                lines.append(f"tool result: {get(b, 'content')}")
    return "\n".join(lines)


# ── PATTERN 6: EXPLORE-PLAN-ACT LOOP ────────────────────────────────────
# Plan mode is a permission mode: call_tool refuses every write tool.
# exit_plan shows the plan, and the approval switches the mode to "act".

@tool("Plan mode: show your plan to the user. The edit tools work after approval.",
      plan="string")
def exit_plan(plan):
    s = _current.get()
    print(f"\n--- PLAN ---\n{plan}\n", file=sys.stderr)
    if ask_human("Approve this plan?"):
        s.mode = "act"
        return "Plan approved. The edit tools work now. Follow the plan."
    return "Plan rejected. Ask the user what to change."


# ── PATTERN 7: CONTEXT-ISOLATED SUBAGENTS ───────────────────────────────
# A subagent is a second loop with a new, empty conversation and fewer tools.
# Only its last message comes back, as the result of the tool call.

SUBAGENT_TOOLS = ["read_file", "grep", "glob", "bash"]
SUBAGENT_PROMPT = "You are a subagent. Do the task, then answer with a report of max 10 lines."


@tool("Start a subagent with its own context and read-only tools. Returns only its "
      "report. worktree=true runs it in a new git worktree with edit tools.",
      task="string", worktree="boolean?")
def run_subagent(task, worktree=False):
    if worktree:
        return run_in_worktree(task)  # pattern 8
    s = Session(active=list(SUBAGENT_TOOLS), system_extra=SUBAGENT_PROMPT)
    return run_loop(s, task)


# ── PATTERN 8: FORK-JOIN PARALLELISM ────────────────────────────────────
# The fork is in run_tool_calls. With worktree=true, each subagent runs as its own
# process in a second checkout of the repo, on its own branch.

def new_worktree() -> tuple[Path, str]:
    branch = f"agent/{uuid.uuid4().hex[:6]}"
    path = ROOT.parent / f"{ROOT.name}-{branch.replace('/', '-')}"
    r = git("worktree", "add", "-q", str(path), "-b", branch)
    if r.returncode:
        raise ToolError(r.stderr.strip())
    return path, branch


def run_in_worktree(task: str) -> str:
    path, branch = new_worktree()
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), task], cwd=path,
                       capture_output=True, text=True,
                       env={**os.environ, "MINI_HARNESS_CHILD": "1"})
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", f"agent: {task[:60]}", cwd=path)
    return (f"Branch {branch} in {path}. Merge with: git merge {branch}\n"
            f"Report:\n{r.stdout[-3000:]}")


# ── PATTERN 9: PROGRESSIVE TOOL EXPANSION ───────────────────────────────
# The system prompt lists every tool in one line. Only the core schemas are in the
# request; load_tools adds more, and the model can call them from the next step on.

CORE_TOOLS = ["read_file", "edit_file", "grep", "glob", "bash", "run_subagent",
              "load_tools", "exit_plan"]


@tool("Load the full schemas of tools from the tool list, by name.", names="array")
def load_tools(names):
    s = _current.get()
    missing = [n for n in names if n not in TOOLS]
    if missing:
        raise ToolError(f"Unknown tools: {', '.join(missing)}")
    s.active += [n for n in names if n not in s.active]
    return f"Loaded: {', '.join(names)}. You can call them now."


# ── PATTERN 10: COMMAND RISK CLASSIFICATION ─────────────────────────────
# Every shell command is split at && || ; | and each part is checked:
# deny first, then ask, then allow. A command that matches no rule is an ask.

DEFAULT_PERMISSIONS = {
    "deny": [r"rm -rf", r"git push (-f|--force)", r"\.env\b"],
    "ask": [r"^git push", r"^npm publish", r"^git reset --hard"],
    "allow": [r"^(npm|pnpm|yarn) (test|run lint)", r"^(python -m )?pytest",
              r"^git (status|diff|log)", r"^(ls|cat|head|wc)\b"],
}


def classify(cmd: str) -> str:
    rules = settings().get("permissions", DEFAULT_PERMISSIONS)
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\|", cmd) if p.strip()]

    def matches(kind, part):
        return any(re.search(rx, part) for rx in rules.get(kind, []))

    if any(matches("deny", p) for p in parts):
        return "deny"
    if any(matches("ask", p) for p in parts):
        return "ask"
    if parts and all(matches("allow", p) for p in parts):
        return "allow"
    return "ask"


# ── PATTERN 12: DETERMINISTIC LIFECYCLE HOOKS ───────────────────────────
# Shell commands from .agent/settings.json run at fixed events and get the event
# as JSON on stdin. Exit code 2 blocks, and the error text goes to the model.

def settings() -> dict:
    f = ROOT / AGENT_DIR / "settings.json"
    return json.loads(f.read_text()) if f.exists() else {}


def run_hooks(event: str, payload: dict) -> str | None:
    for cmd in settings().get("hooks", {}).get(event, []):
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True,
                           input=json.dumps({"event": event, **payload}), timeout=600)
        if r.returncode == 2:
            return (r.stderr or r.stdout).strip()[-4000:] or f"Blocked by: {cmd}"
    return None


# ── COMMAND LINE ────────────────────────────────────────────────────────

def main(argv: list[str]):
    if argv[:1] == ["--consolidate"]:
        print(consolidate_memory())
        return
    s = Session()
    if argv[:1] == ["--plan"]:
        s.mode, argv = "plan", argv[1:]
    if argv:
        print(run_loop(s, " ".join(argv)))
        return
    while True:  # chat mode: one session, many tasks
        try:
            task = input("\nyou> ").strip()
        except EOFError:
            return
        if task:
            print(run_loop(s, task))


if __name__ == "__main__":
    main(sys.argv[1:])
