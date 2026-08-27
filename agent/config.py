"""运行时配置。

凭据一律从环境变量 / .env 读取，绝不写进仓库。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


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

    @classmethod
    def from_env(cls, workspace: Path | None = None) -> "Config":
        ws = (workspace or Path.cwd()).resolve()
        load_dotenv(ws / ".env")

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
        )
