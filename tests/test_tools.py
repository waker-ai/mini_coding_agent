"""工具层的边界测试：路径沙箱、凭据防护、权限闸门、dispatch 兜错。

这些是"说了算数"的约束，不能只靠手动验证——它们一旦悄悄失效，
后果是密钥泄露或者写坏工作目录外的文件，而且不会有任何报错提醒你。

直接用 python tests/test_tools.py 运行，不引入 pytest 依赖。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import Permissions, PermissionMode, ToolContext, dispatch
from agent.tools.paths import is_sensitive


def make_ctx(mode: PermissionMode = PermissionMode.AUTO, asker=None):
    workspace = Path(tempfile.mkdtemp())
    (workspace / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    (workspace / ".env").write_text("DEEPSEEK_API_KEY=sk-REAL-SECRET-VALUE\n", encoding="utf-8")
    (workspace / ".env.example").write_text("DEEPSEEK_API_KEY=sk-xxx\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "note.md").write_text("todo: 修 bug\n", encoding="utf-8")
    perms = Permissions(mode=mode, asker=asker)
    return ToolContext(workspace=workspace, permissions=perms), workspace


def call(ctx, name, **args):
    return dispatch(name, json.dumps(args), ctx)


# ---------- 路径沙箱 ----------

def test_sandbox_blocks_traversal():
    ctx, _ = make_ctx()
    for bad in ["../outside.txt", "../../etc/passwd", "sub/../../escape.txt"]:
        result = call(ctx, "read_file", path=bad)
        assert result.is_error, f"越界路径未被拦截：{bad}"
        assert "工作目录之外" in result.content, f"错误信息不对：{result.content[:60]}"


def test_sandbox_blocks_absolute_outside():
    ctx, _ = make_ctx()
    result = call(ctx, "read_file", path="C:/Windows/win.ini")
    assert result.is_error, "工作目录外的绝对路径未被拦截"


def test_sandbox_allows_inside():
    ctx, _ = make_ctx()
    result = call(ctx, "read_file", path="sub/note.md")
    assert not result.is_error, f"工作目录内的文件被误拦：{result.content[:80]}"
    assert "修 bug" in result.content


# ---------- 凭据防护 ----------

def test_credentials_blocked_for_all_file_tools():
    ctx, _ = make_ctx()
    cases = [
        ("read_file", {"path": ".env"}),
        ("write_file", {"path": ".env", "content": "x"}),
        ("edit_file", {"path": ".env", "old_string": "a", "new_string": "b"}),
    ]
    for name, args in cases:
        result = call(ctx, name, **args)
        assert result.is_error, f"{name} 竟然能碰 .env"
        assert "凭据" in result.content


def test_grep_never_leaks_credentials():
    """grep 直接遍历读文件、绕过 resolve()，必须单独挡住。"""
    ctx, _ = make_ctx()
    for pattern in ["sk-", "DEEPSEEK_API_KEY", "REAL-SECRET"]:
        result = call(ctx, "grep", pattern=pattern)
        assert "sk-REAL-SECRET-VALUE" not in result.content, (
            f"grep /{pattern}/ 把真实密钥捞出来了"
        )


def test_env_example_is_allowed():
    """模板文件不含真实凭据，本来就要入库，不该被拦。"""
    ctx, _ = make_ctx()
    result = call(ctx, "read_file", path=".env.example")
    assert not result.is_error, f".env.example 被误拦：{result.content[:80]}"


def test_is_sensitive_patterns():
    for name in [".env", ".env.local", "server.key", "cert.pem", "id_rsa", ".git-credentials"]:
        assert is_sensitive(name), f"{name} 应当被判定为凭据文件"
    for name in [".env.example", "main.py", "README.md", "keyboard.py"]:
        assert not is_sensitive(name), f"{name} 不该被判定为凭据文件"


# ---------- 权限闸门 ----------

def test_readonly_mode_blocks_writes():
    ctx, workspace = make_ctx(mode=PermissionMode.READONLY)
    result = call(ctx, "write_file", path="new.txt", content="x")
    assert result.is_error and "只读" in result.content
    assert not (workspace / "new.txt").exists(), "只读模式下文件竟然被写出来了"


def test_denied_approval_does_not_write():
    ctx, workspace = make_ctx(mode=PermissionMode.ASK, asker=lambda req: "n")
    result = call(ctx, "write_file", path="denied.txt", content="x")
    assert result.is_error
    assert not (workspace / "denied.txt").exists(), "用户拒绝后文件仍被写出"
    assert "不要重复同样的调用" in result.content, "应当劝阻模型原样重试"


def test_always_allow_is_remembered():
    ctx, workspace = make_ctx(mode=PermissionMode.ASK, asker=lambda req: "a")
    assert not call(ctx, "write_file", path="a1.txt", content="x").is_error

    def boom(req):
        raise AssertionError("已选择总是允许，不该再次询问")

    ctx.permissions.asker = boom
    assert not call(ctx, "write_file", path="a2.txt", content="y").is_error
    assert (workspace / "a2.txt").exists()


def test_fail_closed_without_asker():
    """没有交互通道时必须保守拒绝，不能默默放行。"""
    ctx, workspace = make_ctx(mode=PermissionMode.ASK, asker=None)
    result = call(ctx, "write_file", path="x.txt", content="x")
    assert result.is_error, "无法确认时竟然放行了写操作"
    assert not (workspace / "x.txt").exists()


def test_blocklist_ignores_permission_mode():
    """高危命令即使在 auto 模式也要拦，auto 不等于无底线。"""
    ctx, _ = make_ctx(mode=PermissionMode.AUTO)
    for command in ["rm -rf /", "git push --force origin main", "git reset --hard"]:
        result = call(ctx, "run_command", command=command)
        assert result.is_error, f"高危命令未被拦截：{command}"
        assert "高危" in result.content


# ---------- dispatch 兜错 ----------

def test_dispatch_never_raises():
    ctx, _ = make_ctx()
    bad_inputs = [
        ("read_file", "{坏 JSON}"),
        ("read_file", '{"wrong_arg": 1}'),
        ("read_file", '["不是对象"]'),
        ("不存在的工具", "{}"),
        ("grep", '{"pattern": "["}'),          # 非法正则
    ]
    for name, raw in bad_inputs:
        result = dispatch(name, raw, ctx)      # 不抛异常即为通过
        assert result.is_error, f"{name} {raw} 本应报错"
        assert result.content, "错误信息不能为空，模型要靠它自我纠正"


def test_edit_file_requires_unique_match():
    ctx, workspace = make_ctx()
    (workspace / "dup.txt").write_text("x\nx\nx\n", encoding="utf-8")
    result = call(ctx, "edit_file", path="dup.txt", old_string="x", new_string="y")
    assert result.is_error and "不是唯一匹配" in result.content
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "x\nx\nx\n", "匹配不唯一时不该改动文件"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
