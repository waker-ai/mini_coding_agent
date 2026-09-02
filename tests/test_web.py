"""web_fetch 的边界测试：协议限制、SSRF 防护、URL 编码、正文提取。

全部离线运行，一个字节都不出网——测试要在断网、评委机器上、CI 里都跑得过，
依赖外部站点的测试早晚会因为对方改版而变成假警报。真正需要联网验证的部分
（洛谷题面、重定向到内网）靠手动跑过，结论记在 DESIGN.md 第 29 条。

localhost / 127.0.0.1 / 169.254.169.254 这几个解析不出网：前两个走本机
hosts，后一个本身就是字面量 IP，getaddrinfo 直接返回。

直接用 python tests/test_web.py 运行，不引入 pytest 依赖。
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import ToolContext, ToolError
from agent.tools.web import (
    MAX_TEXT_CHARS,
    _SafeRedirectHandler,
    _clip,
    _decode,
    _html_to_text,
    _strip_scripts,
    _validate,
)


def expect_error(url: str, keyword: str) -> None:
    try:
        _validate(url)
    except ToolError as exc:
        assert keyword in str(exc), f"{url} 的拒绝理由不对：{exc}"
        return
    raise AssertionError(f"{url} 本该被拒绝，却通过了校验")


# ---------- 协议限制 ----------

def test_rejects_non_http_schemes():
    # file:// 能读任意本地文件，等于绕开整个路径沙箱，这条最重要
    expect_error("file:///C:/Windows/win.ini", "只支持 http/https")
    expect_error("ftp://example.com/x", "只支持 http/https")
    expect_error("data:text/html,<h1>x</h1>", "只支持 http/https")


def test_rejects_url_without_scheme():
    expect_error("/etc/passwd", "只支持 http/https")
    expect_error("www.example.com", "只支持 http/https")


# ---------- SSRF：内网与回环 ----------

def test_blocks_loopback_and_private():
    for url in [
        "http://127.0.0.1:8000/api/tree",   # Web 界面自己就跑在这个地址上
        "http://localhost:8000/",
        "http://[::1]:8000/",
        "http://169.254.169.254/latest/meta-data/",  # 云厂商元数据接口
        "http://192.168.1.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
    ]:
        expect_error(url, "非公网地址")


def test_redirect_to_private_is_rejected():
    """公网地址 302 到内网——只校验最终落点是拦不住的，必须逐跳校验。"""
    handler = _SafeRedirectHandler()
    for target in ["http://127.0.0.1:8000/", "http://169.254.169.254/"]:
        try:
            handler.redirect_request(None, None, 302, "Found", {}, target)
        except urllib.error.URLError as exc:
            assert "重定向被拒绝" in str(exc.reason), f"理由不对：{exc.reason}"
            continue
        raise AssertionError(f"重定向到 {target} 本该被拒绝")


def test_redirect_to_other_scheme_is_rejected():
    handler = _SafeRedirectHandler()
    try:
        handler.redirect_request(None, None, 302, "Found", {}, "file:///C:/Windows/win.ini")
    except urllib.error.URLError as exc:
        assert "不支持的协议" in str(exc.reason)
        return
    raise AssertionError("重定向到 file:// 本该被拒绝")


# ---------- URL 编码 ----------

def test_encodes_non_ascii_url():
    """模型写出来的地址十有八九直接带中文，不编码就是 UnicodeEncodeError。"""
    got = _validate("https://www.luogu.com.cn/problem/list?keyword=采药")
    assert got == "https://www.luogu.com.cn/problem/list?keyword=%E9%87%87%E8%8D%AF", got


def test_does_not_double_encode():
    """已经编码过的 URL 再编一次会变成 %25E9，指向一个查不到东西的地址。"""
    encoded = "https://www.luogu.com.cn/problem/list?keyword=%E9%87%87%E8%8D%AF"
    assert _validate(encoded) == encoded


def test_keeps_query_structure():
    url = "https://example.com/a/b?x=1&y=2#frag"
    assert _validate(url) == url


# ---------- 内联 JSON 数据块 ----------

def test_keeps_json_script_and_drops_js():
    """洛谷的样例和搜索结果都只存在于 JSON 数据块里，一刀切掉 script 就全没了。"""
    html = (
        '<script>var tracker = 1; alert("noise")</script>'
        '<script type="application/json">{"pid":"P1048","samples":[["70 3","3"]]}</script>'
    )
    out = _strip_scripts(html)
    assert "tracker" not in out and "alert" not in out, "普通 JS 应当被丢掉"
    assert "P1048" in out and "70 3" in out, "JSON 数据块应当保留"


def test_unescapes_json_unicode():
    """原始 JSON 里一个汉字占 6 个字符，不解码纯属浪费上下文。"""
    html = '<script type="application/json">{"name":"\\u91c7\\u836f"}</script>'
    out = _strip_scripts(html)
    assert "采药" in out, f"\\uXXXX 未被解开：{out}"
    assert "\\u91c7" not in out


def test_malformed_json_survives():
    html = '<script type="application/json">{这不是合法 JSON</script>'
    assert "这不是合法 JSON" in _strip_scripts(html), "非法 JSON 应当原样保留而不是丢弃"


# ---------- 正文提取 ----------

def test_html_to_text_basics():
    html = """
    <html><head><title>T</title><style>body{color:red}</style></head>
    <body><nav><a href="/">首页</a><a href="/x">导航项</a></nav>
    <h2>题目描述</h2><p>输入两个整数 &lt;a,b&gt;，输出 a&amp;b。</p>
    <ul><li>第一条</li><li>第二条</li></ul>
    <script>evil()</script></body></html>
    """
    text = _html_to_text(html)
    assert "## 题目描述" in text, "标题应带 markdown 记号"
    assert "输入两个整数 <a,b>，输出 a&b。" in text, f"HTML 实体未还原：{text}"
    assert "- 第一条" in text and "- 第二条" in text
    assert "color:red" not in text, "<style> 内容应当被丢掉"
    assert "evil" not in text, "<script> 内容应当被丢掉"
    assert "导航项" not in text, "<nav> 应当被丢掉"
    assert "<" not in text.replace("<a,b>", ""), f"还有残留标签：{text}"


def test_html_to_text_drops_noise_lines():
    text = _html_to_text("<ul><li></li><li></li><li>真内容</li></ul>")
    assert "真内容" in text
    assert not any(line.strip() == "-" for line in text.splitlines()), "空列表项应当被清掉"


def test_html_to_text_on_broken_markup():
    """畸形 HTML 不能让工具崩掉，最差只是多留几个标签。"""
    assert "内容" in _html_to_text("<div><p>内容<span></div></body>")


# ---------- 截断与解码 ----------

def test_clip_keeps_head_and_tail():
    """洛谷把正文放在页面最末尾，只留头部会正好把要读的东西切掉。"""
    text = "HEAD" + "x" * (MAX_TEXT_CHARS * 2) + "TAIL"
    clipped, was_clipped = _clip(text)
    assert was_clipped
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert "中间省略" in clipped


def test_clip_leaves_short_text_alone():
    clipped, was_clipped = _clip("短文本")
    assert clipped == "短文本" and not was_clipped


def test_decode_prefers_header_charset():
    assert _decode("你好".encode("gb18030"), "text/html; charset=gb18030", "") == "你好"


def test_decode_falls_back_to_meta_charset():
    raw = '<meta charset="gb18030">你好'.encode("gb18030")
    assert "你好" in _decode(raw, "text/html", "")


def test_decode_never_raises():
    """解不开也要返回点东西——为一次编码猜测让整次抓取失败不值得。"""
    assert _decode(b"\xff\xfe\x00garbage", "", "") is not None


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
