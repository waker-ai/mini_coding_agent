"""终端交互层：REPL + 渲染。

所有 rich 相关的东西都关在这一层，loop.py 只认 Reporter 接口。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

from .config import Config
from .loop import Agent, Reporter, TurnStats
from .tools import REGISTRY, ToolResult, format_call

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
        import json

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


def _print_help(console: Console) -> None:
    console.print(
        Markdown(
            "**可用命令**\n\n"
            "- `/help` 显示本帮助\n"
            "- `/tools` 列出已注册的工具\n"
            "- `/clear` 清空对话历史（保留系统提示）\n"
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
    args = parser.parse_args(argv)

    console = Console()
    config = Config.from_env(Path(args.workspace))
    agent = Agent(config, ConsoleReporter(console))

    console.print(f"[bold]{BANNER}[/]")
    console.print(f"[dim]模型 {config.model} · 工作目录 {config.workspace}[/]\n")

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

        try:
            _print_stats(console, agent.run_turn(user_input))
        except KeyboardInterrupt:
            console.print("\n[yellow]! 已中断，可以继续输入[/]")
