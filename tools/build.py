#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 macOS 上本地把 helloworld 的 luci-app-ssr-plus（arch=all，纯 Lua/Shell，无需交叉编译器）
打包成 OpenWrt 的 .ipk（opkg）和 .apk（apk-tools v3）。

使用方法：
    ./build.py                       # 同时产出 ipk 与 apk（含中文语言包）
    ./build.py --format ipk
    ./build.py --enable INCLUDE_Mihomo,INCLUDE_ChinaDNS_NG
    ./build.py --src /path/to/helloworld --out ./out

说明：
  * 只适用于 PKGARCH=all / 无 C/Go/Rust 源码的包（luci-app-ssr-plus、luci-i18n-*）。
    xray-core / mihomo / naiveproxy 这类需要交叉编译的包，本机编不了，必须走
    OpenWrt SDK（Linux x86_64）或 GitHub Actions。
  * 打包行为对齐 OpenWrt buildroot：luci.mk 的安装规则 + package.mk 的 control/脚本模板。
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))      # 本文件位于 <repo>/tools
TOOLS = HERE
APK_BIN = os.path.join(TOOLS, "apk")
PO2LMO_BIN = os.path.join(TOOLS, "po2lmo")

PKG = "luci-app-ssr-plus"

# 与上游 CI (.github/workflows/release-packages.yml 里的 .config) 完全一致的配置，
# 决定 LUCI_DEPENDS 里哪些条件依赖会被写进包的 Depends。
DEFAULT_CONFIG = {
    "INCLUDE_NONE_V2RAY": "y",
    "INCLUDE_Xray": "n",
    "INCLUDE_Shadowsocks_NONE_Client": "y",
    "INCLUDE_Shadowsocks_NONE_Server": "y",
    "INCLUDE_ChinaDNS_NG": "n",
    "INCLUDE_DNS2TCP": "n",
    "INCLUDE_MosDNS": "n",
    "INCLUDE_Http_Proxy": "n",
    "INCLUDE_Mihomo": "n",
    "INCLUDE_GeoData": "n",
    "INCLUDE_Shadow_TLS": "n",
    "INCLUDE_Kcptun": "n",
    "INCLUDE_NaiveProxy": "n",
    "INCLUDE_Shadowsocks_Rust_Client": "n",
    "INCLUDE_Shadowsocks_Rust_Server": "n",
    "INCLUDE_Shadowsocks_Simple_Obfs": "n",
    "INCLUDE_Shadowsocks_V2ray_Plugin": "n",
    "INCLUDE_ShadowsocksR_Libev_Client": "n",
    "INCLUDE_ShadowsocksR_Libev_Server": "n",
}

MAINTAINER = "OpenWrt LuCI community"
URL = "https://github.com/openwrt/luci"
LICENSE = "GPL-3.0-only"
SECTION = "luci"
ORIGIN = "feeds/helloworld/" + PKG


# ---------------------------------------------------------------- 小工具

def sh(cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(cmd), p.stderr.strip()))
    return p.stdout.strip()


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- Makefile 解析

def parse_makefile(path, pkg):
    src = open(path, encoding="utf-8").read()

    def one(key, default=None):
        m = re.findall(r"^%s\s*:?=\s*(.+?)\s*$" % re.escape(key), src, re.M)
        return m[-1].strip() if m else default

    version = one("PKG_VERSION")
    release = one("PKG_RELEASE", "1")
    title = one("LUCI_TITLE")
    pkgarch = one("LUCI_PKGARCH", "all")

    # LUCI_DEPENDS:= \ 续行块
    m = re.search(r"^LUCI_DEPENDS\s*:?=\s*((?:.*\\\n)*.*)$", src, re.M)
    deps_raw = ""
    if m:
        deps_raw = m.group(1).replace("\\\n", " ")

    depends = []           # [(cond_or_None, pkg)]
    for tok in deps_raw.split():
        if not tok.startswith("+"):
            continue
        tok = tok[1:]
        if ":" in tok:
            cond, name = tok.split(":", 1)
            depends.append((cond.replace("$(PKG_NAME)", pkg), name))
        else:
            depends.append((None, tok))

    # conffiles
    conffiles = []
    mc = re.search(r"define Package/\$\(PKG_NAME\)/conffiles\n(.*?)\nendef", src, re.S)
    if mc:
        conffiles = [l.strip() for l in mc.group(1).splitlines() if l.strip()]

    return {
        "version": "%s-r%s" % (version, release),
        "raw_version": version,
        "title": title,
        "pkgarch": pkgarch,
        "depends": depends,
        "conffiles": conffiles,
    }


