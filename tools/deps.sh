#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deps.sh — 为 build.py 准备两个外部二进制：
#   1) tools/po2lmo   : 把 po/ 里的 .po 编译成 LuCI 的 .lmo（Apache-2.0，openwrt/luci）
#   2) tools/apk      : apk-tools 3.0.0（优先 Linux 源码 meson 编译；兜底 alpine apk-static）
#                       版本固定 3.0.0，因为只有 3.x 的 mkpkg 才产出 APK v3 格式。
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

  local probe="$tmp/probe.c"
  cat > "$probe" <<'EOF'
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
int main(void) { lua_State *l = luaL_newstate(); luaL_openlibs(l); lua_close(l); return 0; }
EOF

  # 找一组真的能用的 lua 编译/链接参数。判定标准只有一条：能否编译并链接这个探针，
  # 不去猜头文件目录名与库名的对应关系（Ubuntu 的 /usr/include/lua5.4 对应 -llua5.4，
  # 而 Homebrew 的 .../include/lua 对应 -llua，两套命名规则完全不同）。
  local lua_flags="" lua_how=""
  local pc cf lf
  if command -v pkg-config >/dev/null 2>&1; then
    for pc in lua5.4 lua5.3 lua5.1 lua; do
      pkg-config --exists "$pc" 2>/dev/null || continue
      cf="$(pkg-config --cflags "$pc" 2>/dev/null)"
      lf="$(pkg-config --libs "$pc" 2>/dev/null)"
      # 故意不加引号：这里需要按空格拆成多个参数
      if cc -O2 -o "$tmp/probe.bin" "$probe" $cf $lf -lm >/dev/null 2>&1; then
        lua_flags="$cf $lf"; lua_how="pkg-config $pc"; break
      fi
    done
  fi

  if [ -z "$lua_flags" ]; then
    local hp; hp="$(command -v brew >/dev/null 2>&1 && brew --prefix 2>/dev/null || true)"
    local d bn libname libdir
    local cands=( /usr/include/lua5.4 /usr/include/lua5.3 /usr/include/lua5.1 /usr/include/lua
                  /usr/local/include )
    if [ -n "$hp" ]; then
      cands+=( $(ls -d "$hp"/Cellar/lua/*/include/lua "$hp"/Cellar/lua/*/include/lua5.* \
                      "$hp"/opt/lua/include/lua "$hp"/opt/lua/include/lua5.* 2>/dev/null) )
      cands+=( "$hp/include" )
    fi
    for d in "${cands[@]}"; do
      [ -f "$d/lua.h" ] || continue
      bn="$(basename "$d")"
      case "$bn" in
        lua5.*)  libname="$bn" ;;        # /usr/include/lua5.4 -> -llua5.4
        lua|include) libname="lua" ;;    # .../include/lua、/usr/local/include -> -llua
        *)       continue ;;
      esac
      for libdir in "$(dirname "$d")/lib" "$(dirname "$(dirname "$d")")/lib" "$hp/lib" /usr/lib; do
        [ -d "$libdir" ] || continue
        if cc -O2 -I"$d" -L"$libdir" -o "$tmp/probe.bin" "$probe" -l"$libname" -lm >/dev/null 2>&1; then
          lua_flags="-I$d -L$libdir -l$libname"; lua_how="$d"; break
        fi
      done
      [ -n "$lua_flags" ] && break
    done
  fi

  [ -n "$lua_flags" ] || die "找不到可用的 lua 开发库，请先安装：apt install liblua5.4-dev pkg-config / brew install lua"
  info "lua 开发库就绪（${lua_how}）"

  info "编译 po2lmo ..."
  ( cd "$tmp" && cc -O2 -I"$tmp/lib" -o po2lmo po2lmo.c sfh_hash.c $lua_flags -lm ) \
    || die "编译 po2lmo 失败（lua 版本不兼容？换 liblua5.1-0-dev 再试）"
  mv "$tmp/po2lmo" "$TOOLS/po2lmo"
  chmod +x "$TOOLS/po2lmo"
  rm -rf "$tmp"
  info "po2lmo -> $TOOLS/po2lmo"
}

