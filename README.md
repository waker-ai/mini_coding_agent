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
```

`.env` 已被 `.gitignore` 忽略，key 不会进入仓库。

## 结构

```
agent/
├── cli.py          终端 REPL 与渲染（rich），实现 Reporter 接口
├── loop.py         agent 主循环：请求 → 工具 → 回灌 → 再请求；三层终止条件
├── llm.py          DeepSeek 通信层：流式分片拼装、重试与错误归一化
├── history.py      对话历史：消息维护、超长工具结果截断
├── prompts.py      系统提示词构建
├── config.py       配置与 .env 加载
└── tools/
    ├── base.py         工具注册表、JSON Schema 导出、dispatch 兜错
    ├── paths.py        路径沙箱
    ├── permissions.py  三档权限模式（ask/auto/readonly）与用户确认闸门
    ├── diffutil.py     统一 diff 生成，供确认预览和工具返回值复用
    ├── filesystem.py   read_file / list_dir
    ├── editing.py      write_file / edit_file（写前展示 diff 并请求确认）
    ├── search.py       grep（纯 Python 实现，跨平台）
    └── shell.py        run_command（高危命令硬拦截 + 用户确认）
```

设计决策的取舍记录见 [DESIGN.md](DESIGN.md)。

## 进度

- [x] 核心循环、流式解析、工具注册表、路径沙箱
- [x] read_file / list_dir / write_file / edit_file / grep / run_command
- [x] 危险操作的用户确认与权限模式（ask / auto / readonly）
- [ ] 上下文压缩（超阈值时摘要化早期消息）
