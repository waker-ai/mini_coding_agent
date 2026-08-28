"""run_command：执行 shell 命令。

风险最高的工具，两道防线叠加：
  1. 硬性黑名单——即使在 auto 模式下也拦截，防止一次"自动化演示"翻车成删库。
     黑名单不追求穷尽（那是不可能的），只挡最常见、最灾难性的几类。
  2. 用户确认——ask 模式下每条命令都要过 permissions.request()，展示命令原文。

不做命令解析 / 白名单校验之类更复杂的沙箱，超出 6 天工期能负责地做完的范围；
这里的取舍是"防灾难性误操作"，不是"防恶意注入"——agent 本来就是要执行用户
下达的任意命令。

Shell 的选择踩过一个坑：`subprocess.run(cmd, shell=True)` 在 Windows 上
并不是调 PowerShell，而是 `%COMSPEC%`（即 cmd.exe）；如果提示词告诉模型
"这里是 PowerShell"，模型写出的 `$PSVersionTable` 之类语法在 cmd.exe 下
全部报错。所以 Windows 下改为显式拼 `powershell -NoProfile -Command`，
其它平台沿用 `shell=True` 默认调用的 `/bin/sh`，两边说明与实际行为对齐。
"""
from __future__ import annotations

import re
import subprocess
import sys

from .base import ToolContext, ToolError, tool
from .permissions import ApprovalRequest, Decision

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
MAX_OUTPUT_CHARS = 30_000

# 灾难性操作硬拦截，不受权限模式影响。正则尽量宽松地匹配常见写法/大小写/参数顺序。
BLOCKLIST = [
    (r"rm\s+.*-[a-z]*r[a-z]*f|rm\s+.*-[a-z]*f[a-z]*r", "递归强制删除（rm -rf 类）"),
    (r"del\s+/[sf].*/[sf]|rd\s+/s", "递归强制删除（del/rd 类，Windows）"),
    (r"format\s+[a-z]:", "格式化磁盘"),
    (r":\(\)\s*\{\s*:\|:", "fork 炸弹"),
    (r"git\s+push\s+.*--force|git\s+push\s+.*-f\b", "强制推送覆盖远程历史"),
    (r"git\s+reset\s+--hard", "硬重置，会丢弃未提交的修改"),
    (r">\s*/dev/sd[a-z]", "直接写磁盘设备"),
    (r"shutdown|Restart-Computer|Stop-Computer", "关机 / 重启系统"),
    (r"curl.*\|\s*(sh|bash)|wget.*\|\s*(sh|bash)", "下载脚本直接管道执行"),
]


def _blocked_reason(command: str) -> str | None:
    lowered = command.lower()
    for pattern, reason in BLOCKLIST:
        if re.search(pattern, lowered):
            return reason
    return None


@tool(
    name="run_command",
    description=(
        "在工作目录下执行一条命令（Windows 上用 PowerShell 运行，其它平台用 /bin/sh），"
        "返回 stdout、stderr 与退出码。用于跑测试、构建、安装依赖、查看命令行工具输出等。"
        "会先请求用户确认；命令若匹配删库、强制推送等高危模式会被直接拒绝，不受确认影响。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的完整命令"},
            "timeout": {
                "type": "integer",
                "description": f"超时时间（秒），默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
        },
        "required": ["command"],
    },
    requires_approval=True,
)
def run_command(ctx: ToolContext, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    if not command.strip():
        raise ToolError("command 不能为空")

    reason = _blocked_reason(command)
    if reason is not None:
        raise ToolError(
            f"该命令匹配高危操作规则（{reason}），已被拒绝执行，不会向用户请求确认。"
            "如果确实需要做这件事，请换一种影响范围更小、可逆的方式。"
        )

    if ctx.permissions is not None:
        decision = ctx.permissions.request(
            ApprovalRequest(tool="run_command", title="执行命令", detail=command)
        )
        if decision is Decision.DENY:
            raise ToolError(ctx.permissions.last_denial)

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))

    if sys.platform == "win32":
        # shell=True 在 Windows 上调的是 cmd.exe，不是 PowerShell；工具描述里
        # 承诺了 PowerShell 语义，这里就必须显式拼出 powershell.exe 来兑现它。
        argv: str | list[str] = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        use_shell = False
    else:
        argv = command
        use_shell = True

    try:
        proc = subprocess.run(
            argv,
            shell=use_shell,
            cwd=ctx.workspace,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"命令执行超过 {timeout} 秒，已终止") from None
    except OSError as exc:
        raise ToolError(f"无法启动命令：{exc}") from None

    stdout = _clip(proc.stdout or "")
    stderr = _clip(proc.stderr or "")
    parts = [f"退出码：{proc.returncode}"]
    parts.append(f"stdout:\n{stdout}" if stdout else "stdout:（空）")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n\n".join(parts)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    omitted = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:half]}\n\n…（中间省略 {omitted} 个字符）…\n\n{text[-half:]}"
