"""上下文压缩的保真度评测。

⚠️ 这个脚本会真实调用 API、消耗额度，所以**不在 run_all.py 里**。
    单独运行：python tests/eval_compaction.py

--------------------------------------------------------------------
为什么需要它？
--------------------------------------------------------------------
"压缩是否正确"其实是三个不同的问题，可验证程度完全不同：

1. 结构正确性 —— 压缩后的历史仍是合法的请求体：没有孤儿 tool 消息、
   每个 tool_call 都有结果、system prompt 没丢。
   这是确定性的，可以证明。已由 tests/test_history.py 覆盖。

2. 机械正确性 —— 最近若干条消息原样保留、摘要非空、token 确实下降。
   同样是确定性的。也在 test_history.py 里。

3. 语义保真度 —— 摘要有没有保住"继续干活所必需的事实"。
   摘要是模型生成的，**无法证明无损**，只能测量。这个脚本干的就是这件事。

--------------------------------------------------------------------
测量方法：探针测试
--------------------------------------------------------------------
先在对话里埋下若干条可检验的事实（一部分来自用户消息，一部分来自工具
返回的内容），把阈值压低强制触发压缩，然后**禁用全部工具**去提问——
模型无法再去读文件，只能从压缩后的上下文里回答。答对几条就是召回率。

禁用工具这一步很关键：如果留着工具，模型答不上来时会去重新读一遍文件，
测出来的就不是"压缩保住了多少"，而是"模型会不会补救"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.loop as loop_mod
from agent.config import Config
from agent.loop import Agent, Reporter
from agent.tools import PermissionMode


class QuietReporter(Reporter):
    def __init__(self) -> None:
        self.notices: list[str] = []

    def on_text(self, delta): pass
    def on_text_end(self): pass
    def on_tool_start(self, name, arguments): print(f"      · {name}")
    def on_tool_end(self, name, result): pass
    def on_notice(self, message):
        self.notices.append(message)
        print(f"      ! {message}")
    def on_error(self, message): print(f"      x {message}")
    def on_todos(self, todos): pass
    def ask_approval(self, request): return "y"


# 埋进对话的事实，以及后面用来检验的探针
PLANTED = [
    (
        "记住两条项目信息，后面我会考你：这个项目的内部代号是 ORION，"
        "负责人叫周明远。先不用做别的。",
        [
            ("项目的内部代号是什么？只回答代号本身。", ["ORION", "orion"]),
            ("项目负责人叫什么名字？只回答名字。", ["周明远"]),
        ],
    ),
]

FILES_TO_READ = ["agent/history.py", "agent/llm.py", "agent/tools/paths.py"]


def probe(agent: Agent, question: str) -> str:
    """禁用全部工具后提问，逼模型只能从压缩后的上下文里回答。"""
    original = loop_mod.get_schemas
    loop_mod.get_schemas = lambda: []
    try:
        agent.run_turn(question)
    finally:
        loop_mod.get_schemas = original

    for message in reversed(agent.history.to_api()):
        if message.get("role") == "assistant" and message.get("content"):
            return message["content"]
    return ""


def main() -> int:
    workspace = Path(__file__).resolve().parent.parent
    config = Config.from_env(workspace, permission_mode=PermissionMode.READONLY)
    # 压低阈值强制触发压缩；正常默认值是 40000
    config.compact_threshold = 6000
    config.keep_recent_messages = 4

    reporter = QuietReporter()
    agent = Agent(config, reporter)

    print("=" * 62)
    print(f"  压缩保真度评测   阈值={config.compact_threshold}  "
          f"保留最近={config.keep_recent_messages} 条")
    print("=" * 62)

    probes: list[tuple[str, list[str]]] = []

    print("\n[1] 埋入事实")
    for instruction, checks in PLANTED:
        agent.run_turn(instruction)
        probes.extend(checks)
        print(f"    已埋入 {len(checks)} 条（来自用户消息）")

    print("\n[2] 读文件，让事实来自工具返回内容")
    for path in FILES_TO_READ:
        print(f"    读 {path}")
        agent.run_turn(f"读一下 {path}，用一句话概括它负责什么。")
    probes.append((
        "到目前为止你一共读过哪几个文件？只列文件名，不要解释。",
        [Path(p).name for p in FILES_TO_READ],
    ))

    compactions = [n for n in reporter.notices if "上下文已压缩" in n]
    print(f"\n[3] 压缩发生了 {len(compactions)} 次")
    for note in compactions:
        print(f"    {note}")
    if not compactions:
        print("    警告：一次都没压缩，本次评测无意义。请调低 compact_threshold。")
        return 1

    print(f"\n[4] 禁用工具后提问，共 {len(probes)} 组探针")
    hits = 0
    total = 0
    for question, expected in probes:
        answer = probe(agent, question)
        for token in expected:
            total += 1
            ok = token.lower() in answer.lower()
            hits += ok
            print(f"    [{'✓' if ok else '×'}] 期望包含 {token!r}")
        print(f"        回答：{answer.strip()[:90]}")

    rate = hits / total * 100 if total else 0
    print("\n" + "=" * 62)
    print(f"  事实召回率：{hits}/{total} = {rate:.0f}%")
    print("=" * 62)
    print("\n注：这是抽样测量，不是无损性证明——摘要由模型生成，本就无法证明无损。")
    print("    结构正确性（无孤儿 tool 消息等）由 tests/test_history.py 确定性地保证。")
    return 0 if rate >= 80 else 1


if __name__ == "__main__":
    sys.exit(main())