def compute_depends(meta, config, pkg):
    """按配置求出最终 Depends 列表（OpenWrt 会额外加 libc，luci.mk 会加 luci-lua-runtime）。"""
    deps = ["libc"]
    for cond, name in meta["depends"]:
        if cond is None:
            deps.append(name)
        else:
            key = cond if cond.startswith("CONFIG_") else "CONFIG_" + cond
            if config.get(key, "n") == "y":
                deps.append(name)
    deps.append("luci-lua-runtime")  # luci.mk: 有 luasrc 目录就加
    # 去重并保持稳定顺序（OpenWrt 输出是排好序的）
    seen, out = set(), []
    for d in sorted(set(deps)):
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------------------------------------------------------------- 文件收集

def git_file_modes(repo, subdir):
    """返回 {相对仓库根的路径: 0o644/0o755}，模拟 OpenWrt 的 cp -pR（保留 git 的可执行位）。"""
    try:
        out = sh(["git", "ls-files", "-s", subdir], cwd=repo, check=False)
    except Exception:
        return {}
    modes = {}
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        meta, path = parts[0].split(), parts[1]
        if meta[0] == "100755":
            modes[path] = 0o755
        elif meta[0] == "100644":
            modes[path] = 0o644
    return modes


def substitute_version(root, version):
    """luci.mk 的 SubstituteVersion：*.htm 里的 PKG_VERSION 占位符。"""
    pat1 = re.compile(r"<%#\s*([^ ]*)PKG_VERSION\s*%>")
    pat2 = re.compile(r'"(<=\s*(?:media|resource)\s*%>[^"]*\.(?:js|css))"')
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".htm"):
                continue
            p = os.path.join(dirpath, f)
            try:
                s = open(p, encoding="utf-8").read()
            except UnicodeDecodeError:
                continue
            new = pat1.sub(lambda m: (m.group(1) or "") + version, s)
            new = pat2.sub(lambda m: '"%s?v=%s"' % (m.group(1), version), new)
            if new != s:
                open(p, "w", encoding="utf-8").write(new)


def stage_tree(src_pkg, dest, pkg, version, git_modes):
    """按 luci.mk / Package/install 规则生成待打包的文件树，返回 {相对路径: 权限}。"""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    modes = {}

    def copy_tree(srcdir, dstdir, src_prefix, mode_map_key):
        if not os.path.isdir(srcdir):
            return
        os.makedirs(dstdir, exist_ok=True)
        for dirpath, dirnames, filenames in os.walk(srcdir):
            rel_dir = os.path.relpath(dirpath, srcdir)
            rel_dir = "" if rel_dir == "." else rel_dir
            out_dir = os.path.join(dstdir, rel_dir) if rel_dir else dstdir
            os.makedirs(out_dir, exist_ok=True)
            for f in sorted(filenames):
                s = os.path.join(dirpath, f)
                rel = os.path.join(rel_dir, f) if rel_dir else f
                d = os.path.join(out_dir, f)
                if os.path.islink(s):
                    linkto = os.readlink(s)
                    if os.path.exists(d) or os.path.islink(d):
                        os.remove(d)
                    os.symlink(linkto, d)
                    continue
                shutil.copy2(s, d)
                key = "%s/%s/%s" % (pkg, src_prefix, rel.replace(os.sep, "/"))
                modes[rel.replace(os.sep, "/")] = git_modes.get(key, 0o644)

    # luasrc/ -> /usr/lib/lua/luci/
    luasrc_modes = {}
    if os.path.isdir(os.path.join(src_pkg, "luasrc")):
        copy_tree(os.path.join(src_pkg, "luasrc"),
                  os.path.join(dest, "usr/lib/lua/luci"), "luasrc", None)
        luasrc_modes = dict(modes)
        modes.clear()
        luasrc_modes = {"usr/lib/lua/luci/" + k: v for k, v in luasrc_modes.items()}

    # root/ -> /
    root_modes = {}
    if os.path.isdir(os.path.join(src_pkg, "root")):
        copy_tree(os.path.join(src_pkg, "root"), dest, "root", None)
        root_modes = dict(modes)
        modes.clear()

    all_modes = {}
    all_modes.update(root_modes)
    all_modes.update(luasrc_modes)

    # 删 *.luadoc
    for dirpath, _, files in os.walk(os.path.join(dest, "usr/lib/lua/luci")):
        for f in files:
            if f.endswith(".luadoc"):
                os.remove(os.path.join(dirpath, f))

    substitute_version(dest, version)

    # Makefile 里显式 chmod 0755 的文件
    forced = [
        "usr/bin/ssr-monitor", "usr/bin/ssr-rules", "usr/bin/ssr-switch",
        "etc/hotplug.d/iface/99-ssrplus-pppoe",
    ]
    for f in forced:
        if os.path.exists(os.path.join(dest, f)):
            os.chmod(os.path.join(dest, f), 0o755)
            all_modes[f] = 0o755

    return all_modes


