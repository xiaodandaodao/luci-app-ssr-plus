# 把这些功能移植到别的 luci-app-ssr-plus 包

本目录提供**两条互不依赖的路线**，用来在「不是从本项目源码构建」的
`luci-app-ssr-plus` 上获得这些功能：

* **自动切换**：给订阅里每个 `select` 选择器挂一个成员相同的 `url-test` 影子组，
  让 mihomo 持续挑选最快的节点（订阅里的选择器本身是永不自动变的）
* 策略组面板延迟可视化：自动选择 / 当前选中 / 实际出口节点 + 成员延迟 chip + 测速
* 排障增强：mihomo 启动失败不再静默、geo 数据升级自动建目标目录

| 路线 | 适用场景 | 需要什么 |
|---|---|---|
| **A. 源码补丁** `ssrplus-local-changes.patch` | 能拿到 helloworld 源码 | 源码树 + 打包环境 |
| **B. 注入预编译包** `../payload/` + `../tools/inject_pkg.py` | **拿不到源码**（别人发布的闭源/预编译 ipk、apk） | 只要那个包本身 |

两条路线产出的包，内容与从本项目源码直接构建的 `196-r1902` **完全一致**（见下文验证）。

---

## A. 源码补丁

补丁相对 **上游 `fw876/helloworld` 的 tag `v196.19`** 生成，包含 14 个文件的改动
（其中 `luasrc/view/shadowsocksr/clash_groups_ui.htm` 是新增文件）。

```bash
cd /path/to/helloworld
git apply /path/to/ssrplus-local-changes.patch     # 或：
patch -p1 < /path/to/ssrplus-local-changes.patch   # 没有 git 也能用
```

两种方式都已实测干净通过（无 reject、新文件正常创建）。

打完后按常规方式编译即可。没有 OpenWrt SDK 的话，可以用本仓库上一层的
`build.py`（macOS 本地打包，见 `../README.md`）：

```bash
cd /path/to/ssrplus-local-build
./build.py --src /path/to/helloworld --out ./out
# 产物：luci-app-ssr-plus_196-r1x_all.ipk / luci-app-ssr-plus-196-r1x.apk + 中文语言包
```

### 补丁包含哪些改动

| 文件 | 作用 |
|---|---|
| `root/usr/share/shadowsocksr/clash_yaml.lua` | `inject_autoselect_groups()` 给每个 `select` 组生成 `url-test` 影子组（过滤 DIRECT/REJECT/伪节点），`write_autoselect_map()` 写映射表；原「智能地区分组」已下线 |
| `luasrc/controller/shadowsocksr.lua` | 延迟测试等 RPC（自定义分组 CRUD 已于 1403 移除） |
| `luasrc/view/shadowsocksr/clash_groups_ui.htm` | **新增**：策略组卡片流面板前端组件 |
| `luasrc/view/shadowsocksr/clash_main_panel.htm` | 引入新组件、传入新接口地址 |
| `luasrc/view/shadowsocksr/clash_panel.htm` | 同上（独立页面版） |
| `luasrc/view/shadowsocksr/component.htm` | 组件更新页签增加懒加载与升级后钩子 |
| `luasrc/model/cbi/shadowsocksr/servers.lua` | 开关：`mihomo_urltest`（Clash 自动切换）、`mihomo_autoselect_apply` |
| `root/etc/init.d/shadowsocksr` | `apply_clash_autoselect()` 启动后切到自动组 + 日志；mihomo 启动失败写出真实原因 |
| `root/usr/share/shadowsocksr/shadowsocksr.config` | 新增选项的默认值 |
| `po/zh_Hans/ssr-plus.po` | 新增文案的中文翻译 |
| `root/usr/share/shadowsocksr/update_components.sh` | geo 数据升级前 `mkdir -p` 目标目录 |
| `Makefile` | `PKG_RELEASE` → `1902` |

> 补丁的目标是「把上游 v196.19 变成 1902」，所以除了本次的自动切换功能，也一并带上了
> 早先的延迟可视化与排障增强改动 —— 这些本来就是本项目相对上游的全部差异。

