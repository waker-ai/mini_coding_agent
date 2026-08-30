"""运行时配置。

凭据一律从环境变量 / .env 读取，绝不写进仓库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .tools.permissions import PermissionMode

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"

# agent 包所在仓库的根目录（config.py 是 agent/config.py，上两级就是仓库根）
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """极简 .env 解析。

    自己写而不是引入 python-dotenv，一是少一个依赖，二是语义完全可控：
    真实环境变量优先级高于 .env，不会被文件里的旧值覆盖。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(slots=True)
class Config:
    api_key: str
    workspace: Path

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.0

    # 单轮任务内最多允许的模型调用次数，是循环终止条件之一（见 loop.py）
    max_steps: int = 40
    # 单条工具结果回灌给模型的字符上限，防止一次 grep 就把上下文打满
    max_tool_output: int = 20_000

    # 历史估算超过这个 token 数就触发压缩。取值明显低于模型上下文窗口，
    # 因为估算有误差，而且要给"这一轮的响应 + 接下来几个工具结果"留出余量。
    compact_threshold: int = 40_000
    # 压缩时至少保留最近这么多条消息不动，保证当前正在做的事不被摘要掉
    keep_recent_messages: int = 8

    request_timeout: float = 120.0
    max_retries: int = 3

    # ask=写操作逐次确认（默认）、auto=全自动、readonly=拒绝一切写操作
    permission_mode: PermissionMode = field(default=PermissionMode.ASK)

    # from_env 是否自动创建了 workspace 目录，供 cli.py 决定要不要提示用户
    workspace_created: bool = field(default=False)

    @classmethod
    def from_env(cls, workspace: Path | None = None, permission_mode: PermissionMode | None = None) -> "Config":
        ws = (workspace or Path.cwd()).resolve()

        # workspace 不存在时，list_dir 等工具会在运行时反复报错——而且报错文案
        # 天生说不清楚，因为 display() 把"等于 workspace 自身"的路径显示成
        # 相对路径 "."，模型看到"目录不存在：."完全猜不出问题出在 workspace
        # 本身。与其让模型在运行时摸索着撞出这个坑，不如在启动时一次性校验掉。
        # 自动创建而不是直接报错退出，是为了和 write_file 已有的
        # mkdir(parents=True) 行为保持一致——不然用户会遇到"这次报错、
        # 下次却因为文件已经建出来而正常"的不一致体验。
        if ws.exists() and not ws.is_dir():
            raise SystemExit(f"工作目录不是一个目录：{ws}")
        workspace_created = not ws.exists()
        if workspace_created:
            ws.mkdir(parents=True, exist_ok=True)

        # .env 存的是「这个 agent 装置」的凭据，不是「被操作项目」的凭据，
        # 所以按 agent 自身仓库根目录找，而不是按 --workspace 指向的目标项目找——
        # 否则 -C 到别的项目跑一次就得在那边也放一份 key，且很容易忘记 .gitignore。
        load_dotenv(PACKAGE_ROOT / ".env")

        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise SystemExit(
                "未找到 DEEPSEEK_API_KEY。\n"
                "请把 .env.example 复制为 .env 并填入你的 key，"
                "或在终端设置环境变量后重试。"
            )

        return cls(
            api_key=api_key,
            workspace=ws,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            permission_mode=permission_mode or PermissionMode.ASK,
            workspace_created=workspace_created,
        )
