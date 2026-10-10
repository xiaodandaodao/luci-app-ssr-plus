#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「自定义分流规则」页签单独渲染成离线预览页（明 / 暗两套）。

用途：验证该页签在深色主题下的可读性 —— 例如 textarea 的底色与文字色是否
真的跟随主题。历史上这里误用了并不存在的 CSS 变量（--scui-border /
--scui-panel-bg），浅色兜底值让深色主题下变成「浅底 + 浅字」。

做法是从真实模板里抽 CSS 与标记，不做任何手写副本，保证所见即线上。

用法：
    # 1) 先把路由器上真正生效的主题样式拉下来（只需一次）
    RHOST=… RUSER=… RPWD=… python3 /tmp/rssh.py 'base64 /www/luci-static/bootstrap/cascade.css' \\
        | base64 -d > out/theme/bootstrap-cascade.css
    # 2) 渲染明暗两套
    python3 tools/build_domainrules_preview.py
    # 3) 截图
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \\
        --disable-gpu --no-sandbox --window-size=1180,1080 \\
        --screenshot=out/shot.png "file://$PWD/out/domainrules-dark.html"
    # 4) 想量化对比度而不是靠眼睛，加 --probe 后用 --dump-dom 读出来
    python3 tools/build_domainrules_preview.py --probe
    … --headless=new --dump-dom … | grep -A4 'probe-out'

不带 --theme-css 也能跑，但页面上没有主题样式，对比度会偏乐观。
"""
import argparse
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS = os.path.join(REPO, "luasrc", "view", "shadowsocksr")
UI_TPL = os.path.join(VIEWS, "clash_groups_ui.htm")
PANEL_TPL = os.path.join(VIEWS, "clash_main_panel.htm")

STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
LUA_TAG = re.compile(r"<%([=+:#-]?)(.*?)%>", re.S)

# 只挑这个页签用得到的字符串（与 po/zh_Hans 一致）
ZH = {
    "Clash Panel": "Clash 面板",
    "Ready.": "就绪。",
    "Proxy Groups": "策略组",
    "Client Rules": "客户端规则",
    "Custom Domain Rules": "自定义分流规则",
    "Component Update": "组件升级",
    "Save": "保存",
    "Close": "关闭",
    "One Clash rule per line, injected before every rule from the subscription. "
    "Format: TYPE,ARGUMENT,POLICY — the policy must be an existing proxy group "
    "(or DIRECT / REJECT).":
        "一行一条 Clash 规则，插在订阅全部规则的最前面。格式：规则类型,参数,目标 —— "
        "目标必须是已存在的代理组（或 DIRECT / REJECT）。",
    "Example": "例如",
    "Insert PikPak Template": "插入 PikPak 模板",
    "Available targets": "可用目标组",
}

# 模拟后端返回的规则内容（与 buildPikpakTemplate 等价）
RULES = """// 自定义分流规则 —— 每行一条 Clash 原生规则,插在订阅规则的最前面
// 格式： 类型,参数,目标组
// 目标组是代理组名(面板上看到的 Proxies / HK / US / 我的香港组 ...)
// 也可以是内置策略：DIRECT(直连)、REJECT(拦截)
// 以 # 开头的是注释,改动保存即生效。

// ===== PikPak 网盘 =====
// 下载 CDN (形如 dl-a10b-0858.mypikpak.com / dl-z01a-*.mypikpak.com):
// 国内直连更快,也不烧机场流量。顺序重要:这两条必须排在下面的 DOMAIN-SUFFIX 前面。
DOMAIN-KEYWORD,dl-a10b-,DIRECT
DOMAIN-KEYWORD,dl-z01a-,DIRECT

// 主站 / API / 地区检测 / 下载调度： PikPak 主动屏蔽大陆 IP, 必须显式走代理。
// pikpak.io / pikpak.site 是 Cloudflare 承载的下载与调度域名 (104.18.x),
// 实测国内直连 10 秒以上无响应, 同样不能漏。
DOMAIN-SUFFIX,mypikpak.com,HK
DOMAIN-SUFFIX,mypikpak.net,HK
DOMAIN-SUFFIX,pikpak.me,HK
DOMAIN-SUFFIX,pikpak.io,HK
DOMAIN-SUFFIX,pikpak.site,HK
DOMAIN-SUFFIX,pikpakdrive.com,HK
"""

POLICIES = ["Apple", "Bahamut", "Bilibili", "DIRECT", "Disney", "Final", "GLOBAL",
            "Google", "HK", "Hiboxmax", "JP", "Microsoft", "Netflix", "OpenAI",
            "Proxies", "REJECT", "SG", "Spotify", "TW", "US", "YouTube", "香港优化"]

# 探针：把关键元素的真实计算样式写进 <pre>，配合
#   Chrome --headless --dump-dom 即可读出，用来断言对比度而不是靠肉眼。
PROBE_JS = """
	var out = [];
	function probe(sel, label) {
		var el = document.querySelector(sel);
		if (!el) { out.push(label + ' => (未找到)'); return; }
		var cs = getComputedStyle(el);
		out.push(label + ' => bg=' + cs.backgroundColor + ' color=' + cs.color +
			' border=' + cs.borderTopColor);
	}
	probe('#ssr-clash-modal', '弹窗');
	probe('#ssr-clash-domain-rules-input', 'textarea');
	probe('#ssr-clash-pane-domainrules code', 'code 标签');
	var modalEl = document.querySelector('#ssr-clash-modal');
	if (modalEl) {
		var mcs = getComputedStyle(modalEl);
		out.push('darkmode=' + JSON.stringify(document.documentElement.getAttribute('data-darkmode')) +
			'  matches(.ssr-clash-ui)=' + modalEl.matches('.ssr-clash-ui') +
			'  --scui-bg=[' + mcs.getPropertyValue('--scui-bg') + ']' +
			'  --scui-fg=[' + mcs.getPropertyValue('--scui-fg') + ']' +
			'  --scui-surface-2=[' + mcs.getPropertyValue('--scui-surface-2') + ']');
	}
	var pre = document.createElement('pre');
	pre.id = 'probe-out';
	pre.style.display = 'none';
	pre.textContent = out.join('\\n');
	document.body.appendChild(pre);
