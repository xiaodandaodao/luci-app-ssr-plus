#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inject_pkg.py —— 把本项目对 luci-app-ssr-plus 的改动「注入」到别人已经打好的
ipk / apk 里，从而在一个**没有源码**的预编译包上获得这些功能（智能分组 + 自定义分组）。

为什么需要它：源码补丁（patches/*.patch）只能用于能拿到源码的情况。上游或第三方
发布的预编译包（尤其闭源的 fork）拿不到源码，但 luci-app-ssr-plus 是 arch=all 的
纯脚本包 —— 包里的内容就是最终文件本身。所以只要把改动过的文件替换进去再重打包，
效果与从源码编译完全一致。

子命令
------
  make-payload   从（已打过补丁的）源码树生成 payload/ —— 之后就不再需要源码
  show           只打印「补丁里的仓库路径 → 包内安装路径」映射，便于核对
  apply          把 payload 注入到目标 ipk/apk，输出新包

示例
----
  # 1) 生成 payload（一次性）
  python3 tools/inject_pkg.py make-payload \\
      --src  /path/to/helloworld \\
      --patch patches/ssrplus-smart-grouping-196-r17.patch \\
      --out   payload/

  # 2) 看看会改哪些文件
  python3 tools/inject_pkg.py show --patch patches/ssrplus-smart-grouping-196-r17.patch

  # 3) 注入
  python3 tools/inject_pkg.py apply --payload payload/ \\
      --pkg  luci-app-ssr-plus_196-r20_all.ipk \\
      --out  out/luci-app-ssr-plus_196-r20+sg.ipk

设计取舍
--------
* **不动 conffiles**。本项目改的文件里没有一个是 conffile（真正的默认配置在
  /usr/share/shadowsocksr/shadowsocksr.config，不是 /etc/config/shadowsocksr）。
  这样注入包不会覆盖用户的配置，也不会破坏 conffiles_static 的校验值。
* **保留原包元数据**。Package / Depends / conffiles / 安装脚本一律沿用目标包，
  只重算 Installed-Size；版本号默认原样保留（可用 --version-suffix 追加标记）。
* 新的 uci 选项在代码里都有默认值，老配置下不会报错 —— 所以注入后功能默认关闭，
  需要在 LuCI 面板里打开开关（与正常升级的行为一致，因为 /etc/config 是 conffile）。
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # ssrplus-local-build/
sys.path.insert(0, ROOT)
try:
    import build as B                             # noqa: E402  复用打包实现
except ImportError:                               # 技能包里改名成了 build_luci_pkg.py
    import build_luci_pkg as B                    # noqa: E402

PKG = B.PKG                                        # luci-app-ssr-plus
I18N_PKG = "luci-i18n-ssr-plus-zh-cn"
PAYLOAD_FORMAT = 1


# ------------------------------------------------------------------ 小工具

def log(msg):
    print(msg, flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def untar_to(tf, dest):
    """安全解包：自己实现，避开 tarfile 的目录创建路径（也不接受绝对/越界路径）。"""
    for m in tf.getmembers():
        name = m.name.lstrip("./")
        if not name:
            continue
        target = os.path.join(dest, name)
        real = os.path.realpath(target)
        if not real.startswith(os.path.realpath(dest) + os.sep) and real != os.path.realpath(dest):
            raise ValueError("包内路径越界: %s" % m.name)
        if m.isdir():
            os.makedirs(target, exist_ok=True)
            os.chmod(target, stat.S_IMODE(m.mode))
        elif m.issym() or m.islnk():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target) or os.path.islink(target):
                os.remove(target)
            os.symlink(m.linkname, target)
        elif m.isfile():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(target, stat.S_IMODE(m.mode))
            os.utime(target, (m.mtime, m.mtime))


def tree_modes(root):
    """收集整棵树的权限（打包 ipk 时需要）。"""
    modes = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            modes[rel] = stat.S_IMODE(os.stat(p).st_mode)
    return modes


def tree_max_mtime(root):
    newest = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            newest = max(newest, int(os.stat(os.path.join(dirpath, f)).st_mtime))
        newest = max(newest, int(os.stat(dirpath).st_mtime))
    return newest


def max_mtime_of_outer(ipk):
    with open(ipk, "rb") as f:
        outer = tarfile.open(fileobj=f, mode="r:gz")
        return max(int(m.mtime) for m in outer.getmembers() if m.mtime)


# ------------------------------------------------------------------ 路径映射

def map_install(repo_rel):
    """仓库相对路径 → 包内安装路径。返回 None 表示不由 luci.mk 直接安装。"""
    p = repo_rel.replace("\\", "/")
    if not p.startswith(PKG + "/"):
        return None
    p = p[len(PKG) + 1:]
    for prefix, dst in (("luasrc/", "usr/lib/lua/luci/"),
                        ("htdocs/", "www/"),
                        ("root/", "")):
        if p.startswith(prefix):
            return dst + p[len(prefix):]
    return None


def classify(repo_rel):
    """返回 ('app'|'i18n'|'none', install_path|None)。"""
    install = map_install(repo_rel)
    if install:
        return "app", install
    if repo_rel.replace("\\", "/") == "%s/po/zh_Hans/ssr-plus.po" % PKG:
        return "i18n", "usr/lib/lua/luci/i18n/ssr-plus.zh-cn.lmo"
    return "none", None


def parse_patch(patch_path):
    """从统一 diff 里取出改动涉及的仓库相对路径。
    返回 [(路径, 是否新增文件)]，按出现顺序、去重。"""
    txt = open(patch_path, encoding="utf-8", errors="replace").read()
    seen, out = set(), []
    for blk in re.split(r"(?m)^(?=diff --git )", txt):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", blk)
        if not m:
            continue
        p = m.group(2)
        if p in seen:
            continue
        seen.add(p)
        out.append((p, "new file mode" in blk.split("\n@@", 1)[0]))
    if not out:
        raise SystemExit("补丁里没有 diff --git 段，确认是 git diff 生成的统一 diff")
    return out


# ------------------------------------------------------------------ payload

def cmd_show(args):
    rows = []
    for rel, is_new in parse_patch(args.patch):
        kind, install = classify(rel)
        rows.append((rel, kind + ("(新)" if is_new and kind != "none" else ""), install or "-"))
    w = max(len(r[0]) for r in rows)
    log("%-*s  %-8s  %s" % (w, "仓库路径", "去向", "包内路径"))
    log("-" * (w + 34))
    for rel, kind, install in rows:
        log("%-*s  %-8s  %s" % (w, rel, kind, install))
    app = sum(1 for r in rows if r[1].startswith("app"))
    i18n = sum(1 for r in rows if r[1].startswith("i18n"))
    log("-" * (w + 30))
    log("入主包 %d 个文件；入语言包 %d 个；其余 %d 个仅参与构建（如 Makefile）。"
        % (app, i18n, len(rows) - app - i18n))


def cmd_make_payload(args):
    src = os.path.abspath(args.src)
    src_pkg = os.path.join(src, PKG)
    if not os.path.isdir(src_pkg):
        raise SystemExit("找不到 %s" % src_pkg)

    meta = B.parse_makefile(os.path.join(src_pkg, "Makefile"), PKG)
    git_modes = B.git_file_modes(src, PKG)

    tmp = tempfile.mkdtemp(prefix="payload-stage-")
    manifest_files = []
    try:
        # 用 build.py 的 stage_tree 生成带正确权限的安装树，保证与正式包一致
        stage = os.path.join(tmp, "stage")
        B.stage_tree(src_pkg, stage, PKG, meta["raw_version"], git_modes)
        mtime = int(B.sh(["git", "log", "-1", "--format=%ct", "--", PKG],
                         cwd=src, check=False) or 0) or int(os.stat(src_pkg).st_mtime)

        out = os.path.abspath(args.out)
        if os.path.exists(out):
            shutil.rmtree(out)
        os.makedirs(out)

        for rel, is_new in parse_patch(args.patch):
            kind, install = classify(rel)
            if kind == "none":
                log("跳过（仅参与构建）: %s" % rel)
                continue
            if kind == "app":
                srch = os.path.join(stage, install)
                if not os.path.isfile(srch):
                    raise SystemExit("暂存树里没有 %s —— 源码树可能没打补丁" % install)
                common = "app"
            else:
                # po -> lmo
                po = os.path.join(src, rel)
                lmo_tmp = os.path.join(tmp, "ssr-plus.zh-cn.lmo")
                B.sh([B.PO2LMO_BIN, po, lmo_tmp])
                srch = lmo_tmp
                common = "i18n"

            dst = os.path.join(out, common, install)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(srch, dst)
            mode = stat.S_IMODE(os.stat(srch).st_mode)
            os.chmod(dst, mode)
            manifest_files.append({
                "target": common,
                "install": install,
                "source": rel,
                "new": is_new,
                "mode": "0%o" % mode,
                "sha256": sha256_file(dst),
            })
            log("payload + %-6s %s%s" % (common, install, "（本项目新增）" if is_new else ""))

        manifest = {
            "format": PAYLOAD_FORMAT,
            "generated_from_version": meta["version"],
            "generated_from_commit": (B.sh(["git", "rev-parse", "--short", "HEAD"],
                                           cwd=src, check=False) or "").strip(),
            "patch": os.path.basename(args.patch),
            "packages": {"app": PKG, "i18n": I18N_PKG},
            "mtime": mtime,
            "files": manifest_files,
        }
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        log("")
        log("payload 已生成: %s（%d 个文件，基线版本 %s）"
            % (out, len(manifest_files), meta["version"]))
        log("接下来就不再需要源码了：apply 时只用到这个目录。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 注入：ipk

def read_ipk(ipk):
    with open(ipk, "rb") as f:
        outer = tarfile.open(fileobj=f, mode="r:gz")
        names = outer.getnames()
        assert "./debian-binary" in names, names
        ctl_gz = outer.extractfile("./control.tar.gz").read()
        data_gz = outer.extractfile("./data.tar.gz").read()
    ctl = tarfile.open(fileobj=io.BytesIO(ctl_gz), mode="r:gz")
    data = tarfile.open(fileobj=io.BytesIO(data_gz), mode="r:gz")
    return ctl, data


def parse_control(txt):
    """按顺序保留 control 字段，便于原样回写。"""
    fields, cur = [], None
    for line in txt.splitlines():
        if line[:1] in (" ", "\t") and cur is not None:
            fields[cur][1].append(line)
        elif ":" in line:
            k, v = line.split(":", 1)
            cur = len(fields)
            fields.append([k, [v.lstrip()]])
        else:
            pass
    return fields


def render_control(fields):
    out = []
    for k, val in fields:
        out.append("%s:%s" % (k, val[0] if val[0] else ""))
        out.extend(val[1:])
    return "\n".join(out) + "\n"


def pkg_identity_ipk(fields):
    d = {}
    for k, val in fields:
        d[k] = val[0]
    return d.get("Package", ""), d.get("Version", "")


def cmd_apply_ipk(args, pkg, payload):
    out, dry = args.out, args.dry_run
    ctl, data = read_ipk(pkg)
    ctl_members = {m.name.lstrip("./"): m for m in ctl.getmembers()}
    if "control" not in ctl_members:
        raise SystemExit("目标 ipk 的 control.tar.gz 里没有 control 文件")
    control_txt = ctl.extractfile(ctl_members["control"]).read().decode("utf-8")
    fields = parse_control(control_txt)
    name, version = pkg_identity_ipk(fields)
    log("目标包: %s 版本 %s" % (name, version))

    manifest = load_payload(payload)
    target = which_target(name, manifest)
    jobs = payload_files(manifest, target)
    if not jobs:
        raise SystemExit("payload 里没有适配 %s 的文件" % name)

    mtime = max_mtime_of_outer(pkg)
    tmp = tempfile.mkdtemp(prefix="inj-")
    try:
        root = os.path.join(tmp, "data")
        os.makedirs(root)
        untar_to(data, root)

        missing, replaced, added = [], [], []
        for job in jobs:
            dst = os.path.join(root, job["install"])
            if os.path.exists(dst):
                replaced.append(job["install"])
            elif job.get("new"):
                added.append(job["install"])
            else:
                missing.append(job["install"])
            if not dry:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(payload, job["_rel"]), dst)
                os.chmod(dst, int(job["mode"], 8))

        for p in replaced:
            log("  替换 %s" % p)
        for p in added:
            log("  新增 %s（本项目新增文件）" % p)
        for p in missing:
            log("  新增 %s（⚠️ 原包中不存在，且本项目并非新增）" % p)
        if missing:
            log("  ⚠️ 有 %d 个文件在原包里应当存在却找不到：目标包的上游版本可能与本项目基线"
                "不同，注入后建议装机实测。" % len(missing))
        if dry:
            log("(dry-run，未写出文件)")
            return

        B.set_tree_meta(root, tree_modes(root), mtime)

        # control：沿用原字段，只重算 Installed-Size / 可选改版本
        new_version = make_version("ipk", version, args.version_suffix, args.revision)
        if new_version != version:
            log("  版本号 %s → %s" % (version, new_version))
        nfiles, total = B.tree_stats(root)
        installed = ((total + 1023) // 1024) * 1024
        ctl_dir = os.path.join(tmp, "control")
        os.makedirs(ctl_dir)
        new_fields = []
        seen_installed = False
        for k, val in fields:
            if k == "Version":
                new_fields.append([k, [new_version]] + val[1:])
            elif k == "Installed-Size":
                seen_installed = True
                new_fields.append([k, [str(installed)]])
            else:
                new_fields.append([k, val])
        if not seen_installed:
            new_fields.append(["Installed-Size", [str(installed)]])
        open(os.path.join(ctl_dir, "control"), "w", encoding="utf-8").write(
            render_control(new_fields))

        # 其余 control 成员（conffiles / postinst / ...）原样搬运
        for nm, m in ctl_members.items():
            if nm in ("control", ""):
                continue
            p = os.path.join(ctl_dir, nm)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            src = ctl.extractfile(m)
            data_bytes = src.read() if src else b""
            with open(p, "wb") as fh:
                fh.write(data_bytes)
            os.chmod(p, stat.S_IMODE(m.mode))

        B.set_tree_meta(ctl_dir, tree_modes(ctl_dir), mtime)

        data_tar = os.path.join(tmp, "data.tar.gz")
        B.make_tar_gz(data_tar, root, "", mtime, tree_modes(root))
        control_tar = os.path.join(tmp, "control.tar.gz")
        B.make_tar_gz(control_tar, ctl_dir, "", mtime, tree_modes(ctl_dir))

        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
        with open(out, "wb") as raw:
            gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0)
            tf = tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT)
            for nm, path in (("debian-binary", None),
                             ("data.tar.gz", data_tar),
                             ("control.tar.gz", control_tar)):
                if path is None:
                    payload_bytes = b"2.0\n"
                    ti = tarfile.TarInfo("./" + nm)
                    ti.size = len(payload_bytes)
                    ti.mtime = mtime
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mode = 0o644
                    tf.addfile(ti, io.BytesIO(payload_bytes))
                else:
                    ti = tf.gettarinfo(path, arcname="./" + nm)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mtime = mtime
                    ti.mode = 0o644
                    with open(path, "rb") as fh:
                        tf.addfile(ti, fh)
            tf.close()
            gz.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    verify_ipk_out(out, jobs, new_version)
    log("")
    log("已写出: %s（%d 字节，版本 %s）" % (out, os.path.getsize(out), new_version))
    log("sha256 : %s" % sha256_file(out))


def verify_ipk_out(out, jobs, version):
    ctl, data = read_ipk(out)
    names = {m.name.lstrip("./") for m in ctl.getmembers()}
    assert "control" in names, names
    ctrl = ctl.extractfile([m for m in ctl.getmembers()
                            if m.name.lstrip("./") == "control"][0]).read().decode()
    got_ver = re.search(r"^Version:\s*(.+)$", ctrl, re.M).group(1).strip()
    assert got_ver == version, (got_ver, version)

    tmp = tempfile.mkdtemp(prefix="vinj-")
    try:
        untar_to(data, tmp)
        bad = []
        for job in jobs:
            p = os.path.join(tmp, job["install"])
            if not os.path.isfile(p):
                bad.append("缺失 " + job["install"])
            elif sha256_file(p) != job["sha256"]:
                bad.append("内容不符 " + job["install"])
        if bad:
            raise SystemExit("注入校验失败: %s" % bad)
        log("回环校验: 注入的 %d 个文件全部就位且 sha256 匹配；control 版本=%s"
            % (len(jobs), got_ver))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 注入：apk

APK_INFO_KEYS = {"name", "version", "hashes", "description", "arch", "license",
                 "origin", "maintainer", "url", "installed-size", "depends", "provides"}


def adbdump_info(apk):
    """把 apk adbdump 的输出解析成字典。列表字段（depends/provides）用
    `# N items` 作为起始标记，元素是缩进的 `- xxx` 行。"""
    out = B.sh([B.APK_BIN, "adbdump", apk], check=False)
    if not out or "info:" not in out:
        raise SystemExit("adbdump 失败，无法读取 apk 元数据")
    info, cur_list, in_desc = {}, None, False
    for line in out.splitlines():
        m = re.match(r"^  ([a-z][a-z-]*):(?:[ \t](.*))?$", line)
        if m and m.group(1) in APK_INFO_KEYS:
            k, v = m.group(1), (m.group(2) or "").strip()
            in_desc = False
            if k in ("depends", "provides") and v.startswith("#"):
                cur_list = k
                info[k] = []
            elif v in ("|", ">"):
                cur_list = None
                info[k] = ""
                in_desc = (k == "description")
            else:
                cur_list = None
                info[k] = v
            continue
        if m and m.group(1) not in APK_INFO_KEYS:
            in_desc = False
            continue
        # 列表项固定缩进 4 空格；2 空格的 `paths:` 段不能混进来
        mm = re.match(r"^ {4}- (.*)$", line)
        if mm and cur_list:
            info[cur_list].append(mm.group(1).strip())
        elif line and not line.startswith(" "):
            cur_list = None            # 回到顶层，列表结束
        elif in_desc and line.strip():
            info["description"] = (info.get("description", "") + " " + line.strip()).strip()
    return info


def bump_revision(version, rev):
    if re.search(r"-r\d+$", version):
        return re.sub(r"-r\d+$", "-r%d" % rev, version)
    return "%s-r%d" % (version, rev)


def make_version(fmt, version, suffix, revision):
    """版本号策略。

    ipk：opkg 对版本串几乎不挑，suffix 直接拼在末尾（如 196-r13+sg1）。
    apk：实测只接受 `<数字>[-r<整数>]`（`196a-r13`、`196-r14`），
         `+`/`~`/`.sg` 一律被拒 —— 所以只支持「单字母后缀」或「改 r 号」。
    """
    if revision is not None:
        return bump_revision(version, revision)
    if not suffix:
        return version
    if fmt == "ipk":
        return version + suffix
    if re.fullmatch(r"[A-Za-z]", suffix):
        rev = re.search(r"-r\d+$", version)
        base = version[:rev.start()] if rev else version
        return base + suffix + (rev.group(0) if rev else "")
    raise SystemExit(
        "apk 版本号不接受 %r。apk 只允许 `<数字>[-r<整数>]` 形式，请改用：\n"
        "  --revision <N>          把 -r 号改成 N（如 196-r13 → 196-r%d）\n"
        "  --version-suffix <字母>  单个字母后缀（如 a → 196a-r13）\n"
        "  或不加参数，直接沿用原版本号（推荐：opkg/apk 会原地覆盖安装）" % (suffix, 14))


def cmd_apply_apk(args, pkg, payload):
    out, dry = args.out, args.dry_run
    info = adbdump_info(pkg)
    name = info.get("name", "")
    version = info.get("version", "")
    log("目标包: %s 版本 %s（arch=%s）" % (name, version, info.get("arch", "?")))

    manifest = load_payload(payload)
    target = which_target(name, manifest)
    jobs = payload_files(manifest, target)
    if not jobs:
        raise SystemExit("payload 里没有适配 %s 的文件" % name)

    tmp = tempfile.mkdtemp(prefix="inja-")
    try:
        root = os.path.join(tmp, "data")
        os.makedirs(root)
        r = subprocess.run([B.APK_BIN, "--allow-untrusted", "extract",
                            "--destination", root, pkg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("apk extract 失败: %s" % (r.stderr or r.stdout))

        # 我们自己解出来的目录里没有脚本，脚本由 build.py 的标准模板重建
        missing, replaced, added = [], [], []
        for job in jobs:
            dst = os.path.join(root, job["install"])
            if os.path.exists(dst):
                replaced.append(job["install"])
            elif job.get("new"):
                added.append(job["install"])
            else:
                missing.append(job["install"])
            if not dry:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(payload, job["_rel"]), dst)
                os.chmod(dst, int(job["mode"], 8))
        for p in replaced:
            log("  替换 %s" % p)
        for p in added:
            log("  新增 %s（本项目新增文件）" % p)
        for p in missing:
            log("  新增 %s（⚠️ 原包中不存在，且本项目并非新增）" % p)
        if missing:
            log("  ⚠️ 有 %d 个文件在原包里应当存在却找不到：目标包的上游版本可能与本项目基线"
                "不同，注入后建议装机实测。" % len(missing))
        if dry:
            log("(dry-run，未写出文件)")
            return

        # 若替换了某个 conffile 的默认内容，同步刷新 conffiles_static 的 hash
        refresh_conffiles_static(root, name, jobs)

        mtime = tree_max_mtime(root)
        B.set_tree_meta(root, tree_modes(root), mtime)

        new_version = make_version("apk", version, args.version_suffix, args.revision)
        if new_version != version:
            log("  版本号 %s → %s" % (version, new_version))
        depends = info.get("depends") or []
        provides = info.get("provides") or []
        log("  依赖 %d 项，provides %d 项（沿用原包）" % (len(depends), len(provides)))
        saved = (B.PKG, B.LICENSE, B.ORIGIN, B.MAINTAINER, B.URL)
        B.PKG, B.LICENSE = name, info.get("license", B.LICENSE)
        B.ORIGIN = info.get("origin", B.ORIGIN)
        B.MAINTAINER = info.get("maintainer", B.MAINTAINER)
        B.URL = info.get("url", B.URL)
        try:
            B.build_apk(out, root, mtime, new_version, depends, pkgname=name,
                        arch=info.get("arch", "noarch"),
                        description=info.get("description", "").strip(),
                        scripts=True, provides=provides)
        finally:
            (B.PKG, B.LICENSE, B.ORIGIN, B.MAINTAINER, B.URL) = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    verify_apk_out(out, jobs, new_version)
    log("")
    log("已写出: %s（%d 字节，版本 %s）" % (out, os.path.getsize(out), new_version))
    log("sha256 : %s" % sha256_file(out))


def refresh_conffiles_static(root, name, jobs):
    d = os.path.join(root, "lib/apk/packages")
    cf = os.path.join(d, "%s.conffiles" % name)
    st = os.path.join(d, "%s.conffiles_static" % name)
    if not os.path.isfile(cf) or not os.path.isfile(st):
        return
    listed = {c.strip() for c in open(cf, encoding="utf-8").read().splitlines() if c.strip()}
    touched = [j["install"] for j in jobs if "/" + j["install"] in listed]
    if not touched:
        return
    lines = []
    for line in open(st, encoding="utf-8").read().splitlines():
        if not line.strip():
            continue
        path = line.split()[0]
        p = os.path.join(root, path.lstrip("/"))
        if path in listed and os.path.isfile(p):
            lines.append("%s %s" % (path, sha256_file(p)))
        else:
            lines.append(line)
    open(st, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    log("  刷新 conffiles_static: %s" % ", ".join(touched))


def verify_apk_out(out, jobs, version):
    info = adbdump_info(out)
    if info.get("version") != version:
        raise SystemExit("apk 版本校验失败: %s != %s" % (info.get("version"), version))
    tmp = tempfile.mkdtemp(prefix="vinja-")
    try:
        r = subprocess.run([B.APK_BIN, "--allow-untrusted", "extract",
                            "--destination", tmp, out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("apk 回环解包失败: %s" % (r.stderr or r.stdout))
        bad = []
        for job in jobs:
            p = os.path.join(tmp, job["install"])
            if not os.path.isfile(p):
                bad.append("缺失 " + job["install"])
            elif sha256_file(p) != job["sha256"]:
                bad.append("内容不符 " + job["install"])
        if bad:
            raise SystemExit("注入校验失败: %s" % bad)
        log("回环校验: 注入的 %d 个文件全部就位且 sha256 匹配；版本=%s" % (len(jobs), version))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 公共

def load_payload(dirpath):
    mf = os.path.join(dirpath, "manifest.json")
    if not os.path.isfile(mf):
        raise SystemExit("%s 里没有 manifest.json" % dirpath)
    manifest = json.load(open(mf, encoding="utf-8"))
    if manifest.get("format") != PAYLOAD_FORMAT:
        raise SystemExit("payload 格式版本不支持: %r" % manifest.get("format"))
    return manifest


def which_target(pkgname, manifest):
    want = manifest["packages"]
    if pkgname == want["app"]:
        return "app"
    if pkgname == want["i18n"]:
        return "i18n"
    raise SystemExit("这个 payload 只适配 %s / %s，不认识 %s"
                     % (want["app"], want["i18n"], pkgname))


def payload_files(manifest, target):
    return [dict(f, _rel=os.path.join(target, f["install"]))
            for f in manifest["files"] if f["target"] == target]


def cmd_apply(args):
    pkg = os.path.abspath(args.pkg)
    if not os.path.isfile(pkg):
        raise SystemExit("找不到 %s" % pkg)

    head = open(pkg, "rb").read(4)
    if pkg.endswith(".ipk"):
        fmt = "ipk"
    elif pkg.endswith(".apk"):
        fmt = "apk"
    else:
        # 兜底：ipk 是 gzip（1f 8b），apk v3 以 ADB 魔数开头
        fmt = "ipk" if head[:2] == b"\x1f\x8b" else "apk"
    log("识别格式: %s" % fmt)

    if fmt == "ipk":
        cmd_apply_ipk(args, pkg, args.payload)
    else:
        cmd_apply_apk(args, pkg, args.payload)


def main():
    ap = argparse.ArgumentParser(
        description="把本项目的 SS/Clash 面板改动注入到已有的 luci-app-ssr-plus ipk/apk",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("make-payload", help="从源码树生成 payload/")
    p1.add_argument("--src", required=True, help="helloworld 仓库根目录（已打补丁）")
    p1.add_argument("--patch", required=True, help="用于确定文件清单的统一 diff")
    p1.add_argument("--out", default=os.path.join(ROOT, "payload"), help="payload 输出目录")
    p1.set_defaults(func=cmd_make_payload)

    p2 = sub.add_parser("show", help="打印路径映射")
    p2.add_argument("--patch", required=True)
    p2.set_defaults(func=cmd_show)

    p3 = sub.add_parser("apply", help="注入到目标包")
    p3.add_argument("--payload", default=os.path.join(ROOT, "payload"))
    p3.add_argument("--pkg", required=True, help="目标 ipk 或 apk")
    p3.add_argument("--out", required=True, help="输出包路径")
    p3.add_argument("--version-suffix", default="",
                    help="改版本号：ipk 直接追加（如 +sg1）；apk 只能是单个字母（如 a → 196a-r13）")
    p3.add_argument("--revision", type=int, default=None,
                    help="把 -r 号改成指定值（ipk/apk 都适用），如 --revision 1013 → 196-r1013")
    p3.add_argument("--dry-run", action="store_true", help="只显示会改哪些文件")
    p3.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