# ---------------------------------------------------------------------------
# 2) apk（必须 3.x —— 只有 3.x 的 mkpkg 产出 APK v3 格式）
#    首选 alpine 的 apk-tools-static 3.x（自包含静态二进制），
#    兜底 Linux 上源码编译（须 -Ddefault_library=static，见下）。
# ---------------------------------------------------------------------------
apk_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo x86_64 ;;
    arm64|aarch64) echo aarch64 ;;
    *) echo "" ;;
  esac
}

# apk-tools 版本固定 —— 必须与本地产出 196-r17 时用的 3.0.0 一致，
# 否则 mkpkg 生成的包格式（APK v3）可能变。
APK_TOOLS_VER="3.0.0"

# 首选：从源码编译 apk-tools（Linux 上不需要任何补丁）
build_apk_from_source() {
  if [ "$(uname -s)" != "Linux" ]; then
    warn "非 Linux 平台不自动编译 apk-tools（macOS 需额外 3 个平台补丁）"
    return 1
  fi
  local missing=""
  command -v meson >/dev/null 2>&1 || missing="${missing} meson"
  command -v ninja >/dev/null 2>&1 || missing="${missing} ninja"
  [ -n "$missing" ] && { warn "缺少构建工具:${missing}（apt install meson ninja-build pkg-config zlib1g-dev libssl-dev）"; return 1; }

  local tmp; tmp="$(mktemp -d)"
  # GitHub 镜像优先：GitHub Actions runner 访问 gitlab.alpinelinux.org 会被挡（HTTP 418）
  local u got=0
  for u in "https://github.com/alpinelinux/apk-tools/archive/refs/tags/v${APK_TOOLS_VER}.tar.gz" \
           "https://gitlab.alpinelinux.org/alpine/apk-tools/-/archive/v${APK_TOOLS_VER}/apk-tools-v${APK_TOOLS_VER}.tar.gz"; do
    info "下载 apk-tools ${APK_TOOLS_VER} 源码（$(echo "$u" | cut -d/ -f3)）"
    if curl -fsSL -A "curl" --max-time 180 "$u" -o "$tmp/apk-tools.tar.gz"; then got=1; break; fi
    warn "下载失败：${u}"
  done
  [ "$got" = 1 ] || { rm -rf "$tmp"; return 1; }
  tar -xzf "$tmp/apk-tools.tar.gz" -C "$tmp" || { warn "解包失败"; rm -rf "$tmp"; return 1; }

  # GitHub 归档解出 apk-tools-3.0.0，GitLab 的是 apk-tools-v3.0.0 —— 用 glob 同时兜住
  local src; src="$(cd "$tmp" && ls -d apk-tools-*/ 2>/dev/null | head -1)"
  [ -n "$src" ] || { warn "解包后没找到 apk-tools-* 目录"; rm -rf "$tmp"; return 1; }
  src="$tmp/${src%/}"
  # tarball 偶尔会丢可执行位，而 meson 要 run_command('./get-version.sh')
  chmod +x "$src/get-version.sh" 2>/dev/null
  info "编译 apk-tools（meson + ninja，约 30 秒）..."
  # 必须 -Ddefault_library=static：否则 apk 会动态依赖 libapk.so.3.0.0，
  # 而那个 .so 只存在于构建目录，单拷 apk 二进制到别处会 "cannot open shared object file"
  if ! ( cd "$src" && meson setup build --prefix=/usr -Ddefault_library=static >/dev/null \
         && ninja -C build >/dev/null ) 2>"$tmp/build.log"; then
    warn "apk-tools 编译失败，日志尾部："; tail -5 "$tmp/build.log" >&2
    rm -rf "$tmp"; return 1
  fi

  local bin="$src/build/src/apk"
  [ -x "$bin" ] || { warn "没找到编译产物 $bin"; rm -rf "$tmp"; return 1; }
  if ldd "$bin" 2>/dev/null | grep -q libapk; then
    warn "编译出来的 apk 仍动态依赖 libapk，无法独立分发（-Ddefault_library=static 没生效）"
    rm -rf "$tmp"; return 1
  fi
  cp "$bin" "$TOOLS/apk" && chmod +x "$TOOLS/apk"
  rm -rf "$tmp"
  info "apk-tools ${APK_TOOLS_VER} -> $TOOLS/apk（自编译，静态链接 libapk）"
  return 0
}

