"""web_fetch：抓取一个公开网页，把正文提取成纯文本交给模型。

为什么只做 fetch、不做 search：search 要把一句自然语言映射成一组 URL，
这需要全网倒排索引，只能租用第三方（Serper / Brave / Bing）——要 API key、
按次付费、多一份凭据要管。而大量站点自带站内检索（洛谷、GitHub、PyPI），
用 fetch 抓它们的搜索页就够了，不必为此引入一个付费依赖。

只用标准库 urllib，理由和 search.py 里那条一样：零依赖、跨平台行为一致。

三条安全边界（都写在代码里，不指望提示词）：
  1. 只允许 http/https。file:// 能读任意本地文件，直接绕过路径沙箱。
  2. 拒绝内网与回环地址，且重定向的每一跳都要重新校验。否则模型可以抓
     127.0.0.1:8000（Web 界面自己）或 169.254.169.254（云厂商元数据接口）——
     这类 SSRF 的危险在于请求是从这台机器内部发出的，防火墙拦不住。
  3. 只接受文本类 Content-Type，且下载有字节上限。二进制塞进上下文毫无意义。

不需要用户确认：GET 是只读的，本地什么都不改，而查资料往往要连着抓好几页，
每页弹一次确认框就没法用了。代价要说清楚——URL 本身是一条出网通道，
auto 模式下模型可以把任意内容拼进 query string 发出去。真要堵这个口子得做
域名白名单，那是另一个层级的工程。
"""
from __future__ import annotations

import gzip
import html as html_lib
import http.cookiejar
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse, urlunparse

from .base import ToolContext, ToolError, tool

DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
# 下载字节上限。先于解码生效，避免一个巨型页面把内存和上下文一起打爆。
MAX_DOWNLOAD_BYTES = 3_000_000
# 提取出的正文字符上限，与 config.max_tool_output 取同一量级
MAX_TEXT_CHARS = 20_000

# 默认 UA 是 Python-urllib/3.x，不少站点直接拒绝。这里表明自己是什么，
# 同时给出一个常规浏览器前缀，避免被当成异常流量。
USER_AGENT = "Mozilla/5.0 (compatible; mini-coding-agent/0.1) Python-urllib"

TEXTUAL_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


def _blocked_host_reason(host: str) -> str | None:
    """把主机名解析成 IP，判断它是不是内网/回环/元数据地址。

    必须真的解析，而不是只看字面量：localtest.me 这类公网域名会解析到
    127.0.0.1，只做字符串匹配挡不住。一个域名可能有多条 A 记录，
    任意一条落在内网就整体拒绝。
    """
    if not host:
        return "URL 里没有主机名"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return f"域名解析失败：{host}（{exc}）"

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"{host} 解析到非公网地址 {ip}，拒绝访问内网与回环地址"
    return None


