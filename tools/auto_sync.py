#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_sync.py —— 上游 fw876/helloworld 自动跟踪（CI 专用，全程非交互）

和 tools/sync-upstream.sh 的分工：

  · sync-upstream.sh ：给人用。交互式，冲突时停下来打提示让你裁决。
  · auto_sync.py     ：给 GitHub Actions 用。不问问题，结果全靠退出码 + JSON 表达。

四个子命令对应工作流里的四个阶段：

  check                     上游有没有比当前基线更新的 tag？有就顺带算出下一个 PKG_RELEASE
  sync <tag>                把「基线 → <tag>」对本包的差分落到工作树；冲突就原地停下并报清单
  finalize <tag> <release>  推进版本号与基线、重生成补丁与注入载荷、刷新文档里的版本串
  conflicts                 打印当前未解决的冲突文件（工作流拿去写进 PR / issue）

退出码：
  0  正常
  2  有冲突，需要人工裁决
  3  上游对本包没有实质改动（不必发版）
  4  tag 或版本号异常（比基线还旧、算出来的 release 不单调等）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/fw876/helloworld.git")
UPSTREAM_API = os.environ.get("UPSTREAM_API", "fw876/helloworld")
PKG_SUBDIR = "luci-app-ssr-plus"        # 本包在上游 monorepo 里的路径
PKG_PATHS = ["Makefile", "luasrc", "root", "po"]   # 属于「包本体」的路径，补丁只含这些
BASE_FILE = ".upstream-base"
DEFAULT_BASE = "v196.19"
NS = "refs/upstream-tags"               # 上游 tag 存这里，不与本仓库自己的 v* 混
PATCH_OUT = "patches/ssrplus-local-changes.patch"
PAYLOAD_OUT = "payload"
CONFLICT_LIST = "conflicts.txt"
DOC_FILES = ["README.md", "ATTRIBUTION.md", "patches/README.md",
             "tools/build_preview.py", "tools/sync-upstream.sh"]

EXIT_OK, EXIT_CONFLICT, EXIT_NOCHANGE, EXIT_BADTAG = 0, 2, 3, 4

TAG_RE = re.compile(r"^v(\d+)(?:\.(\d+))?$")


class SyncError(Exception):
    pass


# -------------------------------------------------------------------- 基础工具