# 解析某个 alpine 版本目录里的包版本号（latest 目录已 404，必须用 vX.Y / edge）
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

# 兜底：下载 alpine 的 apk-tools-static 静态二进制（静态链接，glibc 机器可直接跑）。
# 只接受 3.x —— 2.x 的 mkpkg 产出 APK v2，格式与我们要的不一样，宁可不要。
fetch_apk_static() {
  [ "$(uname -s)" = "Linux" ] || { warn "apk-static 是 Linux 静态二进制，本平台跳过"; return 1; }
  local arch; arch="$(apk_arch)"
  [ -n "$arch" ] || { warn "不支持的 CPU 架构 $(uname -m)"; return 1; }
  local branch="" ver="" tmp
  # 只有 edge / v3.23 起才有 apk-tools 3.x；v3.22 及以下仍是 2.14.x
  for branch in edge v3.23 v3.24; do
    ver="$(apk_index_ver "$branch" "$arch" apk-tools-static)"
    case "$ver" in 3.*) ;; *) ver="" ;; esac
    [ -n "$ver" ] && break
  done
  [ -n "$ver" ] || { warn "alpine 仓库里没找到 3.x 的 apk-tools-static"; return 1; }

  tmp="$(mktemp -d)"
  local url="https://dl-cdn.alpinelinux.org/alpine/${branch}/main/${arch}/apk-tools-static-${ver}.apk"
  info "下载 apk-tools-static ${ver}（${branch}/${arch}）"
  curl -fsSL --max-time 180 "$url" -o "$tmp/apkt.apk" \
    || { warn "下载失败：${url}"; rm -rf "$tmp"; return 1; }

  # .apk 是多段 gzip 拼接（签名段 + .PKGINFO 段 + 数据段）。GNU tar 读完第一段就
  # 认为归档结束、报"找不到成员"（macOS 的 bsdtar 会继续读，所以本机测不出来），
  # 这里改用 python tarfile 的 ignore_zeros 精确提取。
  if ! python3 - "$tmp/apkt.apk" "$TOOLS/apk" <<'PY'
import sys, tarfile
src, dst = sys.argv[1], sys.argv[2]
with tarfile.open(src, "r:gz", ignore_zeros=True) as tf:
    for m in tf.getmembers():
        if m.isfile() and m.name.lstrip("./").endswith("apk.static"):
            open(dst, "wb").write(tf.extractfile(m).read())
            sys.exit(0)
sys.exit(1)
PY
  then
    warn "apk-tools-static 里没提取到 sbin/apk.static"; rm -rf "$tmp"; return 1
  fi
  chmod +x "$TOOLS/apk"
  rm -rf "$tmp"
  warn "用的是 Alpine apk-tools-static ${ver}（3.x，同样产出 APK v3）"
  info "apk-static -> $TOOLS/apk"
  return 0
}

prepare_apk() {
  if usable "$TOOLS/apk" --version; then
    info "apk 已存在，跳过"
    return 0
  fi
  # 首选 alpine 现成的静态二进制：自包含、无需编译工具、每次结果一致
  fetch_apk_static && return 0
  # 兜底：本机编译（需要 meson/ninja + openssl/zlib 开发库）
  build_apk_from_source && return 0
  warn "拿不到 apk 工具，本次只会产出 .ipk（要 apk 请装 meson/ninja 或手工放 $TOOLS/apk）"
  return 0
}

prepare_po2lmo
prepare_apk
info "依赖准备完成"
