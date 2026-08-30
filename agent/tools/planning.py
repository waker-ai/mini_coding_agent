"""todo_write：让 agent 把多步任务拆成清单并自己维护进度。

为什么值得单独加一个工具？多步任务里模型很容易"做着做着忘了还有什么没做"，
尤其是中途被工具报错带偏之后。把计划显式写进对话历史，等于给模型一个
随时可查的锚点；对用户来说，也终于能看见 agent 打算干什么、干到哪一步了。

代价是多一个工具就多一种模型可能用错的地方，所以描述里明确写了
"三步以上才用"，避免它给「读个文件」这种一步任务也列个清单。

状态机刻意约束成「同时最多一项 in_progress」：允许多项并行的话，模型会把
所有条目一次性标成进行中，清单就退化成一份静态列表，失去了跟踪进度的意义。
"""
from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolError, tool

VALID_STATUS = ("pending", "in_progress", "completed")
MARKS = {"pending": "○", "in_progress": "◐", "completed": "●"}


@tool(
    name="todo_write",
    description=(
        "把当前任务拆成一份待办清单并维护它的进度。"
        "适用于需要三步以上才能完成的任务：先一次性列出全部步骤，"
        "然后每开始做一步就把它标为 in_progress、做完立刻标为 completed，"
        "并在同一次调用里带上完整的清单（每次都要传全量，不是增量）。"
        "同一时刻最多只能有一项 in_progress。"
        "一两步就能做完的简单任务不要用这个工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整的待办清单，每次调用都要传全量",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "这一步要做什么，用简短的祈使句，例如「补充 grep 的单元测试」",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(VALID_STATUS),
                            "description": "pending 未开始 / in_progress 进行中 / completed 已完成",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
)
def todo_write(ctx: ToolContext, todos: list[dict[str, Any]]) -> str:
    if not isinstance(todos, list) or not todos:
        raise ToolError("todos 必须是非空数组")

    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"第 {index} 项不是对象")
        content = str(item.get("content", "")).strip()
        status = str(item.get("status", "")).strip()
        if not content:
            raise ToolError(f"第 {index} 项缺少 content")
        if status not in VALID_STATUS:
            raise ToolError(
                f"第 {index} 项的 status 是 {status!r}，只能是 {'/'.join(VALID_STATUS)}"
            )
        cleaned.append({"content": content, "status": status})

    running = [item for item in cleaned if item["status"] == "in_progress"]
    if len(running) > 1:
        raise ToolError(
            f"有 {len(running)} 项同时是 in_progress，最多只能有一项。"
            "请把当前真正在做的那一项保留为 in_progress，其余改回 pending 或 completed。"
        )

    ctx.todos = cleaned
    if ctx.on_todos is not None:
        ctx.on_todos(cleaned)

    done = sum(1 for item in cleaned if item["status"] == "completed")
    lines = [f"{MARKS[item['status']]} {item['content']}" for item in cleaned]
    return f"清单已更新（{done}/{len(cleaned)} 完成）：\n" + "\n".join(lines)
