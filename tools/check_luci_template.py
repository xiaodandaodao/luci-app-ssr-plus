#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LuCI .htm 模板静态校验：
   1) 按 LuCI parser 的规则切分标签，重建等价的 Lua chunk 并用 luaparser 解析；
   2) 把模板近似渲染一遍，抽出 <script> 块用 node --check 校验 JS 语法。
"""
import re
import shutil
import subprocess
import sys
import tempfile
import os

TAG = re.compile(r"<%([=+:#-]?)(.*?)%>", re.S)
SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)

NODE = os.environ.get("NODE_BIN") or shutil.which("node") or shutil.which("nodejs") or "node"


def split_tags(text):
    """产出 (prefix, body) —— 去掉 LuCI 的 '-%>' 去空白标记。"""
    for m in TAG.finditer(text):
        prefix, body = m.group(1), m.group(2)
        if body.endswith("-"):
            body = body[:-1]
        yield prefix, body


def build_lua_chunk(text):
    out = []
    for prefix, body in split_tags(text):
        if prefix == "" or prefix == "-":
            out.append(body)
            out.append("\n")
        elif prefix == "=":
            out.append("__w(%s)\n" % body)
        elif prefix == ":":
            out.append("__w(%s)\n" % repr(body))
        elif prefix == "+":
            out.append("__inc(%s)\n" % repr(body))
        # '#' 注释：直接丢弃
    return "\n".join(out)


def build_render(text):
    out = []
    for m in TAG.finditer(text):
        prefix, body = m.group(1), m.group(2)
        if prefix == "" or prefix == "-":
            out.append("")
        elif prefix == "=":
            out.append("0")
        elif prefix == ":":
            out.append(body)
        elif prefix == "+":
            out.append("/* include %s */" % body)
        else:
            out.append("")
    # 用于 JS 校验时，只关心脚本块，用占位替换 Lua 标签即可
    return TAG.sub(lambda m: _sub(m), text)


def _sub(m):
    prefix, body = m.group(1), m.group(2)
    if prefix == "=":
        return "0"
    if prefix == ":":
        return body
    if prefix == "+":
        return ""
    return ""


def check_lua(path):
    src = open(path, encoding="utf-8").read()
    chunk = build_lua_chunk(src)
    wrap = "local self = nil\nlocal __w = function(...) end\nlocal __inc = function(...) end\n" + chunk
    try:
        from luaparser import ast
        ast.parse(wrap)
        return True, "lua ok (%d bytes, %d tags)" % (len(wrap), len(TAG.findall(src)))
    except Exception as exc:  # noqa: BLE001
        return False, "lua FAIL: %s" % exc


def check_js(path):
    src = open(path, encoding="utf-8").read()
    rendered = TAG.sub(lambda m: _sub(m), src)
    blocks = SCRIPT.findall(rendered)
    if not blocks:
        return True, "no script"
    msgs = []
    ok_all = True
    for i, block in enumerate(blocks):
        # 剥离 CDATA 包装
        block = block.replace("//<![CDATA[", "").replace("//]]>", "")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(block)
            tmp = fh.name
        p = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
        os.unlink(tmp)
        if p.returncode != 0:
            ok_all = False
            msgs.append("script#%d FAIL:\n%s" % (i, p.stderr.strip()))
        else:
            msgs.append("script#%d ok" % i)
    return ok_all, "; ".join(msgs)


def main():
    rc = 0
    for path in sys.argv[1:]:
        print("=" * 70)
        print(path)
        ok1, m1 = check_lua(path)
        print("  [lua] " + m1)
        ok2, m2 = check_js(path)
        print("  [js ] " + m2)
        if not (ok1 and ok2):
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