### 换到别的上游版本

补丁与 `v196.19` 严格对应。如果上游已经发了更新的版本（例如 `v196.20`），
`controller` / `servers.lua` / `po` 这些文件很可能已经变了，补丁会有冲突。
这种情况下**建议走路线 B**：注入是文件级替换，只要求目标包是 `arch=all` 的纯脚本包，
对上游版本不敏感。若坚持用补丁，用 `git apply -3`（三方合并）处理冲突后，
重点复核 `Makefile` 的 `PKG_RELEASE` 与 `po` 的重复条目。

---

## B. 注入预编译包（拿不到源码时用这条）

`luci-app-ssr-plus` 是 `arch=all` 的纯脚本包 —— 包里的内容就是最终文件本身，
没有二进制。所以「把改动过的文件换进去再重打包」与「从源码编译」效果完全等价。

### 1. 生成 payload（只需做一次，做完就不再需要源码）

```bash
cd ssrplus-local-build
python3 tools/inject_pkg.py make-payload \
    --src  /path/to/helloworld \
    --patch patches/ssrplus-local-changes.patch \
    --out  payload/
```

生成 `../payload/`：9 个文件（8 个进主包、1 个是编译好的 `.lmo` 语言包）+ `manifest.json`
（含每个文件的安装路径、权限、sha256）。**这个目录可以直接发给别人**，
对方不需要源码、不需要 helloworld 仓库、也不需要本项目的任何其他东西。

想先看看会改哪些文件：

```bash
python3 tools/inject_pkg.py show --patch patches/ssrplus-local-changes.patch
```

### 2. 注入到别人的包

```bash
python3 tools/inject_pkg.py apply \
    --payload payload/ \
    --pkg  luci-app-ssr-plus_196-r20_all.ipk \
    --out  out/luci-app-ssr-plus_196-r20+sg.ipk
```

* 自动识别 ipk / apk（按扩展名，兜底按魔数）
* 自动按包名选对应的一套文件：主包 → 8 个脚本文件；`luci-i18n-ssr-plus-zh-cn` → `.lmo`
* 语言包同样要注入一次，否则界面上的新按钮没有中文：

```bash
python3 tools/inject_pkg.py apply --payload payload/ \
    --pkg luci-i18n-ssr-plus-zh-cn_xxx_all.ipk --out out/luci-i18n-...-sg.ipk
```

先用 `--dry-run` 空跑一遍，会列出「替换 / 新增」清单，不写文件。

### 3. 这个工具做了什么、不做什么

做：

* 保留原包的**全部元数据**：`Package` / `Depends` / `Provides` / `conffiles` /
  `installed-size`（重算）/ 安装脚本 —— 一律沿用目标包，不按本项目的定义覆盖。
* 只重算 `Installed-Size`，因为文件变了。
* 输出前做**回环校验**：重新解包，逐个文件比对 sha256，并核对版本号；不一致直接非零退出。
* 解包时自己做路径越界检查，不用 `tarfile.extractall`（更安全）。

不做 / 要注意：

* **不碰 conffiles**。本项目改的文件里没有一个是 conffile —— 真正的默认配置在
  `/usr/share/shadowsocksr/shadowsocksr.config`，而 `/etc/config/shadowsocksr` 是个
  0 字节占位 conffile。所以注入包不会覆盖用户已有的服务器配置。
* 但这也意味着：**升级后功能仍是「关」的**，需要在「服务器」页把
  「Mihomo 智能分组与自动选择」打开一次。这不是 bug —— 新版代码里对所有新 uci 选项
  都写了代码级默认值，老配置下不会报错，行为与正常 `opkg upgrade` 一致。
* apk 的 `post-install` / `post-upgrade` / `pre-deinstall` 脚本是按 luci 标准模板
  **重新生成**的（不是从目标包里抄的）。这与上游用的模板一致；若对方的包改过这些脚本，
  注入后会变回标准模板。
