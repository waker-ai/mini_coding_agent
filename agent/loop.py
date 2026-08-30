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
    def __init__(self, config: Config, reporter: Reporter) -> None:
        self.config = config
        self.reporter = reporter
        self.client = LLMClient(config)
        self._last_summary_tokens = 0
        self.permissions = Permissions(mode=config.permission_mode, asker=reporter.ask_approval)
        self.ctx = ToolContext(workspace=config.workspace, permissions=self.permissions)
        self.history = History(
            system_prompt=build_system_prompt(config.workspace, self._overview()),
            max_tool_output=config.max_tool_output,
            compact_threshold=config.compact_threshold,
            keep_recent=config.keep_recent_messages,
        )

    def _overview(self) -> str:
        """开场就把目录结构塞进系统提示，省掉模型的第一次探路调用。"""
        try:
            return list_dir(self.ctx, ".", depth=2)
        except Exception:  # noqa: BLE001 - 概览失败不该阻止 agent 启动
            return "（无法读取目录结构）"

    def run_turn(self, user_input: str) -> TurnStats:
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

        # 出口 2：撞上步数上限。同样要给模型留一条 tool 之外的记录，
        # 否则下一轮历史里会出现"有 tool_call 却没有结果"的空洞。
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
