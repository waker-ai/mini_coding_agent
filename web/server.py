"""Web 前端的后端服务。

agent 必须跑在 Python 进程里——它要读写本地文件、执行 PowerShell 命令，这些
浏览器碰不了。所以浏览器只是显示层，这个模块做的事只有两件：把 agent 的事件流
推给浏览器，把浏览器的输入和确认回传给 agent。

核心是 WebReporter：它实现 loop.py 里的 Reporter 接口。当初把 UI 全部关在这个
接口后面的回报就在这里——agent 包一行代码没改，只是多了一个 Reporter 实现。

线程模型上有两个必须处理的点：

1. Agent.run_turn() 是同步阻塞的，绝不能直接在事件循环里跑。否则 WebSocket
   在整轮任务期间收不到任何消息——包括用户点“允许”的那条，确认框会永久卡死。
   所以 agent 跑在独立线程里。

2. 事件在 agent 线程里产生，但 WebSocket 属于事件循环。跨线程投递必须走
   loop.call_soon_threadsafe()，不能在工作线程里直接 await send。
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from agent.config import Config
from agent.loop import Agent, Reporter
from agent.tools import REGISTRY, ApprovalRequest, PermissionMode

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 用户迟迟不点确认时的等待上限。必须有：否则用户直接关掉页面，
# agent 线程会永久挂在 Event.wait() 上。超时按拒绝处理，
# 与权限系统 fail-closed 的原则保持一致。
APPROVAL_TIMEOUT = 300.0


class WebReporter(Reporter):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop = loop
        self._queue = queue
        self._gate: threading.Event | None = None
        self._answer = "n"
        # 由 websocket_endpoint 在 Agent 构造完成后回填，用于上报上下文占用
        self.context_probe = None

    # ---------- 跨线程投递 ----------

    def emit(self, event: dict[str, Any]) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _emit_context(self) -> None:
        if self.context_probe is None:
            return
        used, limit = self.context_probe()
        self.emit({"type": "context", "used": used, "limit": limit})

    # ---------- Reporter 接口 ----------

    def on_text(self, delta: str) -> None:
        self.emit({"type": "text", "delta": delta})

    def on_text_end(self) -> None:
        self.emit({"type": "text_end"})
        self._emit_context()

    def on_tool_start(self, name: str, arguments: str) -> None:
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            args = {"原始参数": arguments}
        self.emit({"type": "tool_start", "name": name, "args": args})

    def on_tool_end(self, name: str, result) -> None:
        self.emit(
            {
                "type": "tool_end",
                "name": name,
                "summary": result.summary,
                "is_error": result.is_error,
            }
        )
        self._emit_context()

    def on_notice(self, message: str) -> None:
        self.emit({"type": "notice", "message": message})
        self._emit_context()

    def on_error(self, message: str) -> None:
        self.emit({"type": "error", "message": message})

    def ask_approval(self, request: ApprovalRequest) -> str:
        """阻塞等待浏览器的确认结果。

        在 agent 线程里调用，靠 threading.Event 与事件循环那侧握手。
        """
        self._gate = threading.Event()
        self.emit(
            {
                "type": "approval_request",
                "tool": request.tool,
                "title": request.title,
                "detail": request.detail,
            }
        )
        if not self._gate.wait(timeout=APPROVAL_TIMEOUT):
            self.emit({"type": "notice", "message": "等待确认超时，已按拒绝处理"})
            return "n"
        return self._answer

    def resolve_approval(self, answer: str) -> None:
        """由 WebSocket 接收侧调用，唤醒正在等待的 agent 线程。"""
        self._answer = answer
        if self._gate is not None:
            self._gate.set()


def create_app(workspace: Path, mode: PermissionMode) -> FastAPI:
    app = FastAPI(title="mini coding agent")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        reporter = WebReporter(loop, queue)

        try:
            config = Config.from_env(workspace, permission_mode=mode)
        except SystemExit as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close()
            return

        agent = Agent(config, reporter)
        reporter.context_probe = lambda: (
            agent.history.estimated_tokens(),
            agent.history.compact_threshold,
        )

        await ws.send_json(
            {
                "type": "ready",
                "model": config.model,
                "workspace": str(config.workspace),
                "mode": config.permission_mode.value,
                "tools": [
                    {"name": t.name, "description": t.description}
                    for t in REGISTRY.values()
                ],
            }
        )

        async def pump() -> None:
            """把队列里的事件持续推给浏览器。None 是收工信号。"""
            while True:
                event = await queue.get()
                if event is None:
                    break
                await ws.send_json(event)

        pump_task = asyncio.create_task(pump())
        worker: threading.Thread | None = None

        def run_turn(text: str) -> None:
            """在独立线程里跑完整轮任务，结束后回报统计。"""
            try:
                stats = agent.run_turn(text)
                reporter.emit(
                    {
                        "type": "turn_end",
                        "steps": stats.steps,
                        "tool_calls": stats.tool_calls,
                        "tokens": stats.tokens,
                        "compaction_tokens": stats.compaction_tokens,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 线程里的异常必须自己兜住
                reporter.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                reporter.emit(
                    {
                        "type": "turn_end",
                        "steps": 0,
                        "tool_calls": 0,
                        "tokens": 0,
                        "compaction_tokens": 0,
                    }
                )

        try:
            while True:
                payload = await ws.receive_json()
                kind = payload.get("type")

                if kind == "user_input":
                    text = (payload.get("text") or "").strip()
                    if not text:
                        continue
                    if worker is not None and worker.is_alive():
                        await ws.send_json(
                            {"type": "notice", "message": "上一轮任务还在进行中"}
                        )
                        continue
                    worker = threading.Thread(target=run_turn, args=(text,), daemon=True)
                    worker.start()

                elif kind == "approval_response":
                    reporter.resolve_approval(payload.get("answer", "n"))

                elif kind == "set_mode":
                    try:
                        agent.permissions.mode = PermissionMode(payload.get("mode", ""))
                        await ws.send_json(
                            {
                                "type": "notice",
                                "message": f"已切换到 {agent.permissions.mode.value} 模式",
                            }
                        )
                    except ValueError:
                        await ws.send_json({"type": "error", "message": "未知的权限模式"})

                elif kind == "compact":
                    threading.Thread(target=agent.compact, daemon=True).start()

        except WebSocketDisconnect:
            pass
        finally:
            # 页面关掉时唤醒可能还卡在确认框上的 agent 线程，让它以拒绝收场
            reporter.resolve_approval("n")
            queue.put_nowait(None)
            await pump_task

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="web", description="mini coding agent 的 Web 界面")
    parser.add_argument("-C", "--workspace", default=".", help="agent 的工作目录")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=PermissionMode.ASK.value,
        help="权限模式",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = create_app(Path(args.workspace), PermissionMode(args.mode))
    print(f"界面地址： http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