def _validate(url: str) -> str:
    """校验一个 URL 能不能抓，返回规范化后的 URL。不合格就抛 ToolError。

    这里必须做百分号编码：urllib 只接受纯 ASCII 的 URL，而模型写出来的
    地址十有八九直接带中文（`?keyword=采药` 正是最主要的用法），不编码就是
    一个 UnicodeEncodeError。safe 里带上 % 是为了不破坏已经编码过的 URL——
    否则 %E9 会被再编码成 %25E9，变成一个查不到东西的地址。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        shown = parsed.scheme or "（没有协议头）"
        raise ToolError(
            f"只支持 http/https，收到的是 {shown}。读本地文件请用 read_file。"
        )

    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        # urlparse 的 .port 在端口号非法时才抛，且报错文案完全不提这是个 URL
        raise ToolError(f"URL 格式不对：{exc}") from None

    try:
        # 中文域名（IDN）要转成 punycode，getaddrinfo 和 HTTP 头都只认 ASCII
        ascii_host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError:
        raise ToolError(f"无法解析的域名：{host}") from None

    reason = _blocked_host_reason(ascii_host)
    if reason:
        raise ToolError(reason)

    netloc = ascii_host
    if port:
        netloc = f"{netloc}:{port}"
    if parsed.username:
        credential = parsed.username
        if parsed.password:
            credential += f":{parsed.password}"
        netloc = f"{credential}@{netloc}"

    safe = "/%:@&=+$,;~!*'()"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            quote(parsed.path, safe=safe),
            quote(parsed.params, safe=safe),
            quote(parsed.query, safe=safe + "?"),
            quote(parsed.fragment, safe=safe + "?"),
        )
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新校验目标地址。

    只校验最终落点是不够的：一个公网 URL 可以 302 到 127.0.0.1，
    那一跳的请求照样会真的发出去。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError(f"重定向到了不支持的协议：{newurl}")
        reason = _blocked_host_reason(parsed.hostname or "")
        if reason:
            raise urllib.error.URLError(f"重定向被拒绝：{reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode(raw: bytes, content_type: str, encoding_header: str) -> str:
    """按 HTTP 头 → meta 标签 → utf-8 → gb18030 的顺序试着解码。"""
    if "gzip" in encoding_header:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # 截断的 gzip 流解不开就按原样处理，不值得为此让整次抓取失败

    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    if not charset:
        # HTTP 头没写就翻页面里的 meta，中文站点上很常见
        head = raw[:4096].decode("ascii", errors="ignore")
        meta = re.search(r"charset=[\"']?([\w\-]+)", head, re.I)
        if meta:
            charset = meta.group(1)

    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_scripts(html: str) -> str:
    """去掉 <script>，但保留 type 是 JSON 的那些。

    type="application/json" 装的是数据不是代码，站点普遍用它把页面数据内联
    进 HTML（JSON-LD 就是这么规定的）。这些数据块往往比渲染出来的 HTML
    更完整：洛谷题目页的输入输出样例、时空限制只存在于这里，页面上那几个
    小节反而没有；按题目名搜题号拿到的结果列表也在这里。一刀切掉所有
    script，这两件事都做不成。

    保留时顺手把 \\uXXXX 转义解开——原始 JSON 里一个汉字要占 6 个字符，
    直接塞进上下文纯属浪费，模型读起来也费劲。
    """

    def decide(match: re.Match) -> str:
        attrs, body = match.group(1), match.group(2)
        if not re.search(r"type\s*=\s*[\"']?[^\"'>]*json", attrs, re.I):
            return " "
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass  # 不是合法 JSON 就原样保留，能读多少算多少
        return "\n" + body + "\n"

    return re.sub(r"<script\b([^>]*)>(.*?)</script\s*>", decide, html, flags=re.S | re.I)


def _html_to_text(html: str) -> str:
    """把 HTML 压成纯文本。

    刻意不引 BeautifulSoup / lxml：一是保持零依赖，二是这里要的不是精确的
    DOM，而是"能读的正文"。正则方案在畸形 HTML 上也不会崩，最差只是多留
    几个标签，对模型来说完全可接受。
    """
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    text = _strip_scripts(text)
    text = re.sub(r"<style\b[^>]*>.*?</style\s*>", " ", text, flags=re.S | re.I)
    # 导航栏、页脚、内联 SVG 全是噪音，占的 token 常常比正文还多
    text = re.sub(
        r"<(nav|footer|aside|svg|form|iframe|noscript)\b[^>]*>.*?</\1\s*>",
        " ",
        text,
        flags=re.S | re.I,
    )
    # 标题和列表项带上 markdown 记号，模型更容易分辨文档结构
    text = re.sub(
        r"<h([1-6])\b[^>]*>",
        lambda m: "\n\n" + "#" * int(m.group(1)) + " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(
        r"</?(br|p|div|section|article|tr|h[1-6]|ul|ol|table|blockquote|pre)\b[^>]*>",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    # 空列表项和表格分隔符会剩下一堆孤零零的 "-" / "|"。导航栏是重灾区，
    # 一个页面能剩十几行，纯占 token。
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line not in ("-", "|", "- |", "-|")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clip(text: str) -> tuple[str, bool]:
    """超长时保留头尾，取舍与 history._truncate 一致。

    网页的头尾都不能丢：正文通常在前面，但也有站点把服务端渲染的内容放在
    页面最末尾（洛谷就是这样），只留头部会正好把要读的东西切掉。
    """
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    half = MAX_TEXT_CHARS // 2
    omitted = len(text) - MAX_TEXT_CHARS
    return f"{text[:half]}\n\n…（中间省略 {omitted} 个字符）…\n\n{text[-half:]}", True


@tool(
    name="web_fetch",
    description=(
        "抓取一个公开网页并返回其正文文本（HTML 会被转成纯文本，JSON 与纯文本原样返回）。"
        "用于查阅在线文档、题目页面、API 说明等本地没有的资料。"
        "只支持 http/https，不能读本地文件（那是 read_file 的事），也不能访问内网地址。\n"
        "它抓的是服务端返回的原始内容，不执行 JavaScript，因此纯前端渲染的站点"
        "可能只抓到一个空壳。不过很多站点会把完整数据内联成 JSON 放在页面里，"
        "这部分会被保留下来，往往比渲染出的文字更全（洛谷题目页的输入输出样例、"
        "时空限制就只在这里面），值得优先读。确实缺内容时要如实说明，"
        "不要凭猜测补全。\n"
        "没有搜索工具。需要按关键词找内容时，抓目标站点自己的搜索页，"
        "例如 https://www.luogu.com.cn/problem/list?keyword=关键词 。"
        "搜索结果有多条时，先把候选列给用户确认，不要自己认定是哪一条。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的完整 URL，必须以 http:// 或 https:// 开头",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
        },
        "required": ["url"],
    },
)
def web_fetch(ctx: ToolContext, url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    target = _validate(url)
    try:
        seconds = min(max(int(timeout), 1), MAX_TIMEOUT)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT

    request = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    # 每次调用都用一个全新的 cookie 罐，且用完即弃。
    # 必须有：不少站点（洛谷就是）首次访问时先下发 cookie 再 302，客户端不收
    # cookie 就会在同一个地址上反复跳转，urllib 最后报"无限重定向"而不是真实原因。
    # 不跨调用复用：cookie 会累积成会话状态，一次登录态可能被带到无关站点上去。
    opener = urllib.request.build_opener(
        _SafeRedirectHandler,
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )

    try:
        with opener.open(request, timeout=seconds) as response:
            final_url = response.geturl()
            # 重定向处理器已经逐跳校验过，这里再确认一次落点，双保险
            reason = _blocked_host_reason(urlparse(final_url).hostname or "")
            if reason:
                raise ToolError(reason)

            content_type = response.headers.get("Content-Type", "")
            lowered = content_type.lower()
            if content_type and not any(t in lowered for t in TEXTUAL_TYPES):
                raise ToolError(
                    f"这个地址返回的是 {content_type.split(';')[0].strip()}，"
                    "不是文本内容，无法转成文字读取。"
                )
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
            encoding_header = (response.headers.get("Content-Encoding") or "").lower()
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ToolError(f"请求失败，HTTP {exc.code} {exc.reason}（{target}）") from None
    except urllib.error.URLError as exc:
        raise ToolError(f"无法访问 {target}：{exc.reason}") from None
    except TimeoutError:
        raise ToolError(f"请求超时（{seconds} 秒）：{target}") from None
    except OSError as exc:
        raise ToolError(f"网络错误：{exc}") from None

    oversized = len(raw) > MAX_DOWNLOAD_BYTES
    body = _decode(raw[:MAX_DOWNLOAD_BYTES], content_type, encoding_header)

    looks_html = "html" in content_type.lower() or body.lstrip()[:200].lower().startswith(
        ("<!doctype", "<html")
    )
    if looks_html:
        text = _html_to_text(body)
        kind = "HTML→文本"
    else:
        text = body.strip()
        kind = content_type.split(";")[0].strip() or "文本"

    if not text:
        return (
            f"抓取成功但没有提取到任何文本（{final_url}，HTTP {status}，{kind}）。"
            "这个页面的内容很可能由 JavaScript 渲染，服务端返回的是个空壳。"
        )

    text, clipped = _clip(text)
    header = f"{final_url}\nHTTP {status} · {kind} · 正文 {len(text)} 字符"
    if clipped:
        header += "（已截断，保留了头尾两段）"
    if oversized:
        header += f"\n注意：页面超过 {MAX_DOWNLOAD_BYTES} 字节，只下载了前面一部分。"
    return f"{header}\n\n{text}"
