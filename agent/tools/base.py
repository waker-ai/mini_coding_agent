"""工具注册表：工具定义、JSON Schema 生成、本地分发执行。

两个关键设计（面试要能讲清楚）：

1. Schema 手写而非从函数签名反射生成。
   工具描述是影响 agent 表现最大的变量之一，手写才能逐字调优措辞；
   反射方案会把描述质量绑死在 docstring 格式上，得不偿失。

2. dispatch() 永不向上抛异常。
   任何失败——工具不存在、参数不是合法 JSON、缺参、执行报错——都被翻译成
   一段自然语言错误文本，作为 tool 消息回灌给模型。agent 的鲁棒性来自
   "模型能看见错误并自己纠正"，而不是"程序不出错"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .permissions import Permissions


class ToolError(Exception):
    """工具的预期内失败（文件不存在、路径越界等）。会原样转述给模型。"""


@dataclass(slots=True)
class ToolContext:
    """工具执行时能看到的环境：工作目录 + 权限闸门。"""

    workspace: Path
    permissions: "Permissions | None" = None


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    # 给终端展示用的一行摘要，不回灌给模型
    summary: str = ""


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    # 是否需要用户确认后才执行（写文件、执行命令等破坏性工具置 True）
    requires_approval: bool = False

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    requires_approval: bool = False,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """把一个普通函数登记为模型可调用的工具。"""

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        if name in REGISTRY:
            raise RuntimeError(f"工具重名：{name}")
        REGISTRY[name] = Tool(name, description, parameters, fn, requires_approval)
        return fn

    return decorator


def get_schemas() -> list[dict[str, Any]]:
    """按 OpenAI tools 格式导出全部工具，直接塞进请求体。"""
    return [t.to_schema() for t in REGISTRY.values()]


def format_call(name: str, args: dict[str, Any]) -> str:
    """把一次工具调用渲染成人类可读的一行，用于终端展示。"""
    shown = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > 60:
            text = text[:57] + "..."
        shown.append(f"{key}={text!r}")
    return f"{name}({', '.join(shown)})"


def dispatch(name: str, raw_arguments: str, ctx: ToolContext) -> ToolResult:
    """执行一次工具调用。无论如何都返回 ToolResult，不抛异常。"""
    tool_def = REGISTRY.get(name)
    if tool_def is None:
        available = "、".join(REGISTRY) or "（无）"
        return ToolResult(
            f"错误：不存在名为 {name!r} 的工具。可用工具：{available}",
            is_error=True,
            summary=f"未知工具 {name}",
        )

    # 模型偶尔会吐出截断或非法的 JSON，这里必须兜住并让它重试
    try:
        args = json.loads(raw_arguments) if raw_arguments.strip() else {}
    except json.JSONDecodeError as exc:
        return ToolResult(
            f"错误：{name} 的参数不是合法 JSON（{exc}）。收到的原文：{raw_arguments[:500]}\n"
            "请重新生成一次格式正确的调用。",
            is_error=True,
            summary="参数 JSON 解析失败",
        )
    if not isinstance(args, dict):
        return ToolResult(
            f"错误：{name} 的参数必须是 JSON 对象，收到的是 {type(args).__name__}。",
            is_error=True,
            summary="参数类型错误",
        )

    try:
        content = tool_def.handler(ctx, **args)
    except ToolError as exc:
        return ToolResult(f"错误：{exc}", is_error=True, summary=str(exc))
    except TypeError as exc:
        # 少传/多传参数会走到这里
        return ToolResult(
            f"错误：调用 {name} 的参数不匹配（{exc}）。请对照工具定义修正后重试。",
            is_error=True,
            summary="参数不匹配",
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都不能让 agent 崩掉
        return ToolResult(
            f"错误：{name} 执行时发生未预期的异常：{type(exc).__name__}: {exc}",
            is_error=True,
            summary=f"{type(exc).__name__}",
        )

    return ToolResult(content, summary=_summarize(content))


def _summarize(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return "（空结果）"
    if len(lines) == 1:
        return lines[0][:80]
    return f"{lines[0][:60]}…（共 {len(lines)} 行）"
