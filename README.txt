mini coding agent —— 一个从零手写的编程智能体

【Git 仓库】
https://github.com/waker-ai/mini_coding_agent

【如何运行】
Python 3.10+。pip install -r requirements.txt，把 .env.example 复制为 .env
并填入 DEEPSEEK_API_KEY。终端界面 python -m agent，浏览器界面 python -m web
（127.0.0.1:8000）。参数：-C 指定工作目录，--mode 选 ask/auto/readonly，
--resume 接着上次对话。测试 python tests/run_all.py，56 个用例全用桩替换。

【特色功能】

一、上下文压缩的安全切割点
难点不在摘要，而在从哪里切：协议要求每条 tool 消息都能在前面找到带对应
tool_call_id 的 assistant 消息，乱切会被服务端以 400 拒绝，且报错完全不指向
压缩逻辑。切割点优先落在 user 消息处，若尾部全是 tool 消息，宁可放弃压缩也
不切坏。token 计量直接采用 API 返回的 prompt_tokens，不引入分词器依赖。

二、三层安全边界
路径沙箱把文件操作限定在工作目录内；.env、*.key 一类凭据文件即使位于目录内
也一律拒绝读写（grep 直接遍历文件、绕过路径解析，所以单独再挡一次）；写文件
与执行命令逐次展示 diff 或命令原文请求确认，而 rm -rf、git reset --hard 等
高危命令即使在全自动模式下也被硬拦截——auto 不等于无底线。

三、错误一律回灌给模型，不向上抛出
工具不存在、参数不是合法 JSON、执行时报错，全部翻译成中文作为 tool 消息交还
模型。鲁棒性来自"模型能看见错误并自我纠正"，而不是"程序不出错"。

四、双界面共用同一内核
UI 全部关在 Reporter 接口之后，因此新增浏览器界面时 agent 包一行未改。Web 端
有文件树、工具调用卡片、diff 确认框和上下文占用进度条——压缩触发时它会肉眼
可见地回落。

五、web_fetch 联网抓取
不执行 JavaScript，但保留页面内联的 JSON——洛谷题目的输入输出样例与时空限制
只存在于其中。截断保留头尾，因为有站点把正文放在页面最末尾。地址校验会真的做
一次 DNS 解析再判断：localtest.me 是公网域名却解析到 127.0.0.1，字面量匹配
挡不住，任意一条 A 记录落在内网即拒绝。

【其它】
未使用任何 agent 框架或 SDK，openai 库仅作为访问 DeepSeek 兼容接口的 HTTP
客户端。对话历史与上下文管理、工具定义与本地执行、流式输出解析、循环终止条件、
错误处理均为自行实现。每一处设计决策的理由与被放弃的方案记在 DESIGN.md，共
29 条。demo/setup_demo.py 可一键生成演示视频里的靶子项目，便于复现。
