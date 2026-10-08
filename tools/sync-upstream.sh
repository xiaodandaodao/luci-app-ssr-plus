#!/usr/bin/env bash
#
# sync-upstream.sh —— 上游 fw876/helloworld 更新后，把 luci-app-ssr-plus 的变化并进本仓库。
#
# 为什么不用 git merge / subtree：
#   本仓库的「根目录」== 上游仓库里的 luci-app-ssr-plus/ 子目录（再叠加我们的改造）。
#   两边历史无关，目录层级还差一层，直接 merge 会炸成一片冲突。
#   所以改用最可控的做法：
#     1) 取「上游 基线tag → 新tag」对 luci-app-ssr-plus/ 的差分
#     2) 用 git apply -p2 剥掉 luci-app-ssr-plus/ 这一层，落到本仓库根
#     3) 和我们改过的文件走三方合并，只有真正同行冲突才停下
#
# 版本号策略（PKG_RELEASE）—— 与上游 tag 一一对应，所以冲突时永远取我们的：
#   <上游 release><两位本方修订序>：上游 r19 的第 1 版 = 1901，第 2 版 = 1902；
#   上游升到 r20 则重新从 2001 起。必须是纯数字（apk 的 "-r" 后只接受整数）。
#
# 用法：
#   bash tools/sync-upstream.sh --status          查看当前基线与已拉取的上游 tag
#   bash tools/sync-upstream.sh v196.20           同步到上游 v196.20
#   bash tools/sync-upstream.sh --regen-patch v196.20
#                                                 冲突解决并提交后，重生成补丁 + 推进基线
#
set -euo pipefail

PKG_SUBDIR="luci-app-ssr-plus"           # 本包在上游仓库里的路径
PKG_PATHS=(Makefile luasrc root po)      # 本仓库内属于「包本体」的路径（补丁只含这些）
BASE_FILE=".upstream-base"               # 记录我们当前对齐的上游 tag
DEFAULT_BASE="v196.19"
UPSTREAM_REMOTE="upstream"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/fw876/helloworld.git}"
NS="refs/upstream-tags"                  # 上游 tag 存这里，不与本仓库自己的 v* 混
PATCH_OUT="patches/ssrplus-local-changes.patch"

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=; G=; Y=; R=; D=; N=
fi

info() { printf '%s\n' "${B}${G}==>${N} $*"; }
warn() { printf '%s\n' "${B}${Y}警告:${N} $*" >&2; }
die()  { printf '%s\n' "${B}${R}错误:${N} $*" >&2; exit 1; }
dim()  { printf '%s\n' "${D}$*${N}"; }

cd "$(git rev-parse --show-toplevel)"

read_base() {
  if [ -f "$BASE_FILE" ]; then
    tr -d '[:space:]' < "$BASE_FILE"
  else
    printf '%s' "$DEFAULT_BASE"
  fi
}

ensure_upstream() {
  if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
    info "添加上游 remote（只读）：${UPSTREAM_REPO}"
    git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_REPO"
  fi
  # 把 push 地址废掉，避免手滑往上游推东西
  git remote set-url --push "$UPSTREAM_REMOTE" DISABLED_readonly
}

fetch_upstream() {
  info "拉取上游 tag（存进 ${NS}/，不与本仓库的 tag 混）"
  if ! git fetch --no-tags "$UPSTREAM_REMOTE" "+refs/tags/*:${NS}/*" >/dev/null 2>&1; then
    warn "拉取上游失败（离线？）—— 继续用本地已有的 ${NS}/ 记录"
  fi
}

# 只挡「已跟踪文件有未提交改动」；未跟踪文件（临时产物等）不拦
require_clean() {
  local dirty
  dirty="$(git status --porcelain --untracked-files=no)"
  if [ -n "$dirty" ]; then
    die "有未提交的改动，先 commit 或 stash 再来：
$dirty"
  fi
}

cmd_status() {
  local base; base="$(read_base)"
  printf '%s\n' "${B}当前基线${N}  ${base}   ${D}(来自 ${BASE_FILE})${N}"
  printf '%s\n' "${B}已拉取的上游 tag${N}"
  git for-each-ref --sort=v:refname --format='  %(refname:short)' "$NS/" \
    | sed "s|  ${NS}/|  |"
}

