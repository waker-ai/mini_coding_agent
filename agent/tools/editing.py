"""写入类工具：write_file / edit_file。

两者都 requires_approval=True，执行前会经过 permissions.request()，
由调用方（各 handler 自己）生成 diff 预览再申请确认——理由见 permissions.py
顶部注释：把"要不要确认""确认时给用户看什么"这两件事放在同一处，不散落。
"""
from __future__ import annotations

from .base import ToolContext, ToolError, tool
from .diffutil import count_changes, make_diff
from .paths import display, resolve
from .permissions import ApprovalRequest, Decision


def _ensure_allowed(ctx: ToolContext, req: ApprovalRequest) -> None:
    if ctx.permissions is None:
        return  # 没挂权限系统时（如单元测试）放行，不阻塞
    if ctx.permissions.request(req) is Decision.DENY:
        raise ToolError(ctx.permissions.last_denial)


@tool(
    name="write_file",
    description=(
        "创建新文件或整体覆盖已有文件的内容。适合新建文件，或改动大到不适合用 "
        "edit_file 做局部替换的情况。会先向用户展示 diff 并请求确认。"
        "修改已有文件优先用 edit_file，避免无意中丢掉未预期的内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录"},
            "content": {"type": "string", "description": "文件的完整内容"},
        },
        "required": ["path", "content"],
    },
    requires_approval=True,
)
def write_file(ctx: ToolContext, path: str, content: str) -> str:
    target = resolve(ctx, path)
    rel = display(ctx, target)

    old_text = ""
    is_new = not target.exists()
    if not is_new:
        if target.is_dir():
            raise ToolError(f"{rel} 是目录，无法写入")
        try:
            old_text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{rel} 不是 UTF-8 文本文件，拒绝覆盖以免损坏二进制内容") from None

    diff = make_diff(old_text, content, rel)
    label = "新建文件" if is_new else "覆盖文件"
    _ensure_allowed(ctx, ApprovalRequest(tool="write_file", title=f"{label} {rel}", detail=diff))

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"已{label}：{rel}（{lines} 行）"


@tool(
    name="edit_file",
    description=(
        "在已有文件中做一次精确的字符串替换：把 old_string 替换成 new_string。"
        "old_string 必须在文件中唯一出现，因此要包含足够的上下文（比如整行乃至前后几行）"
        "以确保定位准确；如果替换失败，说明匹配不到或者不唯一，请先重新 read_file 确认原文。"
        "会先向用户展示 diff 并请求确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录"},
            "old_string": {"type": "string", "description": "要被替换的原文，必须在文件中唯一"},
            "new_string": {"type": "string", "description": "替换后的新内容"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    requires_approval=True,
)
def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str) -> str:
    target = resolve(ctx, path)
    rel = display(ctx, target)

    if not target.exists():
        raise ToolError(f"文件不存在：{rel}（新建文件请用 write_file）")
    if target.is_dir():
        raise ToolError(f"{rel} 是目录，无法编辑")
    if old_string == new_string:
        raise ToolError("old_string 和 new_string 相同，没有需要修改的内容")

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{rel} 不是 UTF-8 文本文件，拒绝编辑以免损坏二进制内容") from None

    count = text.count(old_string)
    if count == 0:
        raise ToolError(
            f"在 {rel} 中找不到指定的 old_string。"
            "请重新 read_file 核对原文（可能有空白字符差异或文件已被改动）。"
        )
    if count > 1:
        raise ToolError(
            f"old_string 在 {rel} 中出现了 {count} 次，不是唯一匹配。"
            "请扩大 old_string 的上下文（多带几行）使其唯一。"
        )

    new_text = text.replace(old_string, new_string, 1)
    diff = make_diff(text, new_text, rel)
    _ensure_allowed(ctx, ApprovalRequest(tool="edit_file", title=f"编辑 {rel}", detail=diff))

    target.write_text(new_text, encoding="utf-8")
    added, removed = count_changes(diff)
    return f"已编辑：{rel}（+{added} -{removed}）"
