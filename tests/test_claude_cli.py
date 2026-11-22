"""The claude-cli backend: prompt rendering and reply parsing. No CLI calls."""
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

import agent
import claude_cli


def test_plain_text_reply_is_one_text_block_and_ends_the_turn():
    r = claude_cli.parse_reply("All tests pass.\n")
    assert r.stop_reason == "end_turn"
    assert [(b.type, b.text) for b in r.content] == [("text", "All tests pass.")]
    assert agent.final_text(r) == "All tests pass."


def test_tool_calls_block_becomes_sdk_shaped_tool_use_blocks():
    r = claude_cli.parse_reply(
        'I will read both files.\n```tool_calls\n'
        '[{"name": "read_file", "input": {"path": "a.py"}},'
        ' {"name": "grep", "input": {"pattern": "def "}}]\n```')
    assert r.stop_reason == "tool_use"
    text, a, b = r.content
    assert text.type == "text" and text.text == "I will read both files."
    assert (a.type, a.name, a.input) == ("tool_use", "read_file", {"path": "a.py"})
    assert b.name == "grep" and a.id != b.id and a.id.startswith("toolu_")


def test_tool_input_may_contain_backtick_fences():
    new = "```python\nx = 1\n```"
    reply = "```tool_calls\n" + json.dumps(
        [{"name": "edit_file", "input": {"path": "README.md", "old": "", "new": new}}]) + "\n```"
    (call,) = claude_cli.parse_reply(reply).content
    assert call.input["new"] == new


def test_single_object_and_missing_input_are_accepted():
    (call,) = claude_cli.parse_reply('```tool_calls\n{"name": "exit_plan"}\n```').content
    assert call.name == "exit_plan" and call.input == {}


@pytest.mark.parametrize("reply", [
    "```tool_calls\n[{\"name\": \"read_file\", \"input\": {\"path\": }]\n```",  # bad JSON
    "```tool_calls\n[]\n```",                                                    # empty
    "```tool_calls\n[{\"input\": {}}]\n```",                                     # no name
    "```tool_calls\n[{\"name\": \"bash\", \"input\": \"ls\"}]\n```",             # input not object
])
def test_malformed_block_raises_parse_error(reply):
    with pytest.raises(claude_cli.ParseError):
        claude_cli.parse_reply(reply)


def test_render_keeps_tool_calls_results_and_rule_text():
    messages = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": [NS(type="tool_use", id="t1", name="read_file",
                                             input={"path": "a.py"})]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "   1  x = 1"},
            {"type": "text", "text": "# Rules from pkg/AGENTS.md\nNo prints."}]},
    ]
    out = claude_cli.render(messages)
    assert "<user>\nfix it\n</user>" in out
    assert '"name": "read_file"' in out and '"id": "t1"' in out
    assert '<tool_result id="t1">\n   1  x = 1\n</tool_result>' in out
    assert "No prints." in out


def test_malformed_reply_is_retried_once_with_the_error(monkeypatch, tmp_path):
    prompts = []
    replies = ["```tool_calls\n[{bad json}]\n```", "Done."]

    def fake_run(system_prompt, prompt, model, cwd):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    r = claude_cli.call_claude_cli("sys", [{"role": "user", "content": "go"}], [], "m", tmp_path)
    assert agent.final_text(r) == "Done."
    assert len(prompts) == 2 and "not valid" in prompts[1]


def test_cli_call_has_no_builtin_tools_replaced_prompt_and_clean_env(monkeypatch, tmp_path):
    seen = {}

    def fake_subprocess_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "hi"}), "")

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_subprocess_run)
    assert claude_cli.run_claude("SYS", "PROMPT", "claude-opus-5-5", tmp_path) == "hi"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert "--append-system-prompt" not in cmd and "PROMPT" not in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert seen["input"] == "PROMPT" and seen["cwd"] == tmp_path and seen["timeout"]
    assert not any(k.startswith("CLAUDE") for k in seen["env"])


def test_cli_error_result_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, json.dumps({"is_error": True, "result": "Not logged in"}), ""))
    with pytest.raises(RuntimeError, match="Not logged in"):
        claude_cli.run_claude("s", "p", "m", tmp_path)


def test_backend_switch_routes_call_model_to_the_cli(monkeypatch):
    monkeypatch.setattr(agent, "BACKEND", "claude-cli")
    monkeypatch.setattr(claude_cli, "run_claude", lambda *a: "ok")
    assert agent.final_text(agent.call_model("s", [{"role": "user", "content": "x"}], [])) == "ok"
