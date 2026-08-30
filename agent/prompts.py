"""系统提示词。

提示词只负责"引导"，凡是不能被违反的约束（路径沙箱、步数上限）都写在代码里。
"""
from __future__ import annotations

import platform
from pathlib import Path

SYSTEM_TEMPLATE = """你是一个运行在用户终端里的编程助手，可以通过工具直接读写用户的项目文件。

# 工作方式
- 先用工具把事实搞清楚，再动手。不要凭猜测描述代码内容。
- 修改文件之前必须先 read_file 读过它。
- 一次只做一件事，需要多个步骤时按顺序逐步调用工具。
- 工具返回错误时，先读懂错误原因再重试，不要用同样的参数反复调用。
- 任务完成后，用一两句话说明你做了什么，不要复述全部代码。
- 用中文回答。

# 环境
操作系统：{os_name}
工作目录：{workspace}
目录概览：
{tree}
"""


def build_system_prompt(workspace: Path, tree: str) -> str:
    return SYSTEM_TEMPLATE.format(
        os_name=f"{platform.system()} {platform.release()}",
        workspace=workspace,
        tree=tree,
    )


# 压缩早期对话时用的提示词。要点是"面向未来"而不是"面向叙事"：
# 摘要的唯一读者是即将接着干活的模型自己，所以要留下继续工作所需的事实
# （改过哪些文件、当前状态、还没做完什么），而不是复述对话过程。
SUMMARY_PROMPT = """请把下面这段编程助手与用户的对话压缩成一份简明的工作记录。
这份记录会替换掉原始对话，作为你后续继续工作的唯一依据，因此必须保留所有
继续任务所需的事实，不要写成流水账。

请按以下结构输出，没有内容的小节直接省略：

## 用户的目标
（用户最初要求做什么，以及中途调整过的要求）

## 已完成的工作
（创建/修改了哪些文件、做了什么改动、执行过哪些命令及其结果）

## 关键事实
（代码结构、函数签名、配置项、报错信息等后续会用到的具体细节，
保留确切的文件路径和标识符名称，不要模糊化）

## 未完成 / 待办
（还剩什么没做，以及已知但尚未解决的问题）

只输出这份记录本身，不要加任何开场白或结束语。"""


def build_summary_request(messages: list[dict]) -> str:
    """把待压缩的消息渲染成纯文本，交给模型做摘要。

    这里把工具调用和结果都平铺成文本、而不是原样把 messages 传回去，是因为
    摘要请求不需要（也不应该）带 tools 参数：一旦带上，模型很可能"接着干活"
    直接发起新的工具调用，而不是老老实实写摘要。
    """
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "")
        content = (message.get("content") or "").strip()

        if role == "user":
            lines.append(f"[用户] {content}")
        elif role == "assistant":
            if content:
                lines.append(f"[助手] {content}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                lines.append(
                    f"[助手调用工具] {function.get('name')}({function.get('arguments')})"
                )
        elif role == "tool":
            lines.append(f"[工具返回] {content}")

    return f"{SUMMARY_PROMPT}\n\n---- 待压缩的对话开始 ----\n" + "\n".join(lines)
