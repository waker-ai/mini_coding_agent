"""Web 前端的后端服务。

agent 必须跑在 Python 进程里——它要读写本地文件、执行 PowerShell 命令，这些
浏览器碰不了。所以浏览器只是显示层，这个模块做的事只有三件：把 agent 的事件流
推给浏览器、把浏览器的输入和确认回传给 agent、给界面提供文件树与目录选择的接口。

核心是 WebReporter：它实现 loop.py 里的 Reporter 接口。当初把 UI 全部关在这个
接口后面的回报就在这里——agent 包一行代码没改，只是多了一个 Reporter 实现。

线程模型上有两个必须处理的点：

1. Agent.run_turn() 是同步阻塞的，绝不能直接在事件循环里跑。否则 WebSocket
   在整轮任务期间收不到任何消息——包括用户点“允许”的那条，确认框会永久卡死。
   所以 agent 跑在独立线程里。

2. 事件在 agent 线程里产生，但 WebSocket 属于事件循环。跨线程投递必须走
   loop.call_soon_threadsafe()，不能在工作线程里直接 await send。

关于两类文件接口的边界（重要，见 DESIGN.md 第 22 条）：
  /api/tree、/api/file 走 agent 的路径沙箱，只能看当前工作目录内的东西；
  /api/browse 刻意不受沙箱限制——要选新的工作目录就必须能看到它外面。
  它只返回目录名，不返回任何文件内容，且服务只监听 127.0.0.1。
"""
from __future__ import annotations

import asyncio
import json
import string
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent import session as session_store
from agent.config import Config
from agent.loop import Agent, Reporter
from agent.tools import REGISTRY, ApprovalRequest, PermissionMode
from agent.tools.base import ToolContext, ToolError
from agent.tools.filesystem import IGNORED
from agent.tools.paths import is_sensitive, resolve

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 用户迟迟不点确认时的等待上限。必须有：否则用户直接关掉页面，
# agent 线程会永久挂在 Event.wait() 上。超时按拒绝处理，
# 与权限系统 fail-closed 的原则保持一致。
APPROVAL_TIMEOUT = 300.0

# 文件树一次最多返回的条目数，防止指到一个巨大目录时把浏览器打死
MAX_TREE_ENTRIES = 800
# 预览文件的大小上限
MAX_PREVIEW_BYTES = 200_000


class AppState:
    """当前工作目录与权限模式。

    这是个单用户的本机工具，所以直接用应用级单例，不做多会话隔离——
    HTTP 接口（文件树）和 WebSocket（对话）需要看到同一个工作目录，
    做成每连接一份反而要在两者之间同步，得不偿失。
    """

    def __init__(self, workspace: Path, mode: PermissionMode) -> None:
        self.workspace = workspace
        self.mode = mode


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
        # 写入类工具执行完就刷新文件树，让新建/修改的文件立刻在界面上出现
        if name in {"write_file", "edit_file", "run_command"}:
            self.emit({"type": "tree_dirty"})

    def on_notice(self, message: str) -> None:
        self.emit({"type": "notice", "message": message})
        self._emit_context()

    def on_error(self, message: str) -> None:
        self.emit({"type": "error", "message": message})

    def on_todos(self, todos: list[dict[str, str]]) -> None:
        self.emit({"type": "todos", "todos": todos})

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


def build_tree(root: Path, depth: int = 3) -> dict[str, Any]:
    """把工作目录读成一棵 JSON 树，供左侧栏渲染。"""
    counter = {"n": 0}

    def walk(directory: Path, level: int) -> list[dict[str, Any]]:
        if level >= depth or counter["n"] >= MAX_TREE_ENTRIES:
            return []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            return []

        nodes: list[dict[str, Any]] = []
        for entry in entries:
            if entry.name in IGNORED or entry.name.startswith("."):
                continue
            if is_sensitive(entry.name):
                continue
            if counter["n"] >= MAX_TREE_ENTRIES:
                break
            counter["n"] += 1
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                nodes.append(
                    {"name": entry.name, "path": rel, "type": "dir", "children": walk(entry, level + 1)}
                )
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                nodes.append({"name": entry.name, "path": rel, "type": "file", "size": size})
        return nodes

    return {"root": str(root), "children": walk(root, 0)}


