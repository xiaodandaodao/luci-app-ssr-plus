# luci-app-ssr-plus（衍生构建仓库）

> 基于上游 [`fw876/helloworld`](https://github.com/fw876/helloworld) tag **v196.19** 的 `luci-app-ssr-plus`，
> 加上一批 Mihomo 策略组增强，并配了一套**自持的本地/CI 打包工具链**：写完代码推到 GitHub，
> Actions 直接编出 `.ipk` / `.apk` 交付，不需要 OpenWrt SDK，也不用交叉编译。
>
> 来源与改动逐条列在 [ATTRIBUTION.md](./ATTRIBUTION.md)，许可证沿用上游 [GPL-3.0](./LICENSE)。

## 功能（相对上游 v196.19 的增量）

- **自动切换（核心）**：订阅里的策略组几乎全是 `select` 手动选择器 —— 选中的节点断了也不会
  自己换。开启后为每个选择器生成一个成员相同的 `url-test` 自动组，由 Mihomo 持续挑选最快的
  那个；面板上手选任意节点照旧。开关：`mihomo_urltest`（可选 `mihomo_autoselect_apply` 让服务
  每次启动后自动把选择器指向自动组）。
- **策略组面板延迟可视化**：自动选择 / 当前选中 / 实际出口节点 + 成员延迟 chip
  （绿 <200ms、黄 <500ms、橙 <1s、红 ≥1s、灰 = 无数据）+ 单组/全部测速。
- **统一 Clash 面板（左侧三页签）**：代理组 / 客户端规则 / 组件更新 合到一个弹窗里，
  卡片流布局、跟随 LuCI 明暗主题；客户端规则保存后自动重载服务，组件升级完成后自动
  reload（主程序升级则自动刷新页面）。
- **排障增强**：Mihomo 的启动输出不再一律丢进 `/dev/null`。启动后回看进程是否存活，
  立刻退出时把真实原因逐行写进日志并返回失败，替代原来无条件的 `Mihomo Started!`。

上游 v196.14 → v196.19 的改动（AnyTLS 支持、IPv6 流量代理、nftables GFW 模式 DNS 转发修复、
legacy SS 订阅解析修复）已全部并入。

## 安装

从 [Releases](../../releases) 下载对应格式，路由器上安装：

```sh
# opkg（传统 ipk 固件）
opkg install luci-app-ssr-plus_196-r1904_all.ipk
opkg install luci-i18n-ssr-plus-zh-cn_196-r1904_all.ipk

# apk（apk-tools v3 固件，如 OpenWrt 24.10+ / ImmortalWrt）
apk add --allow-untrusted ./luci-app-ssr-plus-196-r1904.apk
```

装完刷新 LuCI 页面即可。功能开关默认是关的，在「服务 → ShadowSocksR Plus+ → 服务器」里打开。

## 构建

### GitHub Actions（主要方式）

仓库自带 [`.github/workflows/build.yml`](.github/workflows/build.yml)：

| 触发 | 行为 |
|---|---|
| push 到 `main` | 静态校验 + 打包，产物进 Actions Artifacts（保留 30 天） |
| 打 tag `v*` | 同上，并自动发 Release，附 `.ipk` / `.apk` 与 sha256 清单 |
| 手动 `workflow_dispatch` | 可临时覆盖 `PKG_RELEASE` 或附加热开关（如 `INCLUDE_Mihomo`） |

单个 job 约 1–2 分钟（只编纯脚本包，不需要 SDK）。发布新版本：

```sh
git tag v196-r1302 && git push origin v196-r1302
```

### 本地构建

需要：`python3`、C 编译器、`tar`、`curl`（以及可选的 `node` 用于校验）。

```sh
bash tools/deps.sh            # 生成 tools/po2lmo 与 tools/apk（自动按本机架构）
python3 tools/build.py        # 默认从本仓库根打包，产物在 out/
python3 tools/build.py --format ipk          # 只出 ipk
python3 tools/build.py --release 1302        # 覆盖 PKG_RELEASE
python3 tools/build.py --enable INCLUDE_Mihomo,INCLUDE_ChinaDNS_NG

bash tools/verify.sh          # 静态校验：Lua / shell / PO / htm 模板 / jsdom 冒烟
```

`build.py` 复刻了 OpenWrt buildroot 的打包行为（`luci.mk` 安装规则、`package.mk` 的 control 与
`preinst/postinst` 模板、conffiles 展开规则），产物会做一次解包回环校验；`--src` 也支持指向
helloworld 这类 monorepo 根（会自动找 `<root>/luci-app-ssr-plus`）。

## 目录

```
Makefile  luasrc/  po/  root/     # 包本体（= 上游 luci-app-ssr-plus，含上述改动）
tools/                            # 打包与校验工具链（build.py / deps.sh / verify.sh / …）
patches/                          # 相对上游基线的统一 diff + 移植说明
payload/                          # 无源码注入载荷（给别人打到自己预编译的包上）
.upstream-base                    # 我们当前对齐的上游 tag（同步流程用）
.github/workflows/build.yml       # CI：校验 → 打包 → Artifact / Release
```

## 同步上游

本仓库的**根目录**就是上游的 `luci-app-ssr-plus/` 子目录（再叠加我们的改造）。
所以「同步」= 把上游新版本对这个子目录的改动搬进本仓库，而不是把我们的补丁打到上游去。
两边历史无关、目录还差一层，直接 `git merge` 会炸成一片冲突，因此改用差分重放：

```sh
bash tools/sync-upstream.sh --status     # 看当前基线 + 已拉取的上游 tag
bash tools/sync-upstream.sh v196.20      # 同步到上游某个 tag
```

脚本会：把上游 tag 拉进 `refs/upstream-tags/`（不污染本仓库自己的 `v*` tag）→ 生成
「基线 tag → 新 tag」对本包的差分 → `git apply -p2` 剥掉目录层落到仓库根 →
和我们改过的文件走三方合并。**只有真正同行冲突才停下**，并打印裁决原则与后续步骤。

冲突时的判断准则：

| 情况 | 怎么做 |
|---|---|
| 上游的 bug 修复 / 新功能 | 采纳上游 |
| 我们的改造（清单见 [ATTRIBUTION.md](./ATTRIBUTION.md) §2） | 保留我们 |
| `Makefile` 里的 `PKG_VERSION` / `PKG_RELEASE` | 永远用我们的（规则见下） |

### 版本号规则

`PKG_RELEASE` 编码为 **`<上游 release><两位本方修订序>`**，与上游 tag 一一对应：

| 情况 | 值 |
|---|---|
| 上游 `v196.19` 的第 1 版 | `1901` |
| 同一上游基线上的第 2 次修订（打包权限位修复） | `1902` |
| 同一上游基线上的第 3 次修订（主程序在线升级指向本仓库） | `1903` |
| 同一上游基线上的第 4 次修订（当前，自定义节点组 + 面板优化） | `1904` |
| 上游升到 `v196.20` 后的第 1 版 | `2001` |

之所以必须是**纯数字**：apk 的版本号是 `<PKG_VERSION>-r<PKG_RELEASE>`，`-r` 后面只接受整数。
`RE13` / `196R-r13` / `196-r13.1` 这类写法会被 `apk mkpkg` 直接拒绝（实测 `package version is invalid`），
只有「单字母后缀」`196a-r13` 和「纯数字 revision」可行。数字编码同时保证数值单调递增，
`opkg` / `apk` 都会判定为新版（`1904 > 1903 > 1902 > 1901 > 1401 > 17 > 13`），旧的 `196-r17` 也能正常升级上来。

合并完、跑过校验和构建之后，**必须重生成补丁并推进基线**：

```sh
bash tools/sync-upstream.sh --regen-patch v196.20
```

它把「我们相对新上游的全部差异」写进 `patches/ssrplus-local-changes.patch`，
并把新基线写进 `.upstream-base`。不重生成就等于没留档，下次同步会失去依据。

需要移植到别人的预编译包（拿不到源码）时，走 `tools/inject_pkg.py`，
见 [patches/README.md](./patches/README.md) 路线 B。

### 自动跟踪（无人值守）

上面这套手活已经接进 CI：`.github/workflows/upstream-sync.yml` 每 6 小时看一次上游
`fw876/helloworld` 有没有发新 tag，分三种情况处理：

| 上游状态 | 动作 |
|---|---|
| 没发新 tag，或新 tag 没动到 `luci-app-ssr-plus/` | 静默结束，不产生提交、不发通知 |
| 有新 tag 且三方合并干净 | 全自动走完：合并 → 推进 `PKG_RELEASE` → 重生成补丁与载荷 → 刷新文档里的版本串 → 推 `main` → 打 tag `v196-r<PKG_RELEASE>` → 触发 `build.yml` 打包并发 Release |
| 有新 tag 但撞上我们改过的文件 | 开 PR（分支 `sync/upstream-<tag>`）+ 发 issue 等你裁决，冲突文件列在 `conflicts.txt` 和 PR 描述里 |

自动化的引擎是 `tools/auto_sync.py`（CI 专用、全程非交互），和给人用的
`tools/sync-upstream.sh` 是同一套规则的两副皮：前者靠退出码表达结果
（`0` 正常 / `2` 有冲突 / `3` 本包无变化），后者会在冲突时停下来打提示。
发完新版后默认只保留最近 2 个 Release（只删 Release，tag 一律保留）。

手动干预：在 Actions 页面手动跑 **上游跟踪与自动发布**，可用 `upstream_tag` 指定同步到
某个 tag、`dry_run` 只看不动、`no_release` 同步但不发版、`keep_releases` 调整保留个数。

冲突 PR 按上面的裁决准则处理完，收尾照 PR 描述里那段跑即可（版本号用 PR 里给的值）：

```sh
# 1. 编辑冲突文件，清掉 <<<<<<< ======= >>>>>>> 标记
git add <解决后的文件> && git commit -m "chore: 同步上游 <上游tag>"
bash tools/verify.sh
# 2. 推进版本号 + 重生成补丁与载荷
python3 tools/auto_sync.py finalize <上游tag> <新PKG_RELEASE>
git add -A && git commit -m "chore: 重生成补丁与注入载荷"
git push
```

合并进 `main` 后会照常触发构建发布。

## 致谢与声明

包本体来自上游 `fw876/helloworld`（及其依赖的 `openwrt/luci` 等），`po2lmo` 编译自
`openwrt/luci`，`apk-static` 来自 Alpine Linux。本仓库只是衍生整理，不是官方发布渠道；
产品问题请优先反馈上游。详见 [ATTRIBUTION.md](./ATTRIBUTION.md)。
