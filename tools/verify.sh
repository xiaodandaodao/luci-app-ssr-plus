#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# verify.sh — 改完代码后的一键静态校验（本地与 GitHub Actions 共用）。
#
#   1) Lua 语法（luaparser）
#   2) root/ 下 init.d / *.sh 的 POSIX shell 语法（sh -n）
#   3) .po 翻译条目无重复 msgid
#   4) LuCI .htm 模板：切分出的 Lua chunk 可解析 + <script> 内 JS 可解析
#   5) 生成面板离线预览并用 jsdom 跑冒烟测试（按钮不出现 undefined 之类）
#
# 依赖：python3、node、pip install luaparser、npm i jsdom（后两者缺失会 [warn] 跳过）
# 用法：./tools/verify.sh
# ---------------------------------------------------------------------------
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

pass=0; fail=0
ok()   { printf '\033[32m  ok\033[0m   %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '\033[31m  FAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }
info() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------- 1) Lua 语法
info "Lua 语法（luaparser）"
if python3 -c "import luaparser" 2>/dev/null; then
  if python3 - <<'PY'
import glob, sys
from luaparser import ast
bad = 0
files = sorted(glob.glob("luasrc/**/*.lua", recursive=True) +
               glob.glob("root/**/*.lua", recursive=True))
for f in files:
    try:
        ast.parse(open(f, encoding="utf-8").read())
    except Exception as e:
        print("  FAIL %s: %s" % (f, e)); bad += 1
print("  检查了 %d 个 lua 文件" % len(files))
sys.exit(1 if bad else 0)
PY
  then ok "luaparser 解析全部 lua"; else bad "lua 语法有误"; fi
else
  warn "未安装 luaparser，跳过（pip install luaparser）"
fi

# ------------------------------------------------------------ 2) shell 语法
info "Shell 语法（sh -n）"
sh_files="$(find root -type f \( -name '*.sh' -o -path '*/init.d/*' \) 2>/dev/null)"
sh_n=0; sh_bad=0
for f in $sh_files; do
  sh_n=$((sh_n+1))
  if sh -n "$f" 2>/dev/null; then :; else bad "shell 语法错误: $f"; sh_bad=$((sh_bad+1)); fi
done
[ "$sh_bad" -eq 0 ] && ok "shell 语法（$sh_n 个文件）"

# -------------------------------------------------------------- 3) .po 重复项
info "PO 翻译重复检查"
if python3 - <<'PY'
import glob, sys

def msgids(path):
    """按 PO 规则把多行 msgid（msgid "" + 续行）拼成完整键。"""
    out, cur = [], None
    for line in open(path, encoding="utf-8"):
        s = line.rstrip("\n")
        if s.startswith("msgid "):
            if cur is not None:
                out.append(cur)
            cur = s[6:].strip().strip('"')
        elif s.startswith('"') and cur is not None:
            cur += s.strip().strip('"')
        elif s.startswith("msgctxt ") or s == "":
            if cur is not None:
                out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out

pofiles = sorted(glob.glob("po/*/*.po"))
dup_total = total = 0
for po in pofiles:
    ids = msgids(po)
    total += len(ids)
    seen = {}
    for i in ids:
        seen[i] = seen.get(i, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    if dups:
        print("  FAIL %s: %d 个重复 msgid" % (po, len(dups)))
        for k, v in list(dups.items())[:3]:
            print("        %dx %s" % (v, k[:60]))
        dup_total += len(dups)
print("  检查了 %d 个 .po（%d 个条目）" % (len(pofiles), total))
sys.exit(1 if dup_total else 0)
PY
then ok "po 无重复 msgid"; else bad "po 有重复 msgid"; fi

# ------------------------------------------------------- 4) .htm 模板 + JS 语法
info "LuCI 模板（lua chunk + JS）"
if python3 tools/check_luci_template.py; then ok "htm 模板校验"; else bad "htm 模板校验失败"; fi

# ----------------------------------------------------- 5) 面板预览 + jsdom 冒烟
info "面板离线预览 + 冒烟测试"
NODE_BIN="$(command -v node || true)"
if [ -n "$NODE_BIN" ]; then
  if [ ! -d tools/node_modules/jsdom ]; then
    (cd tools && npm install --silent jsdom >/dev/null 2>&1) || warn "npm 装 jsdom 失败，跳过冒烟测试"
  fi
  if [ -d tools/node_modules/jsdom ]; then
    python3 tools/build_preview.py >/dev/null 2>&1 || bad "生成 panel-preview.html 失败"
    if NODE_PATH="$REPO/tools/node_modules" "$NODE_BIN" tools/smoke_test.js; then
      ok "面板冒烟测试通过"
    else
      bad "面板冒烟测试失败"
    fi
  else
    warn "缺 jsdom，跳过冒烟测试"
  fi
else
  warn "未安装 node，跳过预览与冒烟测试"
fi

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m全部通过\033[0m（%d 项）\n' "$pass"
  exit 0
fi
printf '\033[31m%d 项失败\033[0m（通过 %d 项）\n' "$fail" "$pass"
exit 1
