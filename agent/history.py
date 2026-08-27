"""对话历史管理。

现在只做两件基础的事：维护消息列表、把过长的工具结果截断后再入历史。
上下文压缩（超阈值时摘要化早期消息）留到后续提交，接口留在这里。
"""
from __future__ import annotations

from typing import Any


class History:
    def __init__(self, system_prompt: str, max_tool_output: int) -> None:
        self._system = {"role": "system", "content": system_prompt}
        self._messages: list[dict[str, Any]] = []
        self.max_tool_output = max_tool_output
        self.total_tokens = 0

    def to_api(self) -> list[dict[str, Any]]:
        """system 消息永远单独持有，保证任何压缩策略都不会把它弄丢。"""
        return [self._system, *self._messages]

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, message: dict[str, Any]) -> None:
        self._messages.append(message)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": self._truncate(content),
            }
        )

    def clear(self) -> None:
        self._messages.clear()
        self.total_tokens = 0

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

    def __len__(self) -> int:
        return len(self._messages)
