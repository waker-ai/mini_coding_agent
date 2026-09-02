mini coding agent —— 一个从零手写的编程智能体

【Git 仓库】
https://github.com/waker-ai/mini_coding_agent

【如何运行】
环境 Python 3.10 以上。
1. pip install -r requirements.txt
2. 把 .env.example 复制为 .env，填入 DEEPSEEK_API_KEY（也可直接用环境变量提供）
3. 终端界面：python -m agent
   浏览器界面：python -m web ，然后打开 127.0.0.1:8000

常用参数：-C 指定工作目录，--mode 选权限模式（ask 逐次确认 / auto 全自动 /
readonly 只读），--resume 接着上次的对话继续。
运行测试：python tests/run_all.py —— 56 个用例，模型全部用桩替换，
不联网也不消耗额度。

【特色功能】

一、上下文压缩的安全切割点
历史逼近阈值时，把早期对话交给模型摘要成一条替换原文。难点不在摘要，而在
从哪里切：协议要求每条 tool 消息都能在前面找到带对应 tool_call_id 的
assistant 消息，从中间乱切会被服务端直接以 400 拒绝，而且报错完全不指向
压缩逻辑，极难排查。切割点优先落在 user 消息处，若整段尾巴都是 tool 消息，
宁可放弃这次压缩也不切坏。token 计量直接采用 API 返回的 prompt_tokens
作为权威值，不引入分词器依赖。

二、三层安全边界
路径沙箱把一切文件操作限定在工作目录内；.env、*.key 一类凭据文件即使位于
工作目录内也一律拒绝读写（grep 直接遍历文件、绕过了路径解析，所以单独再挡
一次）；写文件与执行命令会逐次向用户展示 diff 或命令原文并请求确认，而
rm -rf、git reset --hard 等高危命令即使在全自动模式下也被硬拦截——auto
不等于无底线。

三、错误一律回灌给模型，不向上抛出
工具不存在、参数不是合法 JSON、执行时报错，全部翻译成中文文本作为 tool
消息交还模型。agent 的鲁棒性来自"模型能看见错误并自我纠正"，而不是
"程序不出错"。

四、双界面共用同一内核
UI 全部关在 Reporter 接口之后，因此新增浏览器界面时 agent 包一行未改。
Web 端可以看到文件树、工具调用卡片、diff 确认框，以及一条上下文占用进度条
——压缩触发时它会肉眼可见地回落。

【其它】
未使用任何 agent 框架或 SDK，openai 库仅作为访问 DeepSeek 兼容接口的 HTTP
客户端。对话历史与上下文管理、工具定义与本地执行、流式输出解析、循环终止
条件、错误处理，均为自行实现。

每一处设计决策的理由与被放弃的方案都记在仓库的 DESIGN.md 里，共 26 条，
其中包含开发过程中踩到的真实问题，例如 Windows 上 shell=True 实际调用的是
cmd.exe 而非 PowerShell、以及路径沙箱挡不住工作目录内的 .env 泄露。

demo/setup_demo.py 可一键生成演示视频中使用的靶子项目，便于复现。