def set_tree_meta(dest, modes, mtime):
    """统一 uid/gid=0(root)、mtime、权限；清掉 macOS 扩展属性。"""
    for dirpath, dirnames, filenames in os.walk(dest):
        rel_dir = os.path.relpath(dirpath, dest)
        rel_dir = "" if rel_dir == "." else rel_dir
        os.chmod(dirpath, 0o755)
        os.utime(dirpath, (mtime, mtime))
        for f in filenames:
            p = os.path.join(dirpath, f)
            rel = os.path.join(rel_dir, f) if rel_dir else f
            rel = rel.replace(os.sep, "/")
            if os.path.islink(p):
                continue
            os.chmod(p, modes.get(rel, 0o644))
            os.utime(p, (mtime, mtime))
    # 清掉 macOS 扩展属性（仅 macOS 有 xattr，Linux 上跳过；否则会 FileNotFoundError）
    if sys.platform == "darwin" and shutil.which("xattr"):
        subprocess.run(["xattr", "-cr", dest], capture_output=True)


def tree_stats(dest):
    files, total = 0, 0
    for dirpath, _, filenames in os.walk(dest):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                files += 1
                continue
            files += 1
            total += os.path.getsize(p)
    return files, total


# ---------------------------------------------------------------- ipk 打包

def resolve_conffiles_ipk(stage, declared):
    """复刻 scripts/ipkg-build 的 conffiles 解析：
    每条声明（去掉前导 /，相对 stage）若指向目录，则 find -type f 展开为其下所有普通文件；
    目录/软链本身不会进入 conffiles——opkg 安装前会按 conffiles 逐条备份文件，
    目录条目会导致 `copy_file: omitting directory` 且安装直接失败（255）。
    返回带前导 / 的绝对路径，LC_ALL=C 排序。"""
    out = set()

    def add_file(fp):
        if os.path.isfile(fp) and not os.path.islink(fp):
            r = os.path.relpath(fp, stage).replace(os.sep, "/")
            out.add("/" + r)

    for cf in declared:
        rel = cf.strip().lstrip("/")
        if not rel:
            continue
        p = os.path.join(stage, rel)
        if os.path.isdir(p) and not os.path.islink(p):
            for dirpath, _, files in os.walk(p):
                for f in files:
                    add_file(os.path.join(dirpath, f))
        else:
            add_file(p)
    return sorted(out)


def _tarinfo(tf, path, arcname, mtime, mode=None, isdir=False, islink=False):
    ti = tf.gettarinfo(path, arcname=arcname)
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    ti.mtime = mtime
    if isdir:
        ti.type = tarfile.DIRTYPE
        ti.mode = 0o755
        ti.size = 0
    elif mode is not None and not islink:
        ti.mode = mode
    return ti


