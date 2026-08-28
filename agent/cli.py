"""终端交互层：REPL + 渲染。

所有 rich 相关的东西都关在这一层，loop.py 只认 Reporter 接口。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from .config import Config
from .loop import Agent, Reporter, TurnStats
from .tools import REGISTRY, ApprovalRequest, PermissionMode, ToolResult, format_call

BANNER = "mini coding agent · /help 查看命令 · /exit 退出"


class ConsoleReporter(Reporter):
    def __init__(self, console: Console) -> None:
        self.console = console
        self._streaming = False

    def on_text(self, delta: str) -> None:
        if not self._streaming:
            self.console.print()
            self._streaming = True
        self.console.print(delta, end="", highlight=False, markup=False)

    def on_text_end(self) -> None:
        if self._streaming:
            self.console.print("\n")
            self._streaming = False

    def on_tool_start(self, name: str, arguments: str) -> None:
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            args = {"<raw>": arguments}
        rendered = format_call(name, args) if isinstance(args, dict) else f"{name}({arguments})"
        self.console.print(f"[bold cyan]⏺[/] [cyan]{rendered}[/]")

    def on_tool_end(self, name: str, result: ToolResult) -> None:
        color = "red" if result.is_error else "dim"
        self.console.print(f"  [{color}]└ {result.summary}[/]")

    def on_notice(self, message: str) -> None:
        self.console.print(f"[yellow]! {message}[/]")

    def on_error(self, message: str) -> None:
        self.console.print(f"[bold red]✗ {message}[/]")

    def ask_approval(self, request: ApprovalRequest) -> str:
        """写操作 / 命令执行前的人在回路确认。运行在主线程，会阻塞直到用户输入。"""
        self.console.print()
        if request.tool == "run_command":
            body = Syntax(request.detail, "bash", word_wrap=True)
        else:
            body = Syntax(request.detail, "diff", word_wrap=True)
        self.console.print(Panel(body, title=request.title, border_style="yellow"))

        try:
            answer = self.console.input(
                "[yellow]允许执行？[/] [bold](y)[/]是 / (n)否 / (a)本次会话总是允许该工具  "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.console.print("\n[yellow]! 未输入，默认拒绝[/]")
            return "n"
        self.console.print()  # 补一个换行，避免后续的工具结果摘要跟提示行粘在一起
        return answer


def _print_help(console: Console) -> None:
    console.print(
        Markdown(
            "**可用命令**\n\n"
            "- `/help` 显示本帮助\n"
            "- `/tools` 列出已注册的工具\n"
            "- `/clear` 清空对话历史（保留系统提示）\n"
            "- `/mode` 查看或切换权限模式（ask / auto / readonly）\n"
            "- `/exit` 退出\n"
        )
    )


def _print_tools(console: Console) -> None:
    for tool_def in REGISTRY.values():
        console.print(f"[cyan]{tool_def.name}[/] — {tool_def.description}")


def _print_stats(console: Console, stats: TurnStats) -> None:
    console.print(
        f"[dim]{stats.steps} 步 · {stats.tool_calls} 次工具调用 · "
        f"{stats.tokens} tokens[/]"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="一个极简编程智能体")
    parser.add_argument("-C", "--workspace", default=".", help="agent 的工作目录，默认当前目录")
    parser.add_argument("-p", "--prompt", help="单次任务模式：执行完这条指令就退出")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=PermissionMode.ASK.value,
        help="权限模式：ask（默认，写操作逐次确认）/ auto（全自动）/ readonly（禁止写操作）",
    )
    args = parser.parse_args(argv)

    console = Console()
    config = Config.from_env(Path(args.workspace), permission_mode=PermissionMode(args.mode))
    agent = Agent(config, ConsoleReporter(console))

    console.print(f"[bold]{BANNER}[/]")
    if config.workspace_created:
        console.print(f"[yellow]! 工作目录不存在，已自动创建：{config.workspace}[/]")
    console.print(
        f"[dim]模型 {config.model} · 工作目录 {config.workspace} · "
        f"权限模式 {config.permission_mode.value}[/]\n"
    )

    if args.prompt:
        _print_stats(console, agent.run_turn(args.prompt))
        return 0

    while True:
        try:
            user_input = console.input("[bold green]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见。")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            console.print("再见。")
            return 0
        if user_input == "/help":
            _print_help(console)
            continue
        if user_input == "/tools":
            _print_tools(console)
            continue
        if user_input == "/clear":
            agent.history.clear()
            console.print("[dim]历史已清空[/]")
            continue
        if user_input.startswith("/mode"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                console.print(f"[dim]当前权限模式：{agent.permissions.mode.value}[/]")
            else:
                choice = parts[1].strip().lower()
                try:
                    agent.permissions.mode = PermissionMode(choice)
                    console.print(f"[dim]已切换到 {choice} 模式[/]")
                except ValueError:
                    console.print(f"[red]未知模式：{choice}（可选 ask / auto / readonly）[/]")
            continue

        try:
            _print_stats(console, agent.run_turn(user_input))
        except KeyboardInterrupt:
            console.print("\n[yellow]! 已中断，可以继续输入[/]")
