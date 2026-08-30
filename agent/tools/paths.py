"""路径沙箱。

所有涉及文件的工具都必须先过 resolve()。模型有可能被文件内容里的指令带偏，
也可能自己拼错路径，因此边界检查放在工具层而不是靠提示词约束——
提示词是建议，代码才是约束。

两道检查：
  1. 目录边界：路径必须落在工作目录内；
  2. 敏感文件：即使在工作目录内，凭据类文件也一律拒绝。
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from .base import ToolContext, ToolError

# 凭据类文件，即使位于工作目录内也不允许读写。
#
# 光有目录边界是不够的：.env 就躺在工作目录里，agent 完全可以读出来并把
# 密钥原样打印进对话——而对话可能出现在终端录屏、Web 界面或日志里。
# 这类文件对完成编程任务几乎没有价值，代价却是密钥泄露，所以直接一刀切拒绝。
SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
)

# 模板文件不含真实凭据，本来就要入库，放行
SENSITIVE_EXCEPTIONS = (".env.example", ".env.sample", ".env.template")


def is_sensitive(name: str) -> bool:
    lowered = name.lower()
    if lowered in SENSITIVE_EXCEPTIONS:
        return False
    return any(fnmatch(lowered, pattern) for pattern in SENSITIVE_PATTERNS)


def resolve(ctx: ToolContext, path_str: str) -> Path:
    """把模型给的路径解析为绝对路径，确保它落在工作目录内且不是凭据文件。"""
    if not isinstance(path_str, str) or not path_str.strip():
        raise ToolError("path 必须是非空字符串")

    raw = Path(path_str.strip())
    target = raw.resolve() if raw.is_absolute() else (ctx.workspace / raw).resolve()

    if target != ctx.workspace and ctx.workspace not in target.parents:
        raise ToolError(
            f"拒绝访问工作目录之外的路径：{path_str}（工作目录：{ctx.workspace}）"
        )

    if is_sensitive(target.name):
        raise ToolError(
            f"拒绝访问凭据类文件：{target.name}。"
            "这类文件可能包含 API key 等机密，不会提供给模型，也不允许修改。"
        )

    return target


def display(ctx: ToolContext, path: Path) -> str:
    """转成相对工作目录的短路径，用于回显，避免把绝对路径塞满上下文。"""
    try:
        rel = path.relative_to(ctx.workspace)
    except ValueError:
        return str(path)
    return str(rel).replace("\\", "/") or "."
