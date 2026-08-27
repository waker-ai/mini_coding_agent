"""路径沙箱。

所有涉及文件的工具都必须先过 resolve()。模型有可能被文件内容里的指令带偏，
也可能自己拼错路径，因此边界检查放在工具层而不是靠提示词约束——
提示词是建议，代码才是约束。
"""
from __future__ import annotations

from pathlib import Path

from .base import ToolContext, ToolError


def resolve(ctx: ToolContext, path_str: str) -> Path:
    """把模型给的路径解析为绝对路径，并确保它落在工作目录内。"""
    if not isinstance(path_str, str) or not path_str.strip():
        raise ToolError("path 必须是非空字符串")

    raw = Path(path_str.strip())
    target = raw.resolve() if raw.is_absolute() else (ctx.workspace / raw).resolve()

    if target != ctx.workspace and ctx.workspace not in target.parents:
        raise ToolError(
            f"拒绝访问工作目录之外的路径：{path_str}（工作目录：{ctx.workspace}）"
        )
    return target


def display(ctx: ToolContext, path: Path) -> str:
    """转成相对工作目录的短路径，用于回显，避免把绝对路径塞满上下文。"""
    try:
        rel = path.relative_to(ctx.workspace)
    except ValueError:
        return str(path)
    return str(rel).replace("\\", "/") or "."
