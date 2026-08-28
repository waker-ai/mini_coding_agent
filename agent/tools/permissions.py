"""权限与用户确认。

写文件、执行命令这类操作不可逆，必须有一道人在回路的闸门。设计上分三档：

  ask       默认。破坏性工具每次执行前都要用户点头，可以选"本会话总是允许"。
  auto      全自动，适合演示和信得过的任务。但仍保留 run_command 的硬性黑名单
            （见 shell.py），auto 不等于无底线。
  readonly  只读模式，直接拒绝一切破坏性工具，用来让 agent 先分析不动手。

具体调用 Permissions.request() 的时机由各工具 handler 自己决定（见
editing.py / shell.py），不是在 dispatch() 里按 requires_approval 统一拦截。
原因：不同工具需要在确认框里展示的内容不一样（diff / 命令原文），
统一在 dispatch() 里做只能弹一个内容空洞的确认框。requires_approval
这个字段目前只做工具自描述用，新增写入类工具时别忘了在 handler 内部
显式调用 ctx.permissions.request()。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class PermissionMode(str, Enum):
    ASK = "ask"
    AUTO = "auto"
    READONLY = "readonly"


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(slots=True)
class ApprovalRequest:
    """一次确认请求。detail 通常是 diff 或待执行的命令全文。"""

    tool: str
    title: str
    detail: str = ""


# UI 层实现：展示请求并返回 "y"（允许一次）/ "a"（本会话总是允许该工具）/ "n"（拒绝）
Asker = Callable[[ApprovalRequest], str]


@dataclass(slots=True)
class Permissions:
    mode: PermissionMode = PermissionMode.ASK
    asker: Asker | None = None
    # 本会话内被用户放行的工具名
    always_allowed: set[str] = field(default_factory=set)
    last_denial: str = ""

    def request(self, req: ApprovalRequest) -> Decision:
        if self.mode is PermissionMode.READONLY:
            self.last_denial = (
                "当前处于只读模式，禁止写文件或执行命令。"
                "请只做分析和说明，不要再尝试调用这类工具。"
            )
            return Decision.DENY

        if self.mode is PermissionMode.AUTO or req.tool in self.always_allowed:
            return Decision.ALLOW

        if self.asker is None:
            # 没有可用的交互通道时保守拒绝（fail closed），绝不默默放行
            self.last_denial = "当前会话无法向用户请求确认，已拒绝该操作。"
            return Decision.DENY

        answer = (self.asker(req) or "n").strip().lower()
        if answer in {"a", "always"}:
            self.always_allowed.add(req.tool)
            return Decision.ALLOW
        if answer in {"y", "yes", ""}:
            return Decision.ALLOW

        self.last_denial = (
            f"用户拒绝了这次 {req.tool} 操作。不要重复同样的调用，"
            "换一种做法，或者直接询问用户希望怎么处理。"
        )
        return Decision.DENY
