#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deps.sh — 为 build.py 准备两个外部二进制：
#   1) tools/po2lmo   : 把 po/ 里的 .po 编译成 LuCI 的 .lmo（Apache-2.0，openwrt/luci）
#   2) tools/apk      : apk-tools 静态二进制（apk-static），用于产出 .apk
#
# 两者都按本机架构获取；已存在且可执行的直接跳过（幂等，可重复跑）。
# 用法：  ./tools/deps.sh        （跑在仓库根）
# ---------------------------------------------------------------------------
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$REPO/tools"
mkdir -p "$TOOLS"

info() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

usable() { # usable <file> [args...]
  local f="$1"; shift
  [ -x "$f" ] || return 1
  "$f" "$@" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# 1) po2lmo
# ---------------------------------------------------------------------------
prepare_po2lmo() {
  if usable "$TOOLS/po2lmo" -h; then
    info "po2lmo 已存在，跳过"
    return 0
  fi
  command -v cc gcc clang >/dev/null 2>&1 || die "需要 C 编译器（gcc/clang）来编译 po2lmo"

  # po2lmo.c 只依赖 lmo.h（头文件）和 sfh_hash()（po2lmo 用的哈希函数）。
  # 官方 lib/lmo.c 把 sfh_hash 写在需要 lemon 生成 plural_formula.h 的文件里，
  # 这里把 sfh_hash 摘出来内置，省掉 lemon/bison 整条依赖链（见 tools/sfh_hash.c）。
  info "下载 po2lmo 源码（openwrt/luci, modules/luci-base/src）"
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/lib"
  local luci_base="https://raw.githubusercontent.com/openwrt/luci/master/modules/luci-base/src"
  curl -fsSL "$luci_base/po2lmo.c" -o "$tmp/po2lmo.c" \
    || die "下载 po2lmo.c 失败"
  curl -fsSL "$luci_base/lib/lmo.h" -o "$tmp/lib/lmo.h" \
    || die "下载 lmo.h 失败"
  cp "$REPO/tools/sfh_hash.c" "$tmp/sfh_hash.c" || die "缺少 tools/sfh_hash.c"

  # 收集候选 lua 头文件目录（Debian/Ubuntu 的 /usr/include/luaX.Y，Homebrew 的 Cellar 路径）
  local hp; hp="$(command -v brew >/dev/null 2>&1 && brew --prefix 2>/dev/null || true)"
  local cands=( /usr/include/lua5.1 /usr/include/lua5.3 /usr/include/lua5.4 /usr/include/lua
                /usr/local/include )
  if [ -n "$hp" ]; then
    cands+=( $(ls -d "$hp"/Cellar/lua/*/include/lua* 2>/dev/null) )
  fi

  local probe="$tmp/probe.c"
  cat > "$probe" <<'EOF'
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
int main(void) { lua_State *l = luaL_newstate(); luaL_openlibs(l); lua_close(l); return 0; }
EOF

  local hdr="" libname="" libdir=""
  local d
  for d in "${cands[@]}"; do
    [ -f "$d/lua.h" ] || continue
    local bn; bn="$(basename "$d")"
    local n; n="${bn#lua}"          # lua5.4 -> 5.4, lua -> (空)
    case "$bn" in
      lua5.*) libname="lua$bn" ;;
      lua)    libname="lua" ;;
      *)      continue ;;
    esac
    local candlib
    candlib="$(dirname "$(dirname "$d")")/lib"        # .../include/lua5.4 -> .../lib
    [ -d "$candlib" ] || candlib="/usr/lib/$(cc -dumpmachine 2>/dev/null)"
    if cc -O2 -I"$d" -L"$candlib" -l"$libname" -o "$tmp/probe.bin" "$probe" -lm >/dev/null 2>&1; then
      hdr="$d"; libdir="$candlib"; break
    fi
  done
  [ -n "$hdr" ] || die "找不到可用的 lua 开发库，请先安装：apt install liblua5.4-dev / brew install lua@5.1"
  info "使用 lua：${hdr}（库 ${libname}，在 ${libdir}）"

  info "编译 po2lmo ..."
  ( cd "$tmp" && cc -O2 -I"$hdr" -I"$tmp/lib" -L"$libdir" -o po2lmo po2lmo.c sfh_hash.c -l"$libname" -lm ) \
    || die "编译 po2lmo 失败（lua 版本不兼容？换 liblua5.1-dev 再试）"
  mv "$tmp/po2lmo" "$TOOLS/po2lmo"
  chmod +x "$TOOLS/po2lmo"
  rm -rf "$tmp"
  info "po2lmo -> $TOOLS/po2lmo"
}

# ---------------------------------------------------------------------------
# 2) apk-static（静态链接，musl 目标，glibc 机器可直接跑）
# ---------------------------------------------------------------------------
apk_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo x86_64 ;;
    arm64|aarch64) echo aarch64 ;;
    *) echo "" ;;
  esac
}

# 从 alpine APKINDEX 里解析 apk-tools 最新版本（PKG_ARCH 已固定到具体架构）
latest_apktools_ver() {
  local arch="$1"
  local idx="https://dl-cdn.alpinelinux.org/alpine/latest/main/$arch/APKINDEX.tar.gz"
  local ver
  ver="$(curl -fsSL --max-time 60 "$idx" | tar -xzO APKINDEX 2>/dev/null |
         awk -v a="$arch" '
           /^P:apk-tools$/ {name=1; next}
           /^P:/           {name=0}
           name && /^V:/   {v=$2}
           /^A:/ && v != "" {if ($2 == a) {print v; exit}}
         ')"
  [ -n "$ver" ] || return 0
  echo "$ver"
}

prepare_apk() {
  if usable "$TOOLS/apk" --version; then
    info "apk 已存在，跳过"
    return 0
  fi
  local arch; arch="$(apk_arch)"
  [ -n "$arch" ] || { warn "不支持的 CPU 架构 $(uname -m)，跳过 apk"; return 0; }
  command -v tar >/dev/null 2>&1 || die "需要 tar 解开 apk-tools 包"

  local ver idx tmp
  ver="$(latest_apktools_ver "$arch")"
  [ -n "$ver" ] || ver="2.14.4-r1"
  local base="https://dl-cdn.alpinelinux.org/alpine/latest/main/$arch"
  local url="$base/apk-tools-$ver.apk"

  info "尝试下载 apk-tools ${ver}（${arch}）"
  tmp="$(mktemp -d)"
  curl -fsSL --max-time 120 "$url" -o "$tmp/apkt.apk" \
    || { warn "下载失败：${url}（alpine CDN 版本可能已漂移）"
         warn "可手工放置静态 apk-static 到 $TOOLS/apk，或用 ipk 格式"; rm -rf "$tmp"; return 0; }

  ( cd "$tmp" && tar -xzf apkt.apk ./sbin/apk-static 2>/dev/null ) \
    || { warn "apk-tools 包内未找到 sbin/apk-static"; rm -rf "$tmp"; return 0; }
  [ -f "$tmp/sbin/apk-static" ] || { warn "解包后缺少 sbin/apk-static"; rm -rf "$tmp"; return 0; }

  mv "$tmp/sbin/apk-static" "$TOOLS/apk"
  chmod +x "$TOOLS/apk"
  rm -rf "$tmp"
  info "apk-static -> $TOOLS/apk"
}

prepare_po2lmo
prepare_apk
info "依赖准备完成"
