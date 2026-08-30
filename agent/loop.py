"""agent 主循环——整个项目的核心。

一轮用户输入进来后，循环做的事只有一件：
    请求模型 → 模型要不要调工具 → 要就执行并把结果回灌 → 再问一次模型

终止条件有三层，缺一不可：
  1. 模型这次回复不含 tool_calls：任务告一段落，把控制权交还用户（正常出口）；
  2. 步数达到 max_steps：防止"工具一直失败 → 模型一直重试"的死循环烧钱；
  3. 用户 Ctrl+C：中断当前轮，但保留历史，下一轮还能接着聊。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .history import CompactionResult, History
from .llm import AssistantMessage, LLMClient, LLMError
from .prompts import build_summary_request, build_system_prompt
from . import session as session_store
from .tools import ApprovalRequest, Permissions, ToolContext, ToolResult, dispatch, get_schemas
from .tools.filesystem import list_dir


class Reporter:
    """UI 回调接口。loop 不依赖具体的终端实现，方便替换成日志或测试桩。"""

    def on_text(self, delta: str) -> None: ...
    def on_text_end(self) -> None: ...
    def on_tool_start(self, name: str, arguments: str) -> None: ...
    def on_tool_end(self, name: str, result: ToolResult) -> None: ...
    def on_notice(self, message: str) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_todos(self, todos: list[dict[str, str]]) -> None: ...

    def ask_approval(self, request: ApprovalRequest) -> str:
        """默认拒绝一切（fail closed）。具体 UI 必须覆盖这个方法。"""
        return "n"


@dataclass(slots=True)
class TurnStats:
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    # 压缩时调用模型写摘要的开销，单独记账：它不属于"干活"的消耗，
    # 但确实花了钱，混在 tokens 里会让人看不出压缩的真实成本
    compaction_tokens: int = 0


class Agent:
    def __init__(self, config: Config, reporter: Reporter, resume: bool = False) -> None:
        self.config = config
        self.reporter = reporter
        self.client = LLMClient(config)
        self._last_summary_tokens = 0
        self.permissions = Permissions(mode=config.permission_mode, asker=reporter.ask_approval)
        self.ctx = ToolContext(
            workspace=config.workspace,
            permissions=self.permissions,
            on_todos=reporter.on_todos,
        )
        # system prompt 总是按当前目录现场重建，绝不从存档恢复：
        # 它嵌着启动那一刻的目录快照，恢复旧的会让模型拿着过期印象干活。
        self.history = History(
            system_prompt=build_system_prompt(config.workspace, self._overview()),
            max_tool_output=config.max_tool_output,
            compact_threshold=config.compact_threshold,
            keep_recent=config.keep_recent_messages,
        )
        if resume:
            self.restore_session()
        else:
            # 不加提示的话这里是个静默的坑：做完一个长任务，随手在同一目录
            # 跑个一次性命令，上次的历史就被这一轮的存档覆盖没了。
            # 把它变成一次知情选择。
            existing = session_store.load(config.workspace)
            if existing is not None:
                self.reporter.on_notice(
                    f"该目录有历史会话（{session_store.describe(existing)}），"
                    "本次将开新对话并在结束时覆盖它；要接着上次请加 --resume。"
                )

    # ---------- 会话存档 ----------

    def restore_session(self) -> bool:
        payload = session_store.load(self.config.workspace)
        if payload is None:
            self.reporter.on_notice("没有找到该工作目录的历史会话，将开始新对话")
            return False
        self.history.load_messages(payload["messages"])
        self.reporter.on_notice(f"已恢复会话：{session_store.describe(payload)}")
        return True

    def save_session(self) -> None:
        session_store.save(
            self.config.workspace,
            self.history.export_messages(),
            self.history.total_tokens,
        )

    def _overview(self) -> str:
        """开场就把目录结构塞进系统提示，省掉模型的第一次探路调用。"""
        try:
            return list_dir(self.ctx, ".", depth=2)
        except Exception:  # noqa: BLE001 - 概览失败不该阻止 agent 启动
            return "（无法读取目录结构）"

    def run_turn(self, user_input: str) -> TurnStats:
        # 存档放在 finally 里：三个出口（正常结束 / 步数上限 / 中断报错）
        # 都要落盘，否则 Ctrl+C 一次就丢掉整轮进度。
        try:
            return self._run_turn(user_input)
        finally:
            self.save_session()

    def _run_turn(self, user_input: str) -> TurnStats:
        self.history.add_user(user_input)
        stats = TurnStats()

        while stats.steps < self.config.max_steps:
            stats.steps += 1

            # 压缩必须放在发请求之前：等 400 报错回来就晚了，那时已经浪费了
            # 一次往返，而且错误信息不会告诉你是上下文超了。
            if self.history.needs_compaction():
                self.compact(stats)

            try:
                message = self.client.complete(
                    messages=self.history.to_api(),
                    tools=get_schemas(),
                    on_text=self.reporter.on_text,
                )
            except LLMError as exc:
                self.reporter.on_error(str(exc))
                return stats
            except KeyboardInterrupt:
                self.reporter.on_notice("已中断本轮请求")
                return stats
            finally:
                self.reporter.on_text_end()

            stats.tokens += message.usage.get("total_tokens", 0)
            self.history.total_tokens = stats.tokens
            self.history.note_prompt_tokens(message.usage.get("prompt_tokens", 0))
            self.history.add_assistant(message.to_api())

            # 出口 1：模型不再需要工具，本轮结束
            if not message.tool_calls:
                return stats

            self._run_tools(message, stats)

        # 出口 2：撞上步数上限。
        # 这里不需要往历史里补任何东西：_run_tools() 一定会在循环条件失败之前
        # 跑完，每个 tool_call 都已经有了对应的 tool 消息，历史本身是完整的。
        # 所以只提示用户，历史原样保留——下一轮用户追一句"继续"就能接着干。
        self.reporter.on_notice(
            f"已达到单轮步数上限（{self.config.max_steps} 步），自动停止。"
            "可以直接追加一句指令让它继续。"
        )
        return stats

    def compact(self, stats: TurnStats | None = None) -> CompactionResult:
        """把早期对话摘要掉，腾出上下文空间。也供 /compact 手动调用。"""
        before_tokens = self.history.estimated_tokens()
        self._last_summary_tokens = 0
        result = self.history.compact(self._summarize)
        if stats is not None:
            stats.compaction_tokens += self._last_summary_tokens

        if result.compacted:
            self.reporter.on_notice(
                f"上下文已压缩：{result.messages_before} 条消息 → "
                f"{result.messages_after} 条，"
                f"约 {before_tokens} → {self.history.estimated_tokens()} tokens"
            )
        elif result.reason:
            self.reporter.on_notice(f"未压缩：{result.reason}")
        return result

    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """用模型自己给早期对话写摘要。

        刻意不带 tools 参数：带上的话模型很可能"接着干活"直接发起新的工具调用，
        而不是老老实实写摘要。这也是为什么摘要请求把对话平铺成纯文本传入。
        """
        response = self.client.complete(
            messages=[{"role": "user", "content": build_summary_request(messages)}],
            tools=None,
            on_text=None,
        )
        self._last_summary_tokens = response.usage.get("total_tokens", 0)
        return response.content

    def _run_tools(self, message: AssistantMessage, stats: TurnStats) -> None:
        """依次执行本轮的所有工具调用。

        这里刻意串行执行：模型一次给出的多个调用之间可能存在写-读依赖，
        并行会引入顺序不确定性。等后面区分出只读工具后再考虑并发。
        """
        for call in message.tool_calls:
            stats.tool_calls += 1
            self.reporter.on_tool_start(call.name, call.arguments)

            try:
                result = dispatch(call.name, call.arguments, self.ctx)
            except KeyboardInterrupt:
                result = ToolResult("用户中断了这次工具执行。", is_error=True, summary="已中断")

            self.reporter.on_tool_end(call.name, result)
            # 无论成功失败都必须回灌一条 tool 消息，与 tool_call_id 一一对应
            self.history.add_tool_result(call.id, result.content)