def list_directories(raw_path: str) -> dict[str, Any]:
    """给「选择工作目录」用的目录浏览。

    刻意不受 agent 路径沙箱约束——要选一个新的工作目录，就必须能看到当前
    工作目录外面。作为补偿：只返回目录名，绝不返回文件内容，而且服务只
    监听 127.0.0.1。沙箱本身没有被削弱：agent 依然只能碰选定的工作目录。
    """
    if not raw_path:
        # Windows 上从盘符列表开始，其它平台从根目录开始
        drives = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
        if drives:
            return {
                "path": "",
                "parent": None,
                "dirs": [{"name": d, "path": d} for d in drives],
            }
        raw_path = "/"

    current = Path(raw_path).expanduser()
    try:
        current = current.resolve()
    except OSError:
        raise ToolError(f"无法解析路径：{raw_path}") from None
    if not current.is_dir():
        raise ToolError(f"不是有效目录：{current}")

    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        raise ToolError(f"没有权限访问：{current}") from None

    parent = str(current.parent) if current.parent != current else ""
    return {"path": str(current), "parent": parent, "dirs": dirs}


def create_app(workspace: Path, mode: PermissionMode) -> FastAPI:
    app = FastAPI(title="mini coding agent")
    state = AppState(workspace.resolve(), mode)

    # 静态资源（水印图等）。注意这里托管的是本项目自带的 web/static，
    # 与 agent 的工作目录无关，不受也不影响路径沙箱。
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/tree")
    async def api_tree() -> dict[str, Any]:
        if not state.workspace.is_dir():
            return {"root": str(state.workspace), "children": [], "error": "工作目录不存在"}
        return build_tree(state.workspace)

    @app.get("/api/file")
    async def api_file(path: str) -> dict[str, Any]:
        """预览文件内容。走 agent 的同一套路径沙箱，看不到工作目录之外。"""
        ctx = ToolContext(workspace=state.workspace)
        try:
            target = resolve(ctx, path)
        except ToolError as exc:
            return {"error": str(exc)}
        if not target.is_file():
            return {"error": "不是文件"}
        try:
            data = target.read_bytes()[:MAX_PREVIEW_BYTES]
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"error": "不是 UTF-8 文本文件，无法预览"}
        except OSError as exc:
            return {"error": f"读取失败：{exc}"}
        truncated = target.stat().st_size > MAX_PREVIEW_BYTES
        return {"path": path, "content": text, "truncated": truncated}

    @app.get("/api/browse")
    async def api_browse(path: str = "") -> dict[str, Any]:
        try:
            return list_directories(path)
        except ToolError as exc:
            return {"error": str(exc)}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        reporter = WebReporter(loop, queue)
        holder: dict[str, Any] = {"agent": None, "config": None}

        def build_agent(resume: bool = False) -> str:
            """按当前工作目录重建 Agent。返回空串表示成功，否则是错误信息。"""
            try:
                config = Config.from_env(state.workspace, permission_mode=state.mode)
            except SystemExit as exc:
                return str(exc)
            agent = Agent(config, reporter, resume=resume)
            reporter.context_probe = lambda: (
                agent.history.estimated_tokens(),
                agent.history.compact_threshold,
            )
            holder["agent"] = agent
            holder["config"] = config
            return ""

        error = build_agent()
        if error:
            await ws.send_json({"type": "error", "message": error})
            await ws.close()
            return

        async def send_ready() -> None:
            config = holder["config"]
            await ws.send_json(
                {
                    "type": "ready",
                    "model": config.model,
                    "workspace": str(config.workspace),
                    "mode": config.permission_mode.value,
                    "has_session": session_store.load(state.workspace) is not None,
                    "tools": [
                        {"name": t.name, "description": t.description}
                        for t in REGISTRY.values()
                    ],
                }
            )

        await send_ready()

        async def pump() -> None:
            """把队列里的事件持续推给浏览器。None 是收工信号。"""
            while True:
                event = await queue.get()
                if event is None:
                    break
                await ws.send_json(event)

        pump_task = asyncio.create_task(pump())
        worker: threading.Thread | None = None
        compactor: threading.Thread | None = None

        def busy() -> bool:
            return worker is not None and worker.is_alive()

        def compacting() -> bool:
            return compactor is not None and compactor.is_alive()

        def occupied() -> str:
            """后台有没有正在跑的活。空串表示空闲，否则是可以直接展示的拒绝理由。

            跑任务和压缩都在改写同一份 history，必须互斥：并发跑两个的话
            消息列表会被同时增删，坏掉的是历史本身，事后无从恢复。
            """
            if busy():
                return "上一轮任务还在进行中"
            if compacting():
                return "上下文正在压缩中"
            return ""

        def run_turn(text: str) -> None:
            """在独立线程里跑完整轮任务，结束后回报统计。"""
            try:
                stats = holder["agent"].run_turn(text)
                reporter.emit(
                    {
                        "type": "turn_end",
                        "steps": stats.steps,
                        "tool_calls": stats.tool_calls,
                        "tokens": stats.tokens,
                        "compaction_tokens": stats.compaction_tokens,
                    }
                )
                reporter.emit({"type": "tree_dirty"})
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

        def run_compact() -> None:
            """在独立线程里做手动压缩。

            压缩要额外调一次模型写摘要，慢的时候十几秒，期间 agent 不产生任何
            事件——界面上就是点完按钮什么都不动。所以这里在两端各补一个事件，
            让前端有东西可显示、也有明确的时机把按钮解禁。
            """
            try:
                holder["agent"].compact()
            except Exception as exc:  # noqa: BLE001 - 线程里的异常必须自己兜住
                reporter.emit(
                    {"type": "error", "message": f"压缩失败：{type(exc).__name__}: {exc}"}
                )
            finally:
                # 无论成功、失败还是没压成，这条都必须发出去，
                # 否则前端的按钮会永远停在"压缩中…"
                reporter.emit({"type": "compact_end"})

        try:
            while True:
                payload = await ws.receive_json()
                kind = payload.get("type")

                if kind == "user_input":
                    text = (payload.get("text") or "").strip()
                    if not text:
                        continue
                    reason = occupied()
                    if reason:
                        await ws.send_json(
                            {"type": "notice", "message": f"{reason}，请等它结束"}
                        )
                        continue
                    worker = threading.Thread(target=run_turn, args=(text,), daemon=True)
                    worker.start()

                elif kind == "approval_response":
                    reporter.resolve_approval(payload.get("answer", "n"))

                elif kind == "set_mode":
                    try:
                        state.mode = PermissionMode(payload.get("mode", ""))
                        holder["agent"].permissions.mode = state.mode
                        await ws.send_json(
                            {"type": "notice", "message": f"已切换到 {state.mode.value} 模式"}
                        )
                    except ValueError:
                        await ws.send_json({"type": "error", "message": "未知的权限模式"})

                elif kind == "set_workspace":
                    # 切换工作目录要重建 Agent：系统提示词里带着目录概览，
                    # 历史也是围绕旧目录展开的，继续沿用只会误导模型。
                    reason = occupied()
                    if reason:
                        await ws.send_json(
                            {"type": "notice", "message": f"{reason}，无法切换工作目录"}
                        )
                        continue
                    raw = (payload.get("path") or "").strip()
                    candidate = Path(raw).expanduser()
                    if not candidate.is_dir():
                        await ws.send_json({"type": "error", "message": f"不是有效目录：{raw}"})
                        continue
                    previous = state.workspace
                    state.workspace = candidate.resolve()
                    error = build_agent()
                    if error:
                        state.workspace = previous
                        build_agent()
                        await ws.send_json({"type": "error", "message": error})
                        continue
                    await ws.send_json(
                        {"type": "workspace_changed", "workspace": str(state.workspace)}
                    )
                    await send_ready()

                elif kind == "resume":
                    reason = occupied()
                    if reason:
                        await ws.send_json(
                            {"type": "notice", "message": f"{reason}，无法恢复上次会话"}
                        )
                        continue
                    holder["agent"].restore_session()

                elif kind == "compact":
                    # 已经在压缩了就只提示，不再起第二个线程——两个线程同时改
                    # history 会把消息列表搅烂。此时也不发 compact_end：
                    # 正在跑的那次结束时自会发，提前发出去按钮就被误解禁了。
                    if compacting():
                        await ws.send_json(
                            {"type": "notice", "message": "上下文正在压缩中，请等它结束"}
                        )
                    elif busy():
                        await ws.send_json(
                            {"type": "notice", "message": "上一轮任务还在进行中，暂时无法压缩"}
                        )
                        await ws.send_json({"type": "compact_end"})
                    else:
                        await ws.send_json({"type": "compact_start"})
                        compactor = threading.Thread(target=run_compact, daemon=True)
                        compactor.start()

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
    parser.add_argument("-C", "--workspace", default=".", help="agent 的初始工作目录（界面上可再改）")
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
