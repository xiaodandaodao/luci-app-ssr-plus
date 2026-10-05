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

# apk-tools 版本固定 —— 必须与本地产出 196-r17 时用的 3.0.0 一致，
# 否则 mkpkg 生成的包格式（APK v3）会变。
APK_TOOLS_VER="3.0.0"

# 首选：从源码编译 apk-tools（Linux 上不需要任何补丁）
build_apk_from_source() {
  if [ "$(uname -s)" != "Linux" ]; then
    warn "非 Linux 平台不自动编译 apk-tools（macOS 需额外 3 个平台补丁）"
    return 1
  fi
  local missing=""
  command -v meson >/dev/null 2>&1 || missing="$missing meson"
  command -v ninja >/dev/null 2>&1 || missing="$missing ninja"
  [ -n "$missing" ] && { warn "缺少构建工具:$missing（apt install meson ninja-build libssl-dev zlib1g-dev）"; return 1; }

  local tmp url
  tmp="$(mktemp -d)"
  url="https://gitlab.alpinelinux.org/alpine/apk-tools/-/archive/v${APK_TOOLS_VER}/apk-tools-v${APK_TOOLS_VER}.tar.gz"
  info "下载 apk-tools ${APK_TOOLS_VER} 源码"
  curl -fsSL --max-time 180 "$url" -o "$tmp/apk-tools.tar.gz" \
    || { warn "下载失败：${url}"; rm -rf "$tmp"; return 1; }
  tar -xzf "$tmp/apk-tools.tar.gz" -C "$tmp" || { warn "解包失败"; rm -rf "$tmp"; return 1; }

  local src="$tmp/apk-tools-v${APK_TOOLS_VER}"
  info "编译 apk-tools（meson + ninja，约 30 秒）..."
  if ! ( cd "$src" && meson setup build --prefix=/usr >/dev/null && ninja -C build >/dev/null ) 2>"$tmp/build.log"; then
    warn "apk-tools 编译失败，日志尾部："; tail -5 "$tmp/build.log" >&2
    rm -rf "$tmp"; return 1
  fi

  local bin="$src/build/src/apk"
  [ -x "$bin" ] || { warn "没找到编译产物 $bin"; rm -rf "$tmp"; return 1; }
  cp "$bin" "$TOOLS/apk" && chmod +x "$TOOLS/apk"
  rm -rf "$tmp"
  info "apk-tools ${APK_TOOLS_VER} -> $TOOLS/apk（自编译，APK v3）"
  return 0
}

# 解析某个 alpine 版本目录里的 apk-tools 版本号（注意：latest 目录已不稳定，
# 必须用 vX.Y 具体版本目录）
apk_index_ver() {  # $1=branch $2=arch [$3=包名，默认 apk-tools]
  python3 - "$1" "$2" "${3:-apk-tools}" <<'PY'
import io, sys, tarfile, urllib.request
branch, arch, want = sys.argv[1], sys.argv[2], sys.argv[3]
url = "https://dl-cdn.alpinelinux.org/alpine/%s/main/%s/APKINDEX.tar.gz" % (branch, arch)
try:
    raw = urllib.request.urlopen(url, timeout=45).read()
    txt = tarfile.open(fileobj=io.BytesIO(raw)).extractfile("APKINDEX").read().decode("utf-8", "replace")
except Exception:
    sys.exit(0)
name = None
for line in txt.splitlines():
    if line.startswith("P:"):
        name = line[2:]
    elif line.startswith("V:") and name == want:
        print(line[2:]); break
PY
}

# 兜底：alpine 的 apk-static（apk-tools 2.x → 产出 APK v2 格式，仅在没有编译条件时用）
fetch_apk_static() {
  [ "$(uname -s)" = "Linux" ] || { warn "apk-static 是 Linux 静态二进制，本平台跳过"; return 1; }
  local arch; arch="$(apk_arch)"
  [ -n "$arch" ] || { warn "不支持的 CPU 架构 $(uname -m)"; return 1; }
  local branch="" ver="" tmp
  for branch in v3.22 v3.21 v3.20 v3.19; do
    ver="$(apk_index_ver "$branch" "$arch" apk-tools-static)"
    [ -n "$ver" ] && break
  done
  [ -n "$ver" ] || { warn "alpine 仓库里没解析到 apk-tools-static 版本"; return 1; }

  tmp="$(mktemp -d)"
  local url="https://dl-cdn.alpinelinux.org/alpine/${branch}/main/${arch}/apk-tools-static-${ver}.apk"
  info "尝试下载 apk-tools-static ${ver}（${branch}/${arch}）"
  curl -fsSL --max-time 180 "$url" -o "$tmp/apkt.apk" \
    || { warn "下载失败：${url}"; rm -rf "$tmp"; return 1; }
  ( cd "$tmp" && tar -xzf apkt.apk ./sbin/apk.static ) 2>/dev/null \
    || { warn "apk-tools-static 包内未找到 sbin/apk.static"; rm -rf "$tmp"; return 1; }
  mv "$tmp/sbin/apk.static" "$TOOLS/apk" && chmod +x "$TOOLS/apk"
  rm -rf "$tmp"
  warn "用的是 Alpine apk-tools ${ver}（2.x，产出 APK v2 格式），与本地 3.0.0 产物不一定字节一致"
  info "apk-static -> $TOOLS/apk"
  return 0
}

prepare_apk() {
  if usable "$TOOLS/apk" --version; then
    info "apk 已存在，跳过"
    return 0
  fi
  build_apk_from_source && return 0
  fetch_apk_static && return 0
  warn "拿不到 apk 工具，本次只会产出 .ipk（要 apk 请装 meson/ninja 或手工放 $TOOLS/apk）"
  return 0
}

prepare_po2lmo
prepare_apk
info "依赖准备完成"
