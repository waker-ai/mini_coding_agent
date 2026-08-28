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

    request_timeout: float = 120.0
    max_retries: int = 3

    # ask=写操作逐次确认（默认）、auto=全自动、readonly=拒绝一切写操作
    permission_mode: PermissionMode = field(default=PermissionMode.ASK)

    @classmethod
    def from_env(cls, workspace: Path | None = None, permission_mode: PermissionMode | None = None) -> "Config":
        ws = (workspace or Path.cwd()).resolve()
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
        )
