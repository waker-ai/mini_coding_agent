"""与 DeepSeek 的通信层：只用最底层的 chat.completions 接口。

DeepSeek 提供 OpenAI 兼容 API，所以直接复用 openai 这个 HTTP 客户端库。
注意题目的边界：客户端库可以用，但 agent 循环、工具执行、输出解析必须自己写，
所以这里刻意不碰任何 Assistants / tool_runner 之类"帮你跑循环"的封装。

本模块负责两件事：
  1. 把流式响应的碎片拼回一条完整的 assistant 消息（流式 tool_call 是分片下发的）；
  2. 网络层的重试与错误归一化。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import openai
from openai import OpenAI

from .config import Config


class LLMError(Exception):
    """重试之后仍然失败的 API 错误。"""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 未解析的 JSON 字符串，解析交给 tools.dispatch 统一兜错


@dataclass(slots=True)
class AssistantMessage:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        """转回 API 需要的消息格式，追加进对话历史。

        注意：assistant 消息里带了几个 tool_call，后面就必须跟几条对应
        tool_call_id 的 tool 消息，缺一条整轮请求都会被服务端拒绝。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class LLMClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.request_timeout,
            max_retries=0,  # 重试由本模块自己控制，便于打日志和区分错误类型
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage:
        """发一轮请求并返回拼装好的 assistant 消息。"""
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries):
            try:
                stream = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    tools=tools or None,
                    temperature=self.config.temperature,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                return self._consume(stream, on_text)
            except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError) as exc:
                # 可恢复错误：退避后重试
                last_error = exc
                if attempt < self.config.max_retries - 1:
                    time.sleep(2 ** attempt)
            except openai.APIStatusError as exc:
                if exc.status_code >= 500 and attempt < self.config.max_retries - 1:
                    last_error = exc
                    time.sleep(2 ** attempt)
                    continue
                # 4xx 是请求本身的问题，重试没有意义
                raise LLMError(f"API 返回 {exc.status_code}：{exc.message}") from exc

        raise LLMError(f"重试 {self.config.max_retries} 次后仍然失败：{last_error}")

    def _consume(
        self, stream: Iterable[Any], on_text: Callable[[str], None] | None
    ) -> AssistantMessage:
        """流式分片的拼装。

        文本片段可以边收边打印；tool_call 则是按 index 分片下发的——
        id 和 name 通常只在第一片出现，arguments 会被切成很多段陆续送达，
        所以必须按 index 建槽位累加，不能假设一片就是一次完整调用。
        """
        result = AssistantMessage()
        text_parts: list[str] = []
        slots: dict[int, dict[str, str]] = {}

        for chunk in stream:
            if getattr(chunk, "usage", None):
                result.usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                result.finish_reason = choice.finish_reason

            delta = choice.delta
            if delta is None:
                continue

            if delta.content:
                text_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)

            for fragment in delta.tool_calls or []:
                slot = slots.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                if fragment.id:
                    slot["id"] = fragment.id
                if fragment.function:
                    if fragment.function.name:
                        slot["name"] += fragment.function.name
                    if fragment.function.arguments:
                        slot["arguments"] += fragment.function.arguments

        result.content = "".join(text_parts)
        result.tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(slots.items())
        ]
        return result
