"""会话持久化：让 agent 能接着上次的对话继续干。

三个设计决定（面试要能讲清楚）：

1. **不保存 system prompt，恢复时重建。**
   系统提示词里嵌了启动那一刻的目录概览。上次会话结束后文件可能已经增删，
   把旧快照原样恢复回来，模型会拿着过期的目录印象干活，比没有更糟。
   历史里的对话内容照旧保留——它记录的是"做过什么"，不会过期。

2. **按工作目录分别存档。**
   同一个 agent 会在不同项目里用，共用一份历史只会互相污染。
   存档名用「目录名 + 绝对路径哈希」，既可读，又不会因为两个项目重名而撞车。

3. **原子写入。**
   每轮任务结束都要落盘，而落盘过程中程序被 Ctrl+C 打断是完全可能的。
   先写临时文件再 os.replace 覆盖，保证存档要么是上一个完整版本，
   要么是这一个完整版本，不会出现写了一半的坏 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .config import PACKAGE_ROOT

SESSIONS_DIR = PACKAGE_ROOT / ".sessions"
FORMAT_VERSION = 1


def session_path(workspace: Path) -> Path:
    """存档路径。目录名便于人眼辨认，哈希保证唯一。"""
    absolute = str(Path(workspace).resolve())
    digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:12]
    safe_name = "".join(c for c in Path(absolute).name if c.isalnum() or c in "-_") or "workspace"
    return SESSIONS_DIR / f"{safe_name}-{digest}.json"


def save(workspace: Path, messages: list[dict[str, Any]], total_tokens: int = 0) -> None:
    """把对话历史落盘。失败不抛异常——存档失败不该让整轮任务白干。"""
    target = session_path(workspace)
    payload = {
        "version": FORMAT_VERSION,
        "workspace": str(Path(workspace).resolve()),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_tokens": total_tokens,
        "messages": messages,
    }
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temporary, target)  # 原子替换
    except OSError:
        pass


def load(workspace: Path) -> dict[str, Any] | None:
    """读取存档。文件缺失、损坏或版本不符都返回 None，让调用方开新会话。"""
    target = session_path(workspace)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if payload.get("version") != FORMAT_VERSION:
        return None
    if not isinstance(payload.get("messages"), list):
        return None
    return payload


def clear(workspace: Path) -> bool:
    target = session_path(workspace)
    try:
        target.unlink()
        return True
    except OSError:
        return False


def describe(payload: dict[str, Any]) -> str:
    """给用户看的一行摘要。"""
    messages = payload.get("messages") or []
    turns = sum(1 for m in messages if m.get("role") == "user")
    return (
        f"{payload.get('saved_at', '未知时间')} · "
        f"{turns} 轮对话 · {len(messages)} 条消息"
    )
