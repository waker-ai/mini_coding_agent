"""grep 工具：在工作目录内做文本搜索。

不 shell 出去调系统 grep/ripgrep，原因很直接：题目要求不依赖服务端托管的
执行环境，本地能用的 grep/rg 在不同机器上是否存在也不确定（尤其 Windows），
纯 Python 实现虽然慢一点，但零依赖、跨平台行为一致，6 天工期内这笔账划算。
"""
from __future__ import annotations

import re

from .base import ToolContext, ToolError, tool
from .filesystem import IGNORED
from .paths import display, is_sensitive, resolve

MAX_MATCHES = 200
MAX_FILE_BYTES = 2_000_000  # 超过约 2MB 的文件跳过，避免卡在超大日志/资源文件上


@tool(
    name="grep",
    description=(
        "在工作目录内递归搜索匹配正则表达式的文本行，返回文件路径、行号与内容。"
        "适合定位符号定义、调用点或某段特定文本在项目中的位置。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（Python re 语法）"},
            "path": {"type": "string", "description": "搜索起点目录，默认为工作目录"},
            "glob": {
                "type": "string",
                "description": "只搜索匹配该文件名模式的文件，例如 *.py；默认搜索所有文本文件",
            },
            "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
        },
        "required": ["pattern"],
    },
)
def grep(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    glob: str = "",
    ignore_case: bool = False,
) -> str:
    root = resolve(ctx, path)
    if not root.exists():
        raise ToolError(f"路径不存在：{display(ctx, root)}")

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(f"正则表达式非法：{exc}") from None

    files = [root] if root.is_file() else _iter_files(root, glob)

    matches: list[str] = []
    files_scanned = 0
    truncated = False

    for file_path in files:
        if len(matches) >= MAX_MATCHES:
            truncated = True
            break
        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制文件或读取失败，静默跳过

        files_scanned += 1
        rel = display(ctx, file_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                snippet = line if len(line) <= 300 else line[:300] + "…"
                matches.append(f"{rel}:{lineno}: {snippet}")
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break

    if not matches:
        return f"未找到匹配 /{pattern}/ 的内容（已扫描 {files_scanned} 个文件）"

    header = f"共找到 {len(matches)} 处匹配（扫描了 {files_scanned} 个文件）"
    if truncated:
        header += f"，已达上限 {MAX_MATCHES} 条，结果可能不全，建议缩小搜索范围"
    return header + "\n" + "\n".join(matches)


def _iter_files(root, glob_pattern: str):
    pattern = glob_pattern.strip() or "*"
    for path in root.rglob(pattern):
        if not path.is_file():
            continue
        if any(part in IGNORED for part in path.relative_to(root).parts[:-1]):
            continue
        # grep 直接读文件内容，绕过了 resolve()，所以这里要单独挡一次凭据文件，
        # 否则一句 grep 就能把 .env 里的 key 捞出来
        if is_sensitive(path.name):
            continue
        yield path
