"""只读文件工具：read_file / list_dir。

写入类工具（write_file、edit_file）需要用户确认机制配合，放在后续提交里做。
"""
from __future__ import annotations

from pathlib import Path

from .base import ToolContext, ToolError, tool
from .paths import display, resolve

# 列目录时直接跳过的噪音目录，避免把 node_modules 之类灌进上下文
IGNORED = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build", ".idea", ".vscode",
}

MAX_READ_LINES = 2000
MAX_LINE_CHARS = 500


@tool(
    name="read_file",
    description=(
        "读取工作目录内某个文本文件的内容，返回带行号的文本。"
        "文件很大时用 offset/limit 分段读取。修改文件前必须先读取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对于工作目录，例如 src/main.py",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号，从 1 开始，默认 1",
            },
            "limit": {
                "type": "integer",
                "description": f"最多读取的行数，默认 {MAX_READ_LINES}",
            },
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    target = resolve(ctx, path)

    if not target.exists():
        raise ToolError(f"文件不存在：{display(ctx, target)}")
    if target.is_dir():
        raise ToolError(f"{display(ctx, target)} 是目录，请改用 list_dir")

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"{display(ctx, target)} 不是 UTF-8 文本文件（可能是二进制），无法读取"
        ) from None

    offset = max(1, int(offset))
    limit = max(1, min(int(limit), MAX_READ_LINES))

    lines = text.splitlines()
    if not lines:
        return f"（{display(ctx, target)} 是空文件）"
    if offset > len(lines):
        raise ToolError(
            f"offset={offset} 超出文件范围，{display(ctx, target)} 共 {len(lines)} 行"
        )

    selected = lines[offset - 1 : offset - 1 + limit]
    body = "\n".join(
        f"{offset + i:>6}\t{_clip(line)}" for i, line in enumerate(selected)
    )

    end = offset - 1 + len(selected)
    header = f"{display(ctx, target)}（第 {offset}-{end} 行，共 {len(lines)} 行）"
    footer = ""
    if end < len(lines):
        footer = f"\n\n… 文件还有 {len(lines) - end} 行未显示，可用 offset={end + 1} 继续读取。"
    return f"{header}\n{body}{footer}"


@tool(
    name="list_dir",
    description=(
        "列出工作目录内某个目录下的文件与子目录，用于了解项目结构。"
        "会自动跳过 .git、node_modules 等噪音目录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径，相对于工作目录，默认为工作目录本身",
            },
            "depth": {
                "type": "integer",
                "description": "递归深度，默认 2，最大 5",
            },
        },
        "required": [],
    },
)
def list_dir(ctx: ToolContext, path: str = ".", depth: int = 2) -> str:
    target = resolve(ctx, path)

    if not target.exists():
        raise ToolError(f"目录不存在：{display(ctx, target)}")
    if not target.is_dir():
        raise ToolError(f"{display(ctx, target)} 不是目录，请改用 read_file")

    depth = max(1, min(int(depth), 5))
    lines: list[str] = [f"{display(ctx, target)}/"]
    truncated = _walk(target, depth, 0, lines)

    if len(lines) == 1:
        return f"{display(ctx, target)}/ 是空目录"
    if truncated:
        lines.append("… 条目过多，已截断")
    return "\n".join(lines)


def _walk(directory: Path, depth: int, level: int, out: list[str], limit: int = 300) -> bool:
    if level >= depth:
        return False
    try:
        entries = sorted(
            directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
    except PermissionError:
        out.append("  " * (level + 1) + "（无权限访问）")
        return False

    for entry in entries:
        if entry.name in IGNORED:
            continue
        if len(out) >= limit:
            return True
        indent = "  " * (level + 1)
        if entry.is_dir():
            out.append(f"{indent}{entry.name}/")
            if _walk(entry, depth, level + 1, out, limit):
                return True
        else:
            out.append(f"{indent}{entry.name}")
    return False


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …(本行已截断)"