def make_tar_gz(dest_path, root, rel, mtime, modes):
    """GNU tar 风格的递归打包：目录项 + 按名字排序的子项，全部 ./ 前缀。"""
    with open(dest_path, "wb") as raw:
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0)
        tf = tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT)

        def walk(r):
            dpath = os.path.join(root, r) if r else root
            arcname = "./" if not r else "./" + r + "/"
            tf.addfile(_tarinfo(tf, dpath, arcname, mtime, isdir=True))
            for name in sorted(os.listdir(dpath)):
                full = os.path.join(dpath, name)
                arel = (r + "/" + name) if r else name
                st = os.lstat(full)
                if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                    walk(arel)
                elif stat.S_ISLNK(st.st_mode):
                    ti = _tarinfo(tf, full, "./" + arel, mtime, islink=True)
                    tf.addfile(ti)
                else:
                    ti = _tarinfo(tf, full, "./" + arel, mtime,
                                  mode=modes.get(arel.replace(os.sep, "/"), 0o644))
                    with open(full, "rb") as fh:
                        tf.addfile(ti, fh)

        walk(rel)
        tf.close()
        gz.close()


def build_ipk(out_path, stage, modes, mtime, meta, depends, conffiles=None,
              pkgname=PKG, arch="all", description=""):
    tmp = tempfile.mkdtemp(prefix="ipk-")
    try:
        # ---- data.tar.gz
        data = os.path.join(tmp, "data.tar.gz")
        make_tar_gz(data, stage, "", mtime, modes)

        # ---- control.tar.gz
        ctl_dir = os.path.join(tmp, "control")
        os.makedirs(ctl_dir)
        nfiles, total = tree_stats(stage)
        installed = ((total + 1023) // 1024) * 1024

        lines = [
            "Package: %s" % pkgname,
            "Version: %s" % meta,
            "Depends: %s" % ", ".join(depends),
            "Source: %s" % ORIGIN,
            "SourceName: %s" % pkgname,
            "License: %s" % LICENSE,
            "Section: %s" % SECTION,
            "SourceDateEpoch: %d" % mtime,
            "URL: %s" % URL,
            "Maintainer: %s" % MAINTAINER,
            "Architecture: %s" % arch,
            "Installed-Size: %d" % installed,
            "Description: %s" % description,
        ]
        open(os.path.join(ctl_dir, "control"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

        if conffiles:
            resolved = resolve_conffiles_ipk(stage, conffiles)
            if resolved:
                open(os.path.join(ctl_dir, "conffiles"), "w", encoding="utf-8").write(
                    "\n".join(resolved) + "\n")
                log("conffiles: %d 条（由声明 %r 展开为普通文件）" % (len(resolved), conffiles))

        postinst = (
            '#!/bin/sh\n'
            '[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0\n'
            '[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0\n'
            '. ${IPKG_INSTROOT}/lib/functions.sh\n'
            'default_postinst $0 $@\n'
        )
        postinst_pkg = (
            '[ -n "${IPKG_INSTROOT}" ] || { rm -f /tmp/luci-indexcache.*\n'
            '\trm -rf /tmp/luci-modulecache/\n'
            '\t/etc/init.d/rpcd reload 2>/dev/null\n'
            '\texit 0\n'
            '}\n'
        )
        prerm = (
            '#!/bin/sh\n'
            '[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0\n'
            '. ${IPKG_INSTROOT}/lib/functions.sh\n'
            'default_prerm $0 $@\n'
        )
        for name, content, mode in (("postinst", postinst, 0o755),
                                    ("postinst-pkg", postinst_pkg, 0o755),
                                    ("prerm", prerm, 0o755)):
            p = os.path.join(ctl_dir, name)
            open(p, "w", encoding="utf-8").write(content)
            os.chmod(p, mode)
            os.utime(p, (mtime, mtime))

        ctl_modes = {"control": 0o644, "conffiles": 0o644,
                     "postinst": 0o755, "postinst-pkg": 0o755, "prerm": 0o755}
        control = os.path.join(tmp, "control.tar.gz")
        make_tar_gz(control, ctl_dir, "", mtime, ctl_modes)

        # ---- 外层：debian-binary + data.tar.gz + control.tar.gz
        with open(out_path, "wb") as raw:
            gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0)
            tf = tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT)
            for name, src in (("debian-binary", None), ("data.tar.gz", data), ("control.tar.gz", control)):
                if src is None:
                    payload = b"2.0\n"
                    ti = tarfile.TarInfo("./" + name)
                    ti.size = len(payload)
                    ti.mtime = mtime
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mode = 0o644
                    tf.addfile(ti, __import__("io").BytesIO(payload))
                else:
                    ti = tf.gettarinfo(src, arcname="./" + name)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mtime = mtime
                    ti.mode = 0o644
                    with open(src, "rb") as fh:
                        tf.addfile(ti, fh)
            tf.close()
            gz.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path


# ---------------------------------------------------------------- apk 打包

def inject_apk_conffiles(stage, declared, pkgname, mtime):
    """复刻 package-pack.mk 的 apk 分支：conffiles 原样放入
    lib/apk/packages/<pkg>.conffiles，普通文件再生成 <pkg>.conffiles_static（sha256）。"""
    if not declared:
        return
    d = os.path.join(stage, "lib/apk/packages")
    os.makedirs(d, exist_ok=True)
    lines = [c.strip() for c in declared if c.strip()]
    open(os.path.join(d, "%s.conffiles" % pkgname), "w", encoding="utf-8").write(
        "\n".join(lines) + "\n")
    static = []
    for c in lines:
        p = os.path.join(stage, c.lstrip("/"))
        if os.path.isfile(p) and not os.path.islink(p):
            csum = hashlib.sha256(open(p, "rb").read()).hexdigest()
            static.append("%s %s" % (c, csum))
    if static:
        open(os.path.join(d, "%s.conffiles_static" % pkgname), "w", encoding="utf-8").write(
            "\n".join(static) + "\n")
    for f in os.listdir(d):
        fp = os.path.join(d, f)
        os.chmod(fp, 0o644)
        os.utime(fp, (mtime, mtime))


def script_post_install(pkgname):
    return (
        '#!/bin/sh\n'
        '[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0\n'
        '[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0\n'
        '. ${IPKG_INSTROOT}/lib/functions.sh\n'
        'export root="${IPKG_INSTROOT}"\n'
        'export pkgname="%s"\n'
        'add_group_and_user\n'
        'default_postinst\n'
        '[ -n "${IPKG_INSTROOT}" ] || { rm -f /tmp/luci-indexcache.*\n'
        '\trm -rf /tmp/luci-modulecache/\n'
        '\t/etc/init.d/rpcd reload 2>/dev/null\n'
        '\texit 0\n'
        '}\n' % pkgname
    )


def script_post_upgrade(pkgname):
    return (
        '#!/bin/sh\n'
        'export PKG_UPGRADE=1\n'
        '[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0\n'
        '[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0\n'
        '. ${IPKG_INSTROOT}/lib/functions.sh\n'
        'export root="${IPKG_INSTROOT}"\n'
        'export pkgname="%s"\n'
        'add_group_and_user\n'
        'default_postinst\n'
        '[ -n "${IPKG_INSTROOT}" ] || { rm -f /tmp/luci-indexcache.*\n'
        '\trm -rf /tmp/luci-modulecache/\n'
        '\t/etc/init.d/rpcd reload 2>/dev/null\n'
        '\texit 0\n'
        '}\n' % pkgname
    )


def script_pre_deinstall(pkgname):
    return (
        '#!/bin/sh\n'
        '[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0\n'
        '. ${IPKG_INSTROOT}/lib/functions.sh\n'
        'export root="${IPKG_INSTROOT}"\n'
        'export pkgname="%s"\n'
        'default_prerm\n' % pkgname
    )


def build_apk(out_path, stage, mtime, version, depends, pkgname=PKG,
              arch="noarch", description="", scripts=True, provides=None):
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = str(mtime)
    cmd = [APK_BIN, "mkpkg", "--files", stage, "--output", out_path]
    for k, v in (
        ("name", pkgname),
        ("version", version),
        ("description", description),
        ("arch", arch),
        ("license", LICENSE),
        ("origin", ORIGIN),
        ("maintainer", MAINTAINER),
        ("url", URL),
    ):
        cmd += ["--info", "%s:%s" % (k, v)]
    # 注意：--info 同名键会覆盖，数组型字段必须一次性用空格分隔传入
    if depends:
        cmd += ["--info", "depends:%s" % " ".join(depends)]
    if provides:
        cmd += ["--info", "provides:%s" % " ".join(provides)]

    tmp = tempfile.mkdtemp(prefix="apk-script-")
    try:
        if scripts:
            for fname, content in (
                ("post-install", script_post_install(pkgname)),
                ("post-upgrade", script_post_upgrade(pkgname)),
                ("pre-deinstall", script_pre_deinstall(pkgname)),
            ):
                p = os.path.join(tmp, fname)
                open(p, "w", encoding="utf-8").write(content)
                os.chmod(p, 0o755)
                cmd += ["--script", "%s:%s" % (fname, p)]
        sh(cmd, env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path


# ---------------------------------------------------------------- 校验

def check_exec_bits(stage):
    """语义校验：init.d 脚本与 ssrplus 目录下的 *.sh 必须带可执行位。

    仅比对“包内=stage”抓不到这类 bug——两边同时丢位就都通过，
    而设备上会直接 Permission denied（196-r1901 真实事故）。
    """
    bad = []
    for dirpath, _, files in os.walk(stage):
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            rel = os.path.relpath(p, stage).replace(os.sep, "/")
            need_exec = rel.startswith("etc/init.d/") or \
                (rel.startswith("usr/share/shadowsocksr/") and rel.endswith(".sh"))
            if need_exec and not (os.stat(p).st_mode & 0o111):
                bad.append("缺少可执行位: " + rel)
    return bad


def verify_ipk(ipk, stage):
    """读 ipk 内 data/control 成员清单，比对文件集合与权限（不落盘）。

    早期实现走 extractall，在某些托管 Python 环境（部分 shim 会拦截
    os.mkdir/EEXIST）下会直接 PermissionError；读成员即可完成全部核对。
    """
    import io as _io
    with open(ipk, "rb") as f:
        outer = tarfile.open(fileobj=f, mode="r:gz")
        names = outer.getnames()
        assert "./debian-binary" in names and "./data.tar.gz" in names and "./control.tar.gz" in names, names
        data_gz = outer.extractfile("./data.tar.gz").read()
        ctl_gz = outer.extractfile("./control.tar.gz").read()

    data = tarfile.open(fileobj=_io.BytesIO(data_gz), mode="r:gz")
    ctl = tarfile.open(fileobj=_io.BytesIO(ctl_gz), mode="r:gz")
    ctl_names = ctl.getnames()

    src, dst = set(), set()
    for dirpath, _, files in os.walk(stage):
        for f in files:
            src.add(os.path.relpath(os.path.join(dirpath, f), stage))

    bad = []
    for m in data.getmembers():
        if m.isdir():
            continue
        rel = m.name[2:] if m.name.startswith("./") else m.name
        dst.add(rel)
        sp = os.path.join(stage, rel)
        if not os.path.exists(sp):
            bad.append("多余: " + rel)
            continue
        if m.issym():
            continue
        want = stat.S_IMODE(os.stat(sp).st_mode)
        if m.mode != want:
            bad.append("权限不符 %s: %o != %o" % (rel, m.mode, want))
        if m.uid != 0 or m.gid != 0:
            bad.append("属主不是 root: " + rel)
    bad.extend(check_exec_bits(stage))
    return (src == dst), sorted(src - dst), sorted(dst - src), bad, ctl_names


def verify_apk(apk, stage):
    """用 apk 自身解包做回环校验（文件集合 + 权限）。"""
    tmp = tempfile.mkdtemp(prefix="vapk-")
    try:
        sh([APK_BIN, "--allow-untrusted", "extract", "--destination", tmp, apk], check=False)
        src, dst = set(), set()
        for dirpath, _, files in os.walk(stage):
            for f in files:
                src.add(os.path.relpath(os.path.join(dirpath, f), stage))
        for dirpath, _, files in os.walk(tmp):
            for f in files:
                dst.add(os.path.relpath(os.path.join(dirpath, f), tmp))
        bad = []
        for rel in sorted(src & dst):
            sp = os.path.join(stage, rel)
            dp = os.path.join(tmp, rel)
            if os.path.islink(sp):
                continue
            want = stat.S_IMODE(os.stat(sp).st_mode)
            got = stat.S_IMODE(os.stat(dp).st_mode)
            if want != got:
                bad.append("权限不符 %s: %o != %o" % (rel, got, want))
        bad.extend(check_exec_bits(stage))
        return (src == dst), sorted(src - dst), sorted(dst - src), bad
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 主流程

def build_i18n(repo, src_pkg, out_dir, mtime, po_epoch, fallback_version="0"):
    """luci-i18n-ssr-plus-zh-cn 语言包（luci.mk 的 LuciTranslation 规则）。"""
    po = os.path.join(src_pkg, "po", "zh_Hans", "ssr-plus.po")
    if not os.path.exists(po):
        return None
    version = fallback_version
    # luci.mk: findrev(po) -> "%y.%j.%05d~%h"
    fmt = sh(["git", "log", "-1", "--format=%ct %h", "--abbrev=7", "--",
              os.path.relpath(po, repo)], cwd=repo, check=False)
    if fmt:
        ct, _, h = fmt.partition(" ")
        ct = int(ct)
        secs = ct % 86400
        yday = datetime.fromtimestamp(ct, timezone.utc).strftime("%y.%j")
        version = "%s.%05d~%s" % (yday, secs, h)

    stage = os.path.join(out_dir, ".stage-i18n")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    os.makedirs(os.path.join(stage, "usr/lib/lua/luci/i18n"))
    os.makedirs(os.path.join(stage, "etc/uci-defaults"))

    lmo = os.path.join(stage, "usr/lib/lua/luci/i18n/ssr-plus.zh-cn.lmo")
    sh([PO2LMO_BIN, po, lmo])
    os.chmod(lmo, 0o644)

    uci_def = os.path.join(stage, "etc/uci-defaults/luci-i18n-ssr-plus-zh-cn")
    open(uci_def, "w", encoding="utf-8").write(
        "uci set luci.languages.zh_cn='简体中文 (Simplified Chinese)'; uci commit luci\n")
    os.chmod(uci_def, 0o755)

    modes = {"usr/lib/lua/luci/i18n/ssr-plus.zh-cn.lmo": 0o644,
             "etc/uci-defaults/luci-i18n-ssr-plus-zh-cn": 0o755}
    set_tree_meta(stage, modes, po_epoch)
    return {
        "name": "luci-i18n-ssr-plus-zh-cn",
        "version": version,
        "stage": stage,
        "modes": modes,
        "depends": ["libc", PKG],
        "description": "%s - zh-cn translation" % PKG,
    }


def main():
    ap = argparse.ArgumentParser(description="本地打包 luci-app-ssr-plus 为 ipk/apk")
    ap.add_argument("--src", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="包源码根：可以是本仓库根（含 Makefile/luasrc/po/root），"
                         "也可以是 helloworld 这类 monorepo 根（会自动找 <root>/luci-app-ssr-plus）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "out"), help="产物目录")
    ap.add_argument("--format", default="all", choices=["ipk", "apk", "all"])
    ap.add_argument("--enable", default="",
                    help="额外打开的选项，逗号分隔，如 INCLUDE_Mihomo,INCLUDE_ChinaDNS_NG")
    ap.add_argument("--no-i18n", action="store_true", help="不生成中文语言包")
    ap.add_argument("--release", default=None, help="覆盖 PKG_RELEASE（默认读 Makefile）")
    args = ap.parse_args()

    repo = os.path.abspath(args.src)
    if os.path.isfile(os.path.join(repo, "Makefile")) and os.path.isdir(os.path.join(repo, "luasrc")):
        src_pkg, rel = repo, "."          # 本仓库：根即包根
    elif os.path.isdir(os.path.join(repo, PKG)):
        src_pkg, rel = os.path.join(repo, PKG), PKG   # monorepo：根/<包名>
    else:
        sys.exit("找不到包根 %s" % repo)
    if not os.path.exists(PO2LMO_BIN):
        sys.exit("缺少工具 %s（先跑 tools/deps.sh 准备依赖）" % PO2LMO_BIN)
    if args.format in ("apk", "all") and not os.path.exists(APK_BIN):
        log("注意: 缺少 %s（先跑 tools/deps.sh），本次不出 .apk 产物" % APK_BIN)

    mf = os.path.join(src_pkg, "Makefile")
    meta = parse_makefile(mf, PKG)
    if args.release:
        meta["version"] = "%s-r%s" % (meta["raw_version"], args.release)

    config = dict(DEFAULT_CONFIG)
    for k in args.enable.split(","):
        k = k.strip()
        if k:
            config[k.replace("CONFIG_PACKAGE_%s_" % PKG, "")] = "y"
    depends = compute_depends(meta, config, PKG)
    log("包名    : %s" % PKG)
    log("版本    : %s" % meta["version"])
    log("架构    : %s" % meta["pkgarch"])
    log("依赖    : %s" % ", ".join(depends))

    # SOURCE_DATE_EPOCH：用该包最后一次提交时间，保证可复现
    ts = sh(["git", "log", "-1", "--format=%ct", "--", rel], cwd=repo, check=False)
    mtime = int(ts) if ts else int(datetime.now().timestamp())

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    git_modes = git_file_modes(repo, rel)
    # 本仓库（rel="."）时 git ls-files 返回的路径不带包名前缀，
    # 而 stage_tree 的查表键是 "<pkg>/<src_prefix>/<rel>"，缺前缀会全部
    # 回退成 0644 —— 196-r1901 设备上 init.d / *.sh 丢可执行位就源于此。
    if rel == ".":
        git_modes = {"%s/%s" % (PKG, k): v for k, v in git_modes.items()}
    stage = os.path.join(out_dir, ".stage")
    modes = stage_tree(src_pkg, stage, PKG, meta["raw_version"], git_modes)
    set_tree_meta(stage, modes, mtime)
    nfiles, total = tree_stats(stage)
    log("文件数  : %d（%.1f KiB）" % (nfiles, total / 1024.0))

    made = []
    if args.format in ("ipk", "all"):
        p = os.path.join(out_dir, "%s_%s_%s.ipk" % (PKG, meta["version"], meta["pkgarch"]))
        build_ipk(p, stage, modes, mtime, meta["version"], depends,
                  conffiles=meta["conffiles"], arch=meta["pkgarch"],
                  description=meta["title"])
        ok, miss, extra, bad, ctl = verify_ipk(p, stage)
        made.append(("ipk", p, ok, "缺失%s 多余%s 异常%s" % (miss, extra, bad),
                     "control 段: " + ", ".join(ctl)))
    if args.format in ("apk", "all"):
        # apk 需要 lib/apk/packages/*.conffiles(_static)，打在独立的 stage 副本上
        apk_stage = os.path.join(out_dir, ".stage-apk")
        if os.path.exists(apk_stage):
            shutil.rmtree(apk_stage)
        shutil.copytree(stage, apk_stage, symlinks=True)
        inject_apk_conffiles(apk_stage, meta["conffiles"], PKG, mtime)
        p = os.path.join(out_dir, "%s-%s.apk" % (PKG, meta["version"]))
        build_apk(p, apk_stage, mtime, meta["version"], depends,
                  arch="noarch", description=meta["title"],
                  provides=["%s-any" % PKG])
        ok, miss, extra, bad = verify_apk(p, apk_stage)
        made.append(("apk", p, ok, "缺失%s 多余%s 异常%s" % (miss, extra, bad), ""))

    if not args.no_i18n:
        i18n = build_i18n(repo, src_pkg, out_dir, mtime, mtime,
                          fallback_version=meta["version"])
        if i18n:
            log("语言包  : %s %s" % (i18n["name"], i18n["version"]))
            if args.format in ("ipk", "all"):
                p = os.path.join(out_dir, "%s_%s_all.ipk" % (i18n["name"], i18n["version"]))
                build_ipk(p, i18n["stage"], i18n["modes"], mtime, i18n["version"],
                          i18n["depends"], arch="all", pkgname=i18n["name"],
                          description=i18n["description"])
                ok, miss, extra, bad, ctl = verify_ipk(p, i18n["stage"])
                made.append(("ipk", p, ok, "缺失%s 多余%s 异常%s" % (miss, extra, bad), ""))
            if args.format in ("apk", "all"):
                p = os.path.join(out_dir, "%s-%s.apk" % (i18n["name"], i18n["version"]))
                build_apk(p, i18n["stage"], mtime, i18n["version"], i18n["depends"],
                          arch="noarch", pkgname=i18n["name"],
                          description=i18n["description"], scripts=False)
                ok, miss, extra, bad = verify_apk(p, i18n["stage"])
                made.append(("apk", p, ok, "缺失%s 多余%s 异常%s" % (miss, extra, bad), ""))

    log("")
    for kind, p, ok, detail, extra in made:
        size = os.path.getsize(p)
        sha = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        log("%-4s %s  %8d B  sha256:%s  校验:%s %s" %
            (kind, os.path.basename(p), size, sha, "OK" if ok else "FAIL",
             "" if ok else detail))
    if not all(m[2] for m in made):
        sys.exit(1)


if __name__ == "__main__":
    main()