def sh(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd or str(ROOT), text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise SyncError("命令失败（%d）：%s\n%s" % (p.returncode, " ".join(cmd), p.stderr.strip()))
    return p


def log(msg=""):
    print(msg, flush=True)


def summary(msg):
    """写进 GitHub 的 job summary（本地跑时没有这个环境变量，直接打到 stdout）。"""
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    else:
        print(msg, flush=True)


def tag_key(tag):
    """v196.19 → (196, 19)，不是这个形态的返回 None。"""
    m = TAG_RE.match(tag or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


def read_base():
    f = ROOT / BASE_FILE
    return f.read_text(encoding="utf-8").strip() if f.exists() else DEFAULT_BASE


def read_release():
    txt = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"(?m)^\s*PKG_RELEASE\s*:?=\s*(\d+)\s*$", txt)
    if not m:
        raise SyncError("Makefile 里找不到 PKG_RELEASE")
    return m.group(1)


def upstream_tags():
    """列出上游所有形如 v<num>[.<num>] 的 tag，按版本号升序。"""
    tags = set()
    p = sh(["git", "ls-remote", "--tags", UPSTREAM_REPO], check=False)
    if p.returncode == 0:
        for line in p.stdout.splitlines():
            if line.endswith("^{}"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2:
                tags.add(parts[1].rsplit("/", 1)[-1])
    if not tags:
        # 兜底：git 协议被墙/被代理拦时，走 GitHub API
        p2 = sh(["gh", "api", "--paginate", "repos/%s/tags" % UPSTREAM_API,
                 "--jq", ".[].name"], check=False)
        if p2.returncode == 0:
            tags = {t for t in p2.stdout.split() if t}
    return sorted((t for t in tags if tag_key(t)), key=tag_key)


def ensure_ref(tag):
    """把上游某个 tag 取到 refs/upstream-tags/ 下，返回可用的 ref 名。

    每次都强制更新（refspec 前面的 +）：上游偶尔会重打 tag，只判断「本地有没有这个 ref」
    会一直用着第一次拉到的旧对象，差分出来的内容就是错的。
    """
    ref = "%s/%s" % (NS, tag)
    p = sh(["git", "fetch", "--no-tags", "--depth", "1", UPSTREAM_REPO,
            "+refs/tags/%s:%s" % (tag, ref)], check=False)
    if p.returncode == 0:
        return ref
    # 网络不通时，退回本地已有的那份（宁可用旧的，也别让整条流水线挂掉）
    if sh(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
          check=False).returncode == 0:
        log("[warn] 拉不到上游 %s，改用本地缓存的那份（可能不是最新的）" % tag)
        return ref
    raise SyncError("拉取上游 tag %s 失败：%s" % (tag, p.stderr.strip()))


def upstream_changed(base, new):
    """上游 base → new 之间，本包有哪些文件变了（返回包内相对路径）。"""
    b, n = ensure_ref(base), ensure_ref(new)
    p = sh(["git", "diff", "--name-only", b, n, "--", PKG_SUBDIR + "/"])
    return [x[len(PKG_SUBDIR) + 1:] for x in p.stdout.split() if x]


def our_changes(base):
    """相对上游基线，我们改过哪些包内文件。"""
    b = ensure_ref(base)
    tree = sh(["git", "rev-parse", "%s:%s" % (b, PKG_SUBDIR)]).stdout.strip()
    p = sh(["git", "diff", "--name-only", tree, "HEAD", "--"] + PKG_PATHS, check=False)
    return [x for x in p.stdout.split() if x]


def require_clean():
    dirty = sh(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    if dirty:
        raise SyncError("工作区有未提交的改动，先处理干净：\n%s" % dirty)


def unmerged_files():
    p = sh(["git", "diff", "--name-only", "--diff-filter=U"], check=False)
    return [x for x in p.stdout.split() if x]


# ----------------------------------------------------------------------- check

def cmd_check(args):
    base = read_base()
    cur_release = read_release()
    bkey = tag_key(base)
    if bkey is None:
        raise SyncError("基线 %r 不是 v<num>[.<num>] 形态，检查 %s" % (base, BASE_FILE))

    if args.upstream_tag:
        new = args.upstream_tag
        if tag_key(new) is None:
            raise SyncError("指定的上游 tag %r 形态不对" % new)
    else:
        newer = [t for t in upstream_tags() if tag_key(t) > bkey]
        if not newer:
            result = {"has_update": False, "base_tag": base, "reason": "上游没有比基线更新的 tag"}
            emit_check(result, args)
            return EXIT_NOCHANGE
        new = newer[-1]

    nkey = tag_key(new)
    if nkey <= bkey:
        result = {"has_update": False, "base_tag": base, "new_tag": new,
                  "reason": "上游 tag %s 不比基线 %s 新" % (new, base)}
        emit_check(result, args)
        return EXIT_NOCHANGE

    changed = upstream_changed(base, new)
    if not changed:
        result = {"has_update": False, "base_tag": base, "new_tag": new,
                  "reason": "上游 %s → %s 没动到 %s/，本包无变化" % (base, new, PKG_SUBDIR)}
        emit_check(result, args)
        return EXIT_NOCHANGE

    # 版本号：上游 v196.20 → 2001（<上游 release><两位本方修订序>）
    cand = nkey[1] * 100 + 1
    if cand <= int(cur_release):
        raise SyncError("算出来的 PKG_RELEASE %d 不大于当前 %s —— 换 tag 或先手工推进版本"
                        % (cand, cur_release))
    new_release = str(cand)

    ours = our_changes(base)
    overlap = sorted(set(changed) & set(ours))

    result = {
        "has_update": True,
        "base_tag": base,
        "new_tag": new,
        "current_release": cur_release,
        "new_release": new_release,
        "pkg_version": "196",
        "tag_name": "v196-r%s" % new_release,
        "changed_files": changed,
        "our_files": sorted(ours),
        "overlap_files": overlap,
        # 只是风险预判，真正的结论要等 sync 跑完才知道
        "risk": "conflict-likely" if overlap else "clean",
    }
    emit_check(result, args)
    return EXIT_OK


def emit_check(result, args):
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return
    if not result["has_update"]:
        log("无需同步：%s" % result["reason"])
        summary("## 上游跟踪\n\n无需同步：%s" % result["reason"])
        return
    log("上游有新版本：%s → %s" % (result["base_tag"], result["new_tag"]))
    log("本包改动 %d 个文件；其中我们也可能改过的：%s"
        % (len(result["changed_files"]), ", ".join(result["overlap_files"]) or "无"))
    log("PKG_RELEASE：%s → %s（tag %s）"
        % (result["current_release"], result["new_release"], result["tag_name"]))


# ------------------------------------------------------------------------ sync

def cmd_sync(args):
    tag = args.tag
    require_clean()
    base = read_base()
    bkey, nkey = tag_key(base), tag_key(tag)
    if bkey is None or nkey is None:
        raise SyncError("基线 %r 或目标 tag %r 形态不对" % (base, tag))
    if nkey <= bkey:
        return EXIT_BADTAG

    changed = upstream_changed(base, tag)
    if not changed:
        log("上游 %s → %s 没动到 %s/，无需同步" % (base, tag, PKG_SUBDIR))
        return EXIT_NOCHANGE

    log("上游 %s → %s，本包 %d 个文件有变化：" % (base, tag, len(changed)))
    for f in changed:
        log("  - %s" % f)

    b, n = "%s/%s" % (NS, base), "%s/%s" % (NS, tag)
    tmp = tempfile.mkdtemp(prefix="upstream-diff-")
    patch = os.path.join(tmp, "upstream-%s-to-%s.patch" % (base, tag))
    with open(patch, "w", encoding="utf-8") as fh:
        fh.write(sh(["git", "diff", b, n, "--", PKG_SUBDIR + "/"]).stdout)

    # 先试干净应用；有重叠就退到三方合并
    if sh(["git", "apply", "--check", "-p2", patch], check=False).returncode == 0:
        sh(["git", "apply", "-p2", patch])
        log("差分干净应用 ✅")
    else:
        log("有重叠，改用三方合并（git apply -p2 --3way）")
        sh(["git", "apply", "-p2", "--3way", patch], check=False)

    for junk in ("orig", "rej"):
        for f in (ROOT).rglob("*.%s" % junk):
            f.unlink()

    conflicts = unmerged_files()
    if conflicts:
        (ROOT / CONFLICT_LIST).write_text("\n".join(conflicts) + "\n", encoding="utf-8")
        log("")
        log("以下文件存在冲突，需要人工裁决：")
        for f in conflicts:
            log("  ✗ %s" % f)
        log("")
        log("清单已写入 %s" % CONFLICT_LIST)
        return EXIT_CONFLICT

    log("")
    log("全部合并成功，无冲突 ✅")
    return EXIT_OK


def cmd_conflicts(args):
    files = unmerged_files()
    if (ROOT / CONFLICT_LIST).exists() and not files:
        files = [x for x in (ROOT / CONFLICT_LIST).read_text(encoding="utf-8").split() if x]
    if args.json:
        print(json.dumps({"conflicts": files}, ensure_ascii=False, indent=2))
    else:
        for f in files:
            print(f)
    return EXIT_OK


# -------------------------------------------------------------------- finalize

def write_release(release):
    p = ROOT / "Makefile"
    txt = p.read_text(encoding="utf-8")
    new, cnt = re.subn(r"(?m)^(\s*PKG_RELEASE\s*:?=\s*)\d+(\s*)$",
                       lambda m: m.group(1) + release + m.group(2), txt, count=1)
    if cnt != 1:
        raise SyncError("Makefile 里没找到唯一的 PKG_RELEASE 赋值行")
    p.write_text(new, encoding="utf-8")
    log("Makefile: PKG_RELEASE → %s" % release)


def write_base(tag):
    (ROOT / BASE_FILE).write_text(tag + "\n", encoding="utf-8")
    log("%s → %s" % (BASE_FILE, tag))


def regen_patch(tag):
    """重生成补丁。

    拿工作树（而不是 HEAD）和上游基线比：工作流的干净路径会先 commit 再调 finalize，
    两者等价；但人工在 PR 分支上裁决冲突时往往还没 commit，用 HEAD 会把刚合并进来的
    上游内容整个漏掉。
    """
    ref = "%s/%s" % (NS, tag)
    tree = sh(["git", "rev-parse", "%s:%s" % (ref, PKG_SUBDIR)]).stdout.strip()
    p = ROOT / PATCH_OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(sh(["git", "diff", tree, "--"] + PKG_PATHS).stdout)
    n = len(re.findall(r"(?m)^diff --git ", p.read_text(encoding="utf-8")))
    log("%s 已重生成（相对上游 %s，%d 个文件）" % (PATCH_OUT, tag, n))


TABLE_MARKER = "| 情况 | 值 |"
TABLE_PLACEHOLDER = "@@VERSION_TABLE@@"


def split_table(text):
    """把 README 的版本对照表整块抠出来，原位留占位符。

    不这么做的话，全文把 v196.19 换成 v196.20 会把表格里的历史行一起改写掉
    （「上游 v196.19 的第 1 版 = 1901」变成「v196.20 的第 1 版 = 1901」，事实就错了）。
    """
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == TABLE_MARKER), None)
    if start is None:
        return text, None
    end = start
    while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("|"):
        end += 1
    table = lines[start:end + 1]
    return "\n".join(lines[:start] + [TABLE_PLACEHOLDER] + lines[end + 1:]), table


def bump_table(table, new_base, new_release):
    """表格内部只做三件事：摘掉旧的「当前」标记、删掉已成真的预告行、补一行当前版本。"""
    out = []
    for ln in table:
        if ln.strip() == TABLE_MARKER or re.match(r"^\|\s*-{2,}", ln):
            out.append(ln)
            continue
        # 「上游升到 v196.20 后的第 1 版 | 2001」这种预告行，现在成真了，交给下面补的当前行
        if re.search(r"后的第\s*\d+\s*版", ln):
            continue
        if "（当前" in ln:
            ln = re.sub(r"（当前[^）]*）", "", ln)
        out.append(ln)
    out.append("| 上游 `%s` 的第 1 版（当前，自动同步） | `%s` |" % (new_base, new_release))
    return "\n".join(out)


def refresh_docs(old_base, new_base, old_release, new_release):
    """把散落在文档/脚本里的基线 tag 与版本串跟着推进。

    只动「数字型事实」：基线 tag、包版本串、PKG_RELEASE 数字。
    README 的版本对照表是历史记录，单独走 split_table / bump_table，不参与全文替换。
    """
    old_pkg = "196-r%s" % old_release
    new_pkg = "196-r%s" % new_release
    touched = []
    for rel in DOC_FILES:
        f = ROOT / rel
        if not f.exists():
            continue
        orig = f.read_text(encoding="utf-8")
        txt = orig

        table = None
        if rel == "README.md":
            txt, table = split_table(txt)

        txt = txt.replace(old_base, new_base)
        txt = txt.replace(old_pkg, new_pkg)
        if rel == "patches/README.md":
            txt = txt.replace("`%s`" % old_release, "`%s`" % new_release)

        if table is not None:
            txt = txt.replace(TABLE_PLACEHOLDER, bump_table(table, new_base, new_release))

        if txt != orig:
            f.write_text(txt, encoding="utf-8")
            touched.append(rel)
    log("文档版本串已刷新：%s" % (", ".join(touched) or "无变化"))
    return touched


def regen_payload():
    """重生成注入载荷。

    inject_pkg.py 要一棵「helloworld 风格」的源码树（根下有 luci-app-ssr-plus/，
    且它本身是个 git 仓库，因为要 git ls-files 读文件模式）。本仓库根 == 包根，
    所以这里临时拼一棵出来就行，不必完整 clone 上游。
    """
    po2lmo = ROOT / "tools" / "po2lmo"
    if not (po2lmo.exists() and os.access(str(po2lmo), os.X_OK)):
        log("[warn] 没有 tools/po2lmo，跳过载荷重生成（先跑 bash tools/deps.sh）")
        return False

    tmp = Path(tempfile.mkdtemp(prefix="payload-src-"))
    src = tmp / "helloworld"
    pkg = src / PKG_SUBDIR
    pkg.mkdir(parents=True)
    for item in PKG_PATHS:
        s, d = ROOT / item, pkg / item
        if s.is_dir():
            shutil.copytree(s, d)
        elif s.exists():
            shutil.copy2(s, d)

    sh(["git", "init", "-q"], cwd=str(src))
    sh(["git", "add", "-A"], cwd=str(src))
    sh(["git", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci",
        "commit", "-qm", "base"], cwd=str(src))

    out = ROOT / PAYLOAD_OUT
    sh([sys.executable, "tools/inject_pkg.py", "make-payload",
        "--src", str(src), "--patch", PATCH_OUT, "--out", str(out)])
    shutil.rmtree(tmp, ignore_errors=True)

    n = sum(1 for f in out.rglob("*") if f.is_file())
    log("%s/ 已重生成（%d 个文件）" % (PAYLOAD_OUT, n))
    return True


def cmd_finalize(args):
    tag, release = args.tag, args.release
    old_base, old_release = read_base(), read_release()

    if tag_key(tag) is None:
        raise SyncError("tag %r 形态不对" % tag)
    if not re.fullmatch(r"\d+", release):
        raise SyncError("PKG_RELEASE 必须是纯数字：%r" % release)
    if int(release) <= int(old_release):
        raise SyncError("新 PKG_RELEASE %s 必须大于当前 %s" % (release, old_release))

    write_release(release)
    write_base(tag)
    regen_patch(tag)
    refresh_docs(old_base, tag, old_release, release)
    if not args.no_payload:
        regen_payload()

    log("")
    log("finalize 完成：基线 %s → %s，PKG_RELEASE %s → %s"
        % (old_base, tag, old_release, release))
    return EXIT_OK


# ------------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="上游自动跟踪（CI 专用，非交互）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="检测上游有没有更新的 tag")
    p.add_argument("--upstream-tag", default="", help="手动指定上游 tag（留空=自动取最新）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("sync", help="应用上游差分；冲突时退出码 2")
    p.add_argument("tag")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("finalize", help="推进版本号、重生成补丁与载荷、刷新文档")
    p.add_argument("tag")
    p.add_argument("release")
    p.add_argument("--no-payload", action="store_true", help="跳过载荷重生成")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("conflicts", help="打印未解决的冲突文件")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_conflicts)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except SyncError as e:
        print("[fail] %s" % e, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