* 若目标包与本项目基线的上游版本不同，工具会提示「某些文件在原包里找不到」。
  这类情况建议装机后实测 —— 尤其是 `controller/shadowsocksr.lua` 里的前端接口名，
  必须与 `clash_groups_ui.htm` 配套（两者是一起替换的，所以通常没问题）。

### 4. 版本号策略

| 参数 | ipk | apk |
|---|---|---|
| 不加 | 沿用原版本号（推荐） | 沿用原版本号（推荐） |
| `--version-suffix +sg1` | `196-r13+sg1` | ❌ 拒绝（apk 只接受 `<数字>[-r<整数>]`） |
| `--version-suffix a` | `196-r13a` | `196a-r13`（单字母版本后缀） |
| `--revision 1013` | `196-r1013` | `196-r1013` |

apk 的版本号语法实测很严：`+sg` / `~sg` / `.sg1` / `-sg` 全部被 `apk mkpkg` 拒绝，
只有「单个字母后缀」和「数字 revision」可行。不想动版本号就别加参数 ——
`opkg install` / `apk add` 对同版本号会原地覆盖安装。

---

## 验证记录（都做过实测）

用**未打补丁的 `v196.19` 纯净源码树**构建出「官方基线包」（`196-r19`）作为靶子：

**路线 A**

* `git apply --check` / `patch -p1 --dry-run` 均干净通过
* 打完补丁的树与当前工作树**逐文件 sha 完全一致**（包内容目录递归 diff 为空）
* 从打补丁的树构建（`build.py --src <patched-helloworld>`）：ipk `dd533a857b8b7d09…`、
  apk `53c7c56f2dae098f…`，静态校验含新增的可执行位检查，全部 OK
* 与从本仓库直接构建的 `196-r1902` 比对**包内文件树**：72 个文件、名称/权限/内容 sha
  零差异（外层字节不同只来自 tar mtime，无意义）

**路线 B**（对 `196-r13` 基线包注入；`1902` 的载荷文件内容与 `1901` 相同，仅 manifest 版本号更新）

| 比对 | 结果 |
|---|---|
| 注入后 ipk vs 基线 ipk | 差异 **8 项** = 7 个替换 + 1 个新增，其余文件一字未动 |
| 注入后 ipk vs r1902 ipk | **0 差异** |
| 注入后 apk vs 基线 apk | 差异 **8 项** |
| 注入后 apk vs r1902 apk | **0 差异** |
| apk 元数据 | name / arch / license / origin / maintainer / url / depends(16) / provides 全部保留 |
| 语言包（ipk + apk） | 注入后 `.lmo` sha 与 payload 一致 |

即：注入出来的包，文件树与从源码编译的 `196-r1902` 一模一样。

**真机验证**：`196-r1902` 的 ipk 在 QWRT（aarch64，`opkg`）上 `opkg install` 升级成功，
`/etc/init.d/shadowsocksr` 与 `usr/share/shadowsocksr/*.sh` 权限均为 `0755`，服务可启动、
Mihomo 正常运行、面板状态正常（`196-r1901` 因打包丢可执行位，装机后表现为「未运行」）。
装机建议先备份 `/etc/config/shadowsocksr`。

---

## 路径前缀说明（本仓库）

本目录的补丁是**上游视角**生成的，文件路径带 `luci-app-ssr-plus/` 前缀，用于打在
`fw876/helloworld`（或其 fork）这样的 monorepo 树上：

```sh
cd /path/to/helloworld
git apply --3way patches/ssrplus-local-changes.patch
```

而**本仓库根目录就是那个 `luci-app-ssr-plus/` 目录**（包被提升为仓库根），所以在这里应用要
多剥一层：

```sh
# 在本仓库根目录执行
git apply -p2 patches/ssrplus-local-changes.patch   # 或 patch -p2 < ...
```

只是要「看改了什么」的话直接 `git diff --stat` 不方便（改动已在工作树里），建议读
[../ATTRIBUTION.md](../ATTRIBUTION.md) §2 的逐文件清单。
