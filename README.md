# mini coding agent

一个从零手写的编程智能体：通过与大语言模型交互，自主读写文件、执行命令来完成编程任务。

未使用任何 agent 框架 / SDK。对话历史管理、工具定义与本地执行、模型输出解析、
循环终止条件、错误处理全部为自行实现，仅使用 `openai` 作为 HTTP 客户端库
访问 DeepSeek 的 OpenAI 兼容接口。

## 运行

```bash
pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

python -m agent                       # 交互模式
python -m agent -p "看看这个项目是做什么的"   # 单次任务
python -m agent -C ../some-project    # 指定工作目录
python -m agent --mode auto           # 写操作全自动，不逐次确认
python -m agent --mode readonly       # 只读模式，禁止一切写操作
python -m agent --resume              # 接着该工作目录上次的对话继续
```

交互模式下可用 `/help` 查看斜杠命令：`/compact` 手动压缩上下文、
`/mode` 切换权限模式、`/resume` 恢复上次对话、`/clear` 清空历史与存档。

### Web 界面（可选）

除终端外还提供一个浏览器界面，两者共用同一个 agent 内核：

```bash
python -m web                      # 打开 http://127.0.0.1:8000
python -m web -C ../some-project --mode auto
```

界面上能直观看到：左侧文件树（agent 新建/修改文件时自动刷新，点击可预览内容）、
工具调用卡片、写操作的 diff 确认框，以及一条**上下文占用进度条**——压缩触发时
它会肉眼可见地回落。左上角「切换」按钮可以直接在界面里选择 agent 的工作目录。

配色以南大紫为基调，右下角是北大楼水印。主色由 `index.html` 顶部的
`--nju-purple` 一个变量决定，其余色阶都围绕它调出，换色只改这一行。

`.env` 已被 `.gitignore` 忽略，key 不会进入仓库。

## 结构

```
agent/
├── cli.py              终端 REPL 与渲染（rich），实现 Reporter 接口
├── loop.py             agent 主循环：请求 → 工具 → 回灌 → 再请求；三层终止条件
├── llm.py              DeepSeek 通信层：流式分片拼装、重试与错误归一化
├── history.py          对话历史：消息维护、工具结果截断、上下文压缩（安全切割点）
├── prompts.py          系统提示词与摘要提示词
├── session.py          会话存档：按工作目录分别持久化，支持 --resume
├── config.py           配置与 .env 加载
└── tools/
    ├── base.py         工具注册表、JSON Schema 导出、dispatch 兜错
    ├── paths.py        路径沙箱
    ├── permissions.py  三档权限模式（ask/auto/readonly）与用户确认闸门
    ├── diffutil.py     统一 diff 生成，供确认预览和工具返回值复用
    ├── filesystem.py   read_file / list_dir
    ├── editing.py      write_file / edit_file（写前展示 diff 并请求确认）
    ├── search.py       grep（纯 Python 实现，跨平台）
    └── shell.py        run_command（高危命令硬拦截 + 用户确认）

web/                    可选的浏览器界面（不装 fastapi 也不影响终端使用）
├── server.py           FastAPI + WebSocket，内含 WebReporter
└── static/index.html   单文件前端，原生 JS，无构建步骤

demo/
└── setup_demo.py       生成/重置演示靶子（一个带真实 bug 的小项目）

tests/                  37 个测试，均用桩替换 LLM，不联网不花钱
├── run_all.py          一次跑完全部
├── test_tools.py       沙箱 / 凭据防护 / 权限闸门 / dispatch 兜错
├── test_history.py     上下文压缩的切割点
├── test_loop.py        三层终止条件与历史一致性
└── test_session.py     会话存档往返与 todo_write 校验
```

设计决策的取舍记录见 [DESIGN.md](DESIGN.md)。

## 测试

```bash
python tests/run_all.py
```

37 个测试，全部不联网、不消耗 token（LLM 用桩替换）：

- `test_tools.py` —— 路径沙箱、凭据文件防护、三档权限闸门、dispatch 兜错。
  这些是"说了算数"的约束，一旦悄悄失效，后果是密钥泄露或写坏工作目录外的
  文件，而且不会有任何报错提醒你。
- `test_history.py` —— 上下文压缩的切割点。最容易悄悄切坏的部分：一旦切断
  tool_call 与 tool 结果的配对，请求会被服务端 400 拒绝，且报错不指向压缩逻辑。
- `test_loop.py` —— 三层终止条件，以及每个出口退出时历史都必须保持完整
  （不能有孤儿 tool 消息，也不能有拿不到结果的 tool_call）。
- `test_session.py` —— 存档往返、system prompt 绝不被恢复、损坏存档不崩、
  不同工作目录互相隔离，以及 todo_write 的状态机校验。

另有 `tests/eval_compaction.py` —— 压缩保真度的探针测试。它会真实调用
API，所以不在 `run_all.py` 里，需要单独运行：

```bash
python tests/eval_compaction.py
```

埋入若干可检验的事实后强制触发压缩，再禁用全部工具提问，测量事实召回率。
实测两次均为 6/6。低于 80% 会返回非零退出码。

## 进度

- [x] 核心循环、流式解析、工具注册表、路径沙箱
- [x] read_file / list_dir / write_file / edit_file / grep / run_command
- [x] 危险操作的用户确认与权限模式（ask / auto / readonly）
- [x] 上下文压缩：超阈值时把早期对话摘要成一条，切割点保证不破坏 tool_call 配对
- [x] Web 界面：复用 Reporter 接口，agent 内核零改动
- [x] 会话持久化：`--resume` 接着上次的对话继续
- [x] `todo_write` 规划工具：多步任务自动拆解并显示进度
- [x] 文件树 + 工作目录选择器 + 文件预览
- [x] 凭据类文件（.env / *.key / id_rsa 等）一律拒绝读写，grep 同样挡住
