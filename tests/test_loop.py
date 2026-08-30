"""agent 主循环的回归测试：三层终止条件与历史一致性。

用桩替换掉 LLMClient，这样不联网、不花钱也能把循环行为测清楚。
重点是那条协议约束：任何时刻交出去的历史里，每条 tool 消息都要能在前面
找到对应 tool_call_id 的 assistant 消息，且每个 tool_call 都要有结果——
循环从哪个出口退出都必须保持这一点。

直接用 python tests/test_loop.py 运行，不引入 pytest 依赖。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.loop as loop_mod
from agent.config import Config
from agent.llm import AssistantMessage, ToolCall
from agent.loop import Agent, Reporter
from agent.tools import PermissionMode


class RecordingReporter(Reporter):
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.errors: list[str] = []

    def on_text(self, delta): pass
    def on_text_end(self): pass
    def on_tool_start(self, name, arguments): pass
    def on_tool_end(self, name, result): pass
    def on_notice(self, message): self.notices.append(message)
    def on_error(self, message): self.errors.append(message)
    def ask_approval(self, request): return "y"


class ScriptedClient:
    """按剧本逐轮返回预设的 assistant 消息。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, config):  # 顶替 LLMClient(config)
        return self

    def complete(self, messages, tools=None, on_text=None):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return AssistantMessage(content="做完了", tool_calls=[], usage={})


def tool_call_msg(call_id: str, name: str = "list_dir", args: str = "{}"):
    return AssistantMessage(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        usage={"total_tokens": 10, "prompt_tokens": 5},
    )


def build_agent(script, max_steps: int = 40):
    workspace = Path(tempfile.mkdtemp())
    config = Config(
        api_key="stub",
        workspace=workspace,
        max_steps=max_steps,
        permission_mode=PermissionMode.AUTO,
    )
    reporter = RecordingReporter()
    loop_mod.LLMClient = ScriptedClient(script)
    return Agent(config, reporter), reporter


def assert_history_consistent(messages) -> None:
    """协议约束：tool 消息不能是孤儿，tool_call 也不能没有结果。"""
    defined = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                defined.add(call["id"])
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in defined, (
                f"孤儿 tool 消息 {message['tool_call_id']}"
            )

    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                assert call["id"] in answered, f"tool_call {call['id']} 没有对应结果"


def test_exit_when_no_tool_calls():
    """出口 1：模型不再要工具，本轮正常结束。"""
    agent, _ = build_agent([AssistantMessage(content="答完了", tool_calls=[], usage={})])
    stats = agent.run_turn("随便问问")
    assert stats.steps == 1, f"应当一步结束，实际 {stats.steps}"
    assert stats.tool_calls == 0
    assert_history_consistent(agent.history.to_api())


def test_exit_on_step_limit_keeps_history_consistent():
    """出口 2：撞上步数上限。历史必须仍然完整——每个 tool_call 都有结果。"""
    script = [tool_call_msg(f"call_{i}") for i in range(20)]
    agent, reporter = build_agent(script, max_steps=5)
    stats = agent.run_turn("一直干活")

    assert stats.steps == 5, f"应当卡在 5 步，实际 {stats.steps}"
    assert any("步数上限" in n for n in reporter.notices), "应当提示用户撞上了上限"
    assert_history_consistent(agent.history.to_api())


def test_tool_error_still_produces_result():
    """工具失败也必须回灌一条 tool 消息，否则整轮请求会被服务端拒绝。"""
    script = [
        tool_call_msg("bad_1", name="read_file", args='{"path": "../../越界.txt"}'),
        AssistantMessage(content="那我换个做法", tool_calls=[], usage={}),
    ]
    agent, _ = build_agent(script)
    agent.run_turn("读个不存在的文件")

    messages = agent.history.to_api()
    assert_history_consistent(messages)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "拒绝访问" in tool_msgs[0]["content"], "沙箱错误应当原样回灌给模型"


def test_unknown_tool_does_not_crash():
    """模型编造一个不存在的工具，不能让整轮任务挂掉。"""
    script = [
        tool_call_msg("ghost_1", name="not_a_real_tool"),
        AssistantMessage(content="知道了", tool_calls=[], usage={}),
    ]
    agent, reporter = build_agent(script)
    agent.run_turn("用个不存在的工具")

    assert not reporter.errors, f"不该冒出未捕获的错误：{reporter.errors}"
    messages = agent.history.to_api()
    assert_history_consistent(messages)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert "不存在名为" in tool_msgs[0]["content"]


def test_multiple_tool_calls_all_answered():
    """一条 assistant 消息带多个 tool_call 时，每个都要有对应结果。"""
    multi = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(id="m1", name="list_dir", arguments="{}"),
            ToolCall(id="m2", name="list_dir", arguments='{"path": "."}'),
            ToolCall(id="m3", name="read_file", arguments='{"path": "缺失.txt"}'),
        ],
        usage={"total_tokens": 10, "prompt_tokens": 5},
    )
    agent, _ = build_agent([multi, AssistantMessage(content="好了", tool_calls=[], usage={})])
    stats = agent.run_turn("一次做三件事")

    assert stats.tool_calls == 3
    assert_history_consistent(agent.history.to_api())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
