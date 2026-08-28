"""统一 diff 生成，供确认预览和工具返回值复用。"""
from __future__ import annotations

import difflib


def make_diff(old: str, new: str, path: str, context: int = 3, max_lines: int = 200) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=context,
        )
    )
    if not lines:
        return "（内容没有变化）"
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"… diff 过长，省略剩余 {omitted} 行"]
    return "\n".join(lines)


def count_changes(diff: str) -> tuple[int, int]:
    """统计新增/删除行数，用于给用户一行摘要。"""
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return added, removed
