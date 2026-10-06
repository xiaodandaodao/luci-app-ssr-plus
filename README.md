# luci-app-ssr-plus（衍生构建仓库）

> 基于上游 [`fw876/helloworld`](https://github.com/fw876/helloworld) tag **v196.14** 的 `luci-app-ssr-plus`，
> 加上一批 Mihomo 策略组增强，并配了一套**自持的本地/CI 打包工具链**：写完代码推到 GitHub，
> Actions 直接编出 `.ipk` / `.apk` 交付，不需要 OpenWrt SDK，也不用交叉编译。
>
> 来源与改动逐条列在 [ATTRIBUTION.md](./ATTRIBUTION.md)，许可证沿用上游 [GPL-3.0](./LICENSE)。

## 功能（相对上游 v196.14 的增量）

- **智能地区分组**：订阅 YAML 没自带节点分组时，按节点名里的地区关键词（港/新/日/美/台/韩…）
  自动生成地区 url-test 组 + 全局自动选择组，并把组名注入各分流组。开关：`mihomo_urltest` +
  `mihomo_auto_regions`（每组最少节点数 / 最多组数可调）。
- **自定义分组**：面板「分组管理」里手动建组、勾选节点、选自动/手动模式，可作用于各分流组。
- **url-test 自动选最快节点**：为每个叶子组生成 url-test 孪生组并置顶。
- **策略组面板延迟可视化**：自动选择 / 当前选中 / 实际出口节点 + 成员延迟 chip
  （绿 <200ms、黄 <500ms、橙 <1s、红 ≥1s、灰 = 无数据）+ 单组/全部测速。

## 安装

从 [Releases](../../releases) 下载对应格式，路由器上安装：

```sh
# opkg（传统 ipk 固件）
opkg install luci-app-ssr-plus_196-r1401_all.ipk
opkg install luci-i18n-ssr-plus-zh-cn_196-r1401_all.ipk

# apk（apk-tools v3 固件，如 OpenWrt 24.10+ / ImmortalWrt）
apk add --allow-untrusted ./luci-app-ssr-plus-196-r1401.apk
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
| 上游 `v196.14` 的第 1 版（当前） | `1401` |
| 同一上游基线上的第 2 次修订 | `1402` |
| 上游升到 `v196.15` 后的第 1 版 | `1501` |

之所以必须是**纯数字**：apk 的版本号是 `<PKG_VERSION>-r<PKG_RELEASE>`，`-r` 后面只接受整数。
`RE13` / `196R-r13` / `196-r13.1` 这类写法会被 `apk mkpkg` 直接拒绝（实测 `package version is invalid`），
只有「单字母后缀」`196a-r13` 和「纯数字 revision」可行。数字编码同时保证数值单调递增，
`opkg` / `apk` 都会判定为新版（`1401 > 17 > 13`），旧的 `196-r17` 也能正常升级上来。

合并完、跑过校验和构建之后，**必须重生成补丁并推进基线**：

```sh
bash tools/sync-upstream.sh --regen-patch v196.20
```

它把「我们相对新上游的全部差异」写进 `patches/ssrplus-local-changes.patch`，
并把新基线写进 `.upstream-base`。不重生成就等于没留档，下次同步会失去依据。

需要移植到别人的预编译包（拿不到源码）时，走 `tools/inject_pkg.py`，
见 [patches/README.md](./patches/README.md) 路线 B。

## 致谢与声明

包本体来自上游 `fw876/helloworld`（及其依赖的 `openwrt/luci` 等），`po2lmo` 编译自
`openwrt/luci`，`apk-static` 来自 Alpine Linux。本仓库只是衍生整理，不是官方发布渠道；
产品问题请优先反馈上游。详见 [ATTRIBUTION.md](./ATTRIBUTION.md)。
