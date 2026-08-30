"""上下文压缩的回归测试。

重点测 _find_safe_cut()：压缩后的消息序列必须仍然满足
"每条 tool 消息都能在前面找到对应 tool_call_id"这条协议约束，
否则请求会被服务端 400 拒绝，而且报错完全不会指向压缩逻辑。

直接用 python tests/test_history.py 运行，不引入 pytest 依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.history import History


def fake_summarizer(messages):
    return f"[摘要：{len(messages)} 条消息]"


def assert_tool_pairing(messages) -> None:
    """校验协议约束：tool 消息引用的 id 必须在前面的 assistant 消息里定义过。"""
    defined = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                defined.add(call["id"])
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in defined, (
                f"孤儿 tool 消息 {message['tool_call_id']}：压缩切断了 tool_call 配对"
            )


def make_history(rounds: int, keep_recent: int = 4) -> History:
    history = History("SYS", max_tool_output=1000, compact_threshold=10, keep_recent=keep_recent)
    for index in range(rounds):
        history.add_user(f"任务{index}")
        history.add_assistant(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}_{suffix}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"x"}'},
                    }
                    for suffix in ("a", "b")
                ],
            }
        )
        history.add_tool_result(f"call_{index}_a", "结果A")
        history.add_tool_result(f"call_{index}_b", "结果B")
        history.add_assistant({"role": "assistant", "content": f"完成{index}"})
    return history


def test_multi_round_compaction():
    for rounds in range(2, 9):
        history = make_history(rounds)
        result = history.compact(fake_summarizer)
        assert result.compacted, f"rounds={rounds} 应该压缩成功"
        assert_tool_pairing(history.to_api())


def test_cut_point_lands_inside_tool_block():
    """keep_recent 恰好让朴素切点落在 tool 消息中间——最容易切坏的情况。"""
    history = History("SYS", 1000, compact_threshold=10, keep_recent=3)
    history.add_user("任务")
    history.add_assistant(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                for i in range(3)
            ],
        }
    )
    for i in range(3):
        history.add_tool_result(f"c{i}", f"r{i}")
    history.add_assistant({"role": "assistant", "content": "done"})

    history.compact(fake_summarizer)
    assert_tool_pairing(history.to_api())
    assert history.to_api()[1]["role"] != "tool", "保留段不能以 tool 消息开头"


def test_all_tail_is_tool_refuses_to_compact():
    """整段尾巴都是 tool 消息时，宁可不压缩也不能切坏。"""
    history = History("SYS", 1000, compact_threshold=10, keep_recent=2)
    history.add_user("t")
    history.add_assistant(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                for i in range(6)
            ],
        }
    )
    for i in range(6):
        history.add_tool_result(f"c{i}", f"r{i}")

    result = history.compact(fake_summarizer)
    assert not result.compacted
    assert_tool_pairing(history.to_api())


def test_summarizer_failure_keeps_history_intact():
    """摘要调用失败不能拖垮整轮任务，历史要保持原样。"""
    history = make_history(5)
    before = len(history)

    def boom(_messages):
        raise RuntimeError("API 挂了")

    result = history.compact(boom)
    assert not result.compacted
    assert len(history) == before
    assert_tool_pairing(history.to_api())


def test_empty_summary_keeps_history_intact():
    history = make_history(5)
    before = len(history)
    result = history.compact(lambda _messages: "   ")
    assert not result.compacted
    assert len(history) == before


def test_repeated_compaction():
    """摘要本身会被再次摘要，不能出错。"""
    history = make_history(6)
    history.compact(fake_summarizer)
    assert_tool_pairing(history.to_api())

    for index in range(6, 10):
        history.add_user(f"u{index}")
        history.add_assistant(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"x{index}", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                ],
            }
        )
        history.add_tool_result(f"x{index}", "res")

    result = history.compact(fake_summarizer)
    assert result.compacted
    assert_tool_pairing(history.to_api())


def test_token_accounting():
    history = History("SYS", 1000, compact_threshold=100, keep_recent=2)
    assert history.estimated_tokens() == 0
    assert not history.needs_compaction()

    history.add_user("中" * 500)
    assert history.estimated_tokens() == 200  # 500 / 2.5
    assert history.needs_compaction()

    # API 报告的实测值应当覆盖本地估算，并把字符计数清零
    history.note_prompt_tokens(350)
    assert history.estimated_tokens() == 350

    history.add_user("x" * 250)
    assert history.estimated_tokens() == 450  # 350 + 250/2.5


def test_system_prompt_survives_compaction():
    history = make_history(6)
    history.compact(fake_summarizer)
    assert history.to_api()[0] == {"role": "system", "content": "SYS"}


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
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
