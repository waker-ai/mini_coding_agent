"""对话历史管理与上下文压缩。

三件事：
  1. 维护消息列表，system 消息单独持有，任何压缩都不会把它弄丢；
  2. 单条工具结果过长时截断（保留头尾）；
  3. 历史逼近上下文上限时，把早期消息交给模型摘要成一条，替换掉原文。

压缩这件事真正的难点不是"生成摘要"，而是"从哪里切"。OpenAI 兼容协议有一条
硬约束：每条 role="tool" 的消息都必须能在它前面找到一条带有对应 tool_call_id
的 assistant 消息。一旦从中间随便切一刀，把 assistant 留在了被摘要掉的那半边、
tool 结果留在保留的这半边，整个请求会被服务端直接拒绝（400），而且报错信息
很难让人联想到是压缩逻辑切坏了。所以 _find_safe_cut() 是这个模块的核心。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 摘要生成器：接收待压缩的消息列表，返回一段纯文本摘要
Summarizer = Callable[[list[dict[str, Any]]], str]


@dataclass(slots=True)
class CompactionResult:
    compacted: bool
    messages_before: int = 0
    messages_after: int = 0
    reason: str = ""


class History:
    def __init__(
        self,
        system_prompt: str,
        max_tool_output: int,
        compact_threshold: int = 40_000,
        keep_recent: int = 8,
    ) -> None:
        self._system = {"role": "system", "content": system_prompt}
        self._messages: list[dict[str, Any]] = []
        self.max_tool_output = max_tool_output
        # 历史（含 system）超过这个 token 数就触发压缩
        self.compact_threshold = compact_threshold
        # 压缩时至少保留最近这么多条消息不动
        self.keep_recent = keep_recent

        self.total_tokens = 0
        # API 每次返回的 prompt_tokens，就是"上次发出去的历史有多大"的权威值
        self._last_prompt_tokens = 0
        # 自上次 API 调用以来新追加内容的字符数，用于估算当前实际大小
        self._chars_since_measure = 0

    # ---------- 基本读写 ----------

    def to_api(self) -> list[dict[str, Any]]:
        """system 消息永远单独持有，保证任何压缩策略都不会把它弄丢。"""
        return [self._system, *self._messages]

    def add_user(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def add_assistant(self, message: dict[str, Any]) -> None:
        self._append(message)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": self._truncate(content),
            }
        )

    def clear(self) -> None:
        self._messages.clear()
        self.total_tokens = 0
        self._last_prompt_tokens = 0
        self._chars_since_measure = 0

    def _append(self, message: dict[str, Any]) -> None:
        self._messages.append(message)
        self._chars_since_measure += _message_chars(message)

    def __len__(self) -> int:
        return len(self._messages)

    # ---------- token 计量 ----------

    def note_prompt_tokens(self, prompt_tokens: int) -> None:
        """记录 API 报告的真实 prompt_tokens。

        不自己跑分词器：DeepSeek 没有公开可离线使用的 tokenizer，硬套 tiktoken
        在中文上的误差可以到 30% 以上。而每次响应里的 prompt_tokens 正好就是
        "刚刚发出去的这份历史有多大"的权威答案，白给的精确值没有理由不用。
        """
        if prompt_tokens > 0:
            self._last_prompt_tokens = prompt_tokens
            self._chars_since_measure = 0

    def estimated_tokens(self) -> int:
        """当前历史的估算大小 = 上次实测值 + 之后新增内容的粗略估计。

        粗略估计只用于"要不要触发压缩"这个二元判断，且阈值本身留了余量，
        所以按 2.5 字符/token 一刀切就够了（中文约 1、英文约 4，取中间值），
        没必要为此引入一个重量级依赖。
        """
        return self._last_prompt_tokens + int(self._chars_since_measure / 2.5)

    def needs_compaction(self) -> bool:
        return self.estimated_tokens() >= self.compact_threshold

    # ---------- 压缩 ----------

    def compact(self, summarizer: Summarizer) -> CompactionResult:
        """把早期消息摘要成一条，替换原文。"""
        before = len(self._messages)
        if before <= self.keep_recent:
            return CompactionResult(False, before, before, "消息条数太少，无需压缩")

        cut = self._find_safe_cut(len(self._messages) - self.keep_recent)
        if cut <= 0:
            return CompactionResult(False, before, before, "找不到安全的切割点，跳过本次压缩")

        older, recent = self._messages[:cut], self._messages[cut:]

        try:
            summary = summarizer(older).strip()
        except Exception as exc:  # noqa: BLE001 - 压缩失败不能让整轮任务挂掉
            return CompactionResult(False, before, before, f"摘要生成失败，保持原样：{exc}")
        if not summary:
            return CompactionResult(False, before, before, "摘要为空，保持原样")

        # 摘要用 user 角色注入：assistant 角色会让模型误以为这些话是它自己说的，
        # 而在对话中段插入第二条 system 消息在各家兼容实现上表现并不一致。
        # user 角色是最稳妥、各家都支持的"外部信息注入"方式。
        digest = {
            "role": "user",
            "content": (
                "【以下是之前对话的摘要，原始消息已因上下文长度限制被省略】\n"
                f"{summary}\n"
                "【摘要结束，请基于以上信息继续当前任务】"
            ),
        }
        self._messages = [digest, *recent]

        # 实测值已经失效（它对应的是压缩前的历史），重新按字符数估一个，
        # 否则下一轮会拿着旧的大数字立刻再次触发压缩。
        self._last_prompt_tokens = 0
        self._chars_since_measure = sum(
            _message_chars(m) for m in [self._system, *self._messages]
        )
        return CompactionResult(True, before, len(self._messages))

    def _find_safe_cut(self, target: int) -> int:
        """从 target 开始向后找一个不会切断 tool_call 配对的切割点。

        规则：切割点之后的第一条消息不能是 role="tool"，否则它引用的
        tool_call_id 会随着被摘要掉的 assistant 消息一起消失，请求必然被拒。
        优先切在 user 消息处（一轮任务的自然边界，语义最完整）；
        找不到就退而求其次切在 assistant 消息处。
        """
        if target <= 0:
            return 0

        for index in range(target, len(self._messages)):
            if self._messages[index].get("role") == "user":
                return index

        for index in range(target, len(self._messages)):
            if self._messages[index].get("role") != "tool":
                return index

        return 0  # 整段尾巴都是 tool 消息，放弃这次压缩

    def _truncate(self, content: str) -> str:
        """超长结果保留头尾。

        中间部分往往是重复内容（大量日志、长文件），头尾则包含命令回显和
        最终的报错，是模型最需要的信息。
        """
        limit = self.max_tool_output
        if len(content) <= limit:
            return content
        head = content[: limit // 2]
        tail = content[-limit // 2 :]
        omitted = len(content) - limit
        return f"{head}\n\n…（中间省略 {omitted} 个字符）…\n\n{tail}"


def _message_chars(message: dict[str, Any]) -> int:
    """粗略统计一条消息的字符数，tool_calls 的参数也要算进去。"""
    total = len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("name") or "") + len(function.get("arguments") or "")
    return total