"""


def strip_tags(src):
    def sub(m):
        prefix, body = m.group(1), m.group(2)
        if prefix in ("", "-", "+"):
            return ""
        if prefix == "=":
            return "nil"
        if prefix == "#":
            return ""
        return ZH.get(body, body)

    return LUA_TAG.sub(sub, src)


def extract_styles(path):
    """取出模板里真正的 <style> 块。

    注意：必须先剥掉 <% ... %> 的 Lua 块 —— 模板的头部注释里出现过
    「只输出 <style> + <script>」这样的字样，直接跑正则会把注释里的
    `<style>` 当成起点，把整段 Lua 注释咽进样式表，导致紧随其后的
    第一条 CSS 规则（设计变量块）被解析器整条丢弃。踩过一次。
    """
    src = open(path, encoding="utf-8").read()
    src = re.sub(r"<%.*?%>", "", src, flags=re.S)
    return "\n".join(STYLE.findall(src))


def extract_panel_body():
    src = open(PANEL_TPL, encoding="utf-8").read()
    start = src.index('<div id="ssr-clash-main-panel-wrap">')
    end = src.index("<%+shadowsocksr/clash_groups_ui%>")
    return src[start:end]


def build(dark, theme_css=None, probe=False):
    ui_css = extract_styles(UI_TPL)
    panel_css = extract_styles(PANEL_TPL)
    body = strip_tags(extract_panel_body())

    # 填入内容 / 提示
    body = body.replace(
        'id="ssr-clash-domain-rules-input"',
        'id="ssr-clash-domain-rules-input" data-filled="1"')
    body = body.replace(
        '<span id="ssr-clash-domain-rules-hint" class="ssr-domain-rules-hint"></span>',
        '<span id="ssr-clash-domain-rules-hint" class="ssr-domain-rules-hint">'
        '可用目标组: ' + " / ".join(POLICIES[:14]) + " …</span>")

    # 只让 domainrules 这一个页签可见；弹窗改为静态排布
    harness_css = """
	html, body { margin: 0; }
	body {
		padding: 24px;
		background: #edeff4;
		font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
			"Hiragino Sans GB", "Microsoft YaHei", sans-serif;
	}
	html[data-darkmode="true"] body { background: #090b10; }
	#ssr-clash-main-buttons, #ssr-clash-modal-mask { display: none !important; }
	#ssr-clash-modal {
		display: block !important;
		position: static !important;
		transform: none !important;
		width: auto !important;
		max-height: none !important;
		overflow: visible !important;
	}
	#ssr-clash-pane-groups, #ssr-clash-pane-rules, #ssr-clash-pane-components { display: none !important; }
	#ssr-clash-pane-domainrules { display: block !important; }
"""

    script = """
	document.getElementById('ssr-clash-domain-rules-input').value = %s;
""" % repr(RULES) + (PROBE_JS if probe else "")

    html_attr = ' data-darkmode="true"' if dark else ""
    theme = "dark" if dark else "light"
    theme_block = ""
    if theme_css:
        theme_block = ('<style type="text/css">/* LuCI 主题 cascade.css（原样内联，置于最前，'
                       '与线上加载顺序一致）*/\n' + theme_css + '\n</style>\n')
    return f"""<!DOCTYPE html>
<html{html_attr}>
<head>
<meta charset="utf-8" />
<title>自定义分流规则 · {theme}</title>
{theme_block}<style type="text/css">
{ui_css}
</style>
<style type="text/css">
{panel_css}
</style>
<style type="text/css">
{harness_css}
</style>
</head>
<body>
{body}
<script>
{script}
{probe}
</script>
</body>
</html>
"""


def main():
    global PANEL_TPL
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(REPO, "out"))
    ap.add_argument("--panel", default=None,
                    help="改用另一份面板模板（例如 git show HEAD:… 出来的旧版本，用于对比）")
    ap.add_argument("--suffix", default="",
                    help="输出文件名后缀，如 before / after")
    ap.add_argument("--probe", action="store_true",
                    help="在页面里塞一个隐藏的 <pre id=\"probe-out\">，写关键元素的计算样式，"
                         "配合 Chrome --headless --dump-dom 做断言")
    ap.add_argument("--theme-css", default=os.path.join(REPO, "out", "theme", "bootstrap-cascade.css"),
                    help="LuCI 主题 cascade.css；给了就把页面渲染在真实主题之下（推荐）")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    if args.panel:
        PANEL_TPL = os.path.abspath(args.panel)
        print("使用面板模板:", PANEL_TPL)
    theme_css = None
    if args.theme_css and os.path.exists(args.theme_css):
        theme_css = open(args.theme_css, encoding="utf-8", errors="replace").read()
        print("已内联主题 CSS:", args.theme_css, len(theme_css), "字节")
    else:
        print("警告：未找到主题 CSS，仅按面板自带样式渲染（对比度会偏乐观）")
    for dark in (False, True):
        name = "domainrules-%s%s.html" % ("dark" if dark else "light", args.suffix)
        path = os.path.join(args.out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(build(dark, theme_css, args.probe))
        print("已生成", path)


if __name__ == "__main__":
    main()