cmd_sync() {
  local new="$1"
  require_clean
  ensure_upstream
  fetch_upstream

  local base; base="$(read_base)"
  local bref="${NS}/${base}" nref="${NS}/${new}"

  git rev-parse --verify --quiet "$nref" >/dev/null \
    || die "本地没有上游 ${new}（先跑 --status 看有哪些，或检查网络）"

  if [ "$base" = "$new" ]; then
    info "基线已经是 ${new}，上游无新变化"
    return 0
  fi
  git rev-parse --verify --quiet "${bref}:${PKG_SUBDIR}" >/dev/null \
    || die "基线 ${base} 下找不到 ${PKG_SUBDIR}/ 目录"

  info "差分：上游 ${base} → ${new}"
  local tmp; tmp="$(mktemp -d)"
  local patch="${tmp}/upstream-${base}-to-${new}.patch"
  git diff "$bref" "$nref" -- "${PKG_SUBDIR}/" > "$patch"

  local files
  files="$(git diff --name-only "$bref" "$nref" -- "${PKG_SUBDIR}/" | sed "s|^${PKG_SUBDIR}/||")"
  if [ -z "$files" ]; then
    info "上游 ${base} → ${new} 对本包没有改动，无需同步"
    return 0
  fi

  # 我们改过哪些包内文件（相对基线）
  local ours; ours="$(git diff --name-only "$(git rev-parse "${bref}:${PKG_SUBDIR}")" HEAD -- "${PKG_PATHS[@]}")"

  printf '%s\n' "${B}上游改了 ${N}$(printf '%s\n' "$files" | wc -l | tr -d ' ')${B} 个文件：${N}"
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if printf '%s\n' "$ours" | grep -qxF "$f"; then
      printf '  %s  %s\n' "$f" "${Y}⚠ 我们也改过 → 可能冲突${N}"
    else
      printf '  %s\n' "$f"
    fi
  done <<< "$files"

  info "应用上游差分（-p2，剥掉 ${PKG_SUBDIR}/ 这一层）"
  if git apply --check -p2 "$patch" 2>/dev/null; then
    git apply -p2 "$patch"
    info "全部干净应用 ✅"
  else
    dim "  有重叠，改用三方合并（--3way）"
    git apply -p2 --3way "$patch" || true
  fi

  local conflicts
  conflicts="$(git diff --name-only --diff-filter=U)"
  if [ -n "$conflicts" ]; then
    printf '\n%s\n' "${B}${R}需要你人工裁决：${N}"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      printf '  %s\n' "$f"
    done <<< "$conflicts"
    printf '\n%s\n' "${B}裁决原则${N}"
    cat <<'TIPS'
  · 上游的 bug 修复 / 新功能        → 采纳上游
  · 我们的改造（见 ATTRIBUTION.md §2）→ 保留我们
  · Makefile 的 PKG_RELEASE          → 永远用我们的。规则 <上游 release><两位本方修订序>
                                       （上游 r14 第 1 版 = 1401；升到 r15 → 1501）
TIPS
    printf '\n%s\n' "${B}下一步${N}"
    cat <<TIPS
  1. 编辑上面这些文件，解决冲突标记 <<<<<<< ======= >>>>>>>
  2. git add <解决后的文件>
  3. bash tools/verify.sh
  4. python3 tools/build.py --format all --out dist
  5. git commit -m "chore: 同步上游 ${new}"
  6. bash tools/sync-upstream.sh --regen-patch ${new}
TIPS
  else
    printf '\n%s\n' "${B}${G}全部合并成功，无冲突。${N}"
    printf '%s\n' "${B}下一步${N}"
    cat <<TIPS
  1. bash tools/verify.sh                     # 静态校验
  2. python3 tools/build.py --format all --out dist
  3. git commit -am "chore: 同步上游 ${new}"
  4. bash tools/sync-upstream.sh --regen-patch ${new}
TIPS
  fi
}

cmd_regen() {
  local new="$1"
  require_clean
  local nref="${NS}/${new}"
  git rev-parse --verify --quiet "${nref}:${PKG_SUBDIR}" >/dev/null \
    || die "找不到 ${new}:${PKG_SUBDIR}（先跑 --status / 确认是上游 tag）"

  local tree
  tree="$(git rev-parse "${nref}:${PKG_SUBDIR}")"
  mkdir -p "$(dirname "$PATCH_OUT")"
  git diff "$tree" HEAD -- "${PKG_PATHS[@]}" > "$PATCH_OUT"

  local n; n="$(grep -c '^diff --git' "$PATCH_OUT" || true)"
  info "补丁已重写：${PATCH_OUT}（相对上游 ${new}，${n} 个文件）"
  git diff --stat "$tree" HEAD -- "${PKG_PATHS[@]}"

  printf '%s\n' "$new" > "$BASE_FILE"
  info "基线推进为 ${new}（写入 ${BASE_FILE}）"
  dim "  提示：补丁路径固定为 ${PATCH_OUT}，不再随版本改名。"
  dim "  别忘了同步刷新注入载荷：python3 tools/inject_pkg.py make-payload"
}

case "${1:---help}" in
  --status|-s)   cmd_status ;;
  --regen-patch) [ $# -ge 2 ] || die "用法：bash tools/sync-upstream.sh --regen-patch <上游tag>"
                 cmd_regen "$2" ;;
  --help|-h|help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
  -*)            die "未知参数 $1" ;;
  *)             cmd_sync "$1" ;;
esac
