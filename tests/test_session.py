"""会话持久化与 todo_write 的测试。

存档这块最要紧的两条：
  1. system prompt 绝不能被恢复（它嵌着过期的目录快照）；
  2. 恢复出来的历史必须仍然满足 tool_call 配对约束，否则一 resume 就 400。

直接用 python tests/test_session.py 运行，不引入 pytest 依赖。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import session as session_store
from agent.history import History
from agent.tools import Permissions, PermissionMode, ToolContext, dispatch


def make_history() -> History:
    history = History("SYSTEM-带着目录快照", max_tool_output=1000)
    history.add_user("帮我改个 bug")
    history.add_assistant(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
            ],
        }
    )
    history.add_tool_result("c1", "文件内容")
    history.add_assistant({"role": "assistant", "content": "改好了"})
    return history


# ---------- 存档 ----------

def test_roundtrip_preserves_messages():
    workspace = Path(tempfile.mkdtemp())
    original = make_history()

    session_store.save(workspace, original.export_messages(), 123)
    payload = session_store.load(workspace)
    assert payload is not None, "刚存的档竟然读不回来"

    restored = History("SYSTEM-新的目录快照", max_tool_output=1000)
    restored.load_messages(payload["messages"])
    assert restored.export_messages() == original.export_messages()


def test_system_prompt_is_never_persisted():
    """存档里绝不能有 system 消息——它的目录快照会过期。"""
    workspace = Path(tempfile.mkdtemp())
    history = make_history()
    session_store.save(workspace, history.export_messages(), 0)

    payload = session_store.load(workspace)
    roles = [m.get("role") for m in payload["messages"]]
    assert "system" not in roles, "存档里混进了 system 消息"

    # 恢复后用的必须是新的 system prompt
    restored = History("SYSTEM-新的目录快照", max_tool_output=1000)
    restored.load_messages(payload["messages"])
    assert restored.to_api()[0]["content"] == "SYSTEM-新的目录快照"


def test_restored_history_keeps_tool_pairing():
    workspace = Path(tempfile.mkdtemp())
    session_store.save(workspace, make_history().export_messages(), 0)
    restored = History("SYS", max_tool_output=1000)
    restored.load_messages(session_store.load(workspace)["messages"])

    defined = set()
    for message in restored.to_api():
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                defined.add(call["id"])
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in defined, "恢复出来的历史有孤儿 tool 消息"


def test_workspaces_are_isolated():
    """不同工作目录的存档不能互相污染。"""
    ws_a, ws_b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    session_store.save(ws_a, [{"role": "user", "content": "项目A"}], 0)
    session_store.save(ws_b, [{"role": "user", "content": "项目B"}], 0)

    assert session_store.load(ws_a)["messages"][0]["content"] == "项目A"
    assert session_store.load(ws_b)["messages"][0]["content"] == "项目B"
    assert session_store.session_path(ws_a) != session_store.session_path(ws_b)


def test_missing_and_corrupt_sessions_return_none():
    assert session_store.load(Path(tempfile.mkdtemp())) is None, "不存在的存档应返回 None"

    workspace = Path(tempfile.mkdtemp())
    session_store.save(workspace, [{"role": "user", "content": "x"}], 0)
    target = session_store.session_path(workspace)
    target.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert session_store.load(workspace) is None, "损坏的存档应返回 None 而不是抛异常"


def test_version_mismatch_is_rejected():
    workspace = Path(tempfile.mkdtemp())
    session_store.save(workspace, [{"role": "user", "content": "x"}], 0)
    target = session_store.session_path(workspace)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["version"] = 999
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert session_store.load(workspace) is None, "版本不符应当拒绝加载"


def test_clear_removes_session():
    workspace = Path(tempfile.mkdtemp())
    session_store.save(workspace, [{"role": "user", "content": "x"}], 0)
    assert session_store.load(workspace) is not None
    session_store.clear(workspace)
    assert session_store.load(workspace) is None


# ---------- todo_write ----------

def make_ctx():
    seen = []
    ctx = ToolContext(
        workspace=Path(tempfile.mkdtemp()),
        permissions=Permissions(mode=PermissionMode.AUTO),
        on_todos=seen.append,
    )
    return ctx, seen


def test_todo_write_updates_context_and_notifies_ui():
    ctx, seen = make_ctx()
    todos = [
        {"content": "读配置", "status": "completed"},
        {"content": "改代码", "status": "in_progress"},
        {"content": "跑测试", "status": "pending"},
    ]
    result = dispatch("todo_write", json.dumps({"todos": todos}), ctx)
    assert not result.is_error, result.content
    assert ctx.todos == todos
    assert seen == [todos], "UI 回调没有收到清单"
    assert "1/3" in result.content


def test_todo_write_rejects_multiple_in_progress():
    """允许多项并行，清单就退化成静态列表，失去跟踪进度的意义。"""
    ctx, _ = make_ctx()
    todos = [
        {"content": "A", "status": "in_progress"},
        {"content": "B", "status": "in_progress"},
    ]
    result = dispatch("todo_write", json.dumps({"todos": todos}), ctx)
    assert result.is_error and "最多只能有一项" in result.content
    assert ctx.todos == [], "校验失败时不该写入清单"


def test_todo_write_rejects_bad_input():
    ctx, _ = make_ctx()
    bad = [
        {"todos": []},
        {"todos": [{"content": "", "status": "pending"}]},
        {"todos": [{"content": "A", "status": "不存在的状态"}]},
        {"todos": [{"content": "A"}]},
        {"todos": "不是数组"},
    ]
    for payload in bad:
        result = dispatch("todo_write", json.dumps(payload), ctx)
        assert result.is_error, f"本应报错却通过了：{payload}"
        assert result.content


def _cleanup() -> None:
    """测试会往真实的 .sessions 目录写临时存档，跑完自己收拾干净。"""
    for leftover in session_store.SESSIONS_DIR.glob("tmp*.json"):
        try:
            leftover.unlink()
        except OSError:
            pass


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
    _cleanup()
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
