# 来源与改动说明（ATTRIBUTION）

本仓库是 [`luci-app-ssr-plus`](https://github.com/fw876/helloworld) 的**衍生构建仓库**：
上游源码 + 本文列出的功能改动 + 一套自持的打包/校验工具链。
所有上游代码的权利归原作者，许可证见 [LICENSE](./LICENSE)（GPL-3.0）。

## 1. 上游基线

| 项目 | 值 |
|---|---|
| 上游仓库 | `https://github.com/fw876/helloworld` |
| 基线 tag | **v196.19** |
| 基线 commit | `16617be`（luci-app-ssr-plus: fix legacy SS subscription parsing with Xray） |
| 包版本 | `luci-app-ssr-plus` `196-r1904`（上游 v196.19 为 `196-r19`）<br>版本号规则：`<上游 release><两位本方修订序>`，即「上游 r19 的第 4 版」= `1904`（第 1 版 `1901`，第 2 版 `1902`，第 3 版 `1903`） |
| 许可证 | GPL-3.0（随仓库保留上游 `LICENSE` 原文，未改动） |

上游 monorepo 里 `luci-app-ssr-plus/` 是一个子目录，本仓库把它**提升为仓库根**：

```
上游 fw876/helloworld/luci-app-ssr-plus/   →   本仓库 /
  Makefile                                       Makefile
  luasrc/  po/  root/                            luasrc/  po/  root/
```

因此 `patches/` 里的补丁仍是**上游视角**的路径（`luci-app-ssr-plus/…`，用于打在上游 / fork 的 helloworld 树上），
若要直接打在本仓库根，请用 `patch -p2` / `git apply -p2`。

## 2. 相对上游的功能改动（14 个文件）

对应 `patches/ssrplus-local-changes.patch`。

### ⓪ 上游 v196.14 → v196.19 的改动（全部并入，非本仓库原创）

按 `tools/sync-upstream.sh` 的差分重放流程同步，其中 4 个 commit 触及本包：

| 上游 commit | 内容 |
|---|---|
| `c39f1e3` | AnyTLS 支持：新增 `sing-anytls` outbound，含节点导入、配置生成、编辑与旧节点迁移 |
| `b791299` | 修复 nftables GFW 模式下的 DNS 转发 |
| `afb3aaf` | 可选 IPv6 流量代理：nftables IPv6 TCP/UDP 规则、AAAA 地址集、国内 IPv6 地址库（默认关闭） |
| `16617be` | 修复 legacy SS 订阅链接在 Xray 下的解析（整段 Base64 先解码再解析端点） |

冲突裁决：`Makefile` 的 `PKG_RELEASE` 取我们的（同步当时为 `1901`，现为 `1904`）；`po/zh_Hans/ssr-plus.po`
两边的新增条目都保留（我们的 Clash 面板文案 + 上游 IPv6/AnyTLS 文案）。

### ①–⑦ 本仓库的改动

以下八批改动：

### ① 面板延迟可视化

| 文件 | 改动 |
|---|---|
| `luasrc/controller/shadowsocksr.lua` | `clash_groups` RPC 扩充返回：`delays` 映射、每组成员延迟、`resolved`（顺 `now` 链递归到真实出口节点）；新增 `clash_delay_test` RPC（调 mihomo `GET /group/<name>/delay`，超时值 0 转为 false 表示超时） |
| `luasrc/view/shadowsocksr/clash_groups_ui.htm` | **新增**共享 UI 组件：渲染自动选择/当前选中、实际出口、成员延迟 chip（按延迟升序，绿<200ms / 黄<500ms / 橙<1s / 红≥1s / 灰无数据）、单组「测速」与「全部测速」 |
| `luasrc/view/shadowsocksr/clash_main_panel.htm` | 引入上述组件 + 注入 `urls:{}` |
| `luasrc/view/shadowsocksr/clash_panel.htm` | 同上（独立页面版） |

### ② 自动切换：给每个选择器挂一个 url-test 影子组（1403 重写）

订阅里的策略组几乎清一色是 `select`（手动选择）—— 选中的那个节点即使断了、慢了，mihomo 也不会
替你换一个。开启后，为每个 `select` 组生成一个成员相同的 `url-test` 影子组，放回原组成员列表首位，
于是面板上选它就交给 mihomo 自动挑最快；继续手选任意节点也照旧。

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/clash_yaml.lua` | 核心：`inject_autoselect_groups()` + `write_autoselect_map()`。成员过滤掉 `DIRECT` / `REJECT` / `PASS` 与机场常见的「剩余流量 / 到期时间」伪节点（它们本地秒回，混进 url-test 必然被选中）；有效成员不足 2 个的组跳过；`♻️ <组名>` 形式的影子组排在最前 |
| `root/etc/init.d/shadowsocksr` | 新增 `apply_clash_autoselect()`：mihomo 启动后等 external-controller 就绪，按映射表逐个 `PUT /proxies/<group>` 切到自动组；组名（含中文/emoji）预先百分号编码。任何一步失败只记日志，不影响已经起来的代理 |
| `luasrc/model/cbi/shadowsocksr/servers.lua` | `mihomo_urltest` 改称「Clash 自动切换」；新增 `mihomo_autoselect_apply`（每次启动后自动应用，默认开） |
| `root/usr/share/shadowsocksr/shadowsocksr.config` | 新增 `mihomo_autoselect_apply` 默认值，移除 `mihomo_auto_regions*` |
| `po/zh_Hans/ssr-plus.po` | 文案更新 |
| `Makefile` | `PKG_RELEASE` `1402` → `1403`（编码规则见 §1） |

### ③ 精简：移除智能地区分组与自定义分组（1403）

地区识别本就该由 subconverter 干，塞在路由器脚本里既臃肿又难维护（约 500 行地区关键词表）。
按「能精简就精简」的原则整体下线，代码可从 git 历史找回：

* `root/usr/share/shadowsocksr/clash_yaml.lua`：`REGION_DEFS` / `REGION_MATCHERS` / `region_of()` /
  `has_node_subgroups()` / `read_custom_groups()` / `collect_region_buckets()` / `inject_smart_groups()`
* `luasrc/controller/shadowsocksr.lua`：`clash_custom_groups` / `clash_custom_group_save` /
  `clash_custom_group_delete` 三个 RPC 及 uci `custom_group` 相关读写
* `luasrc/view/shadowsocksr/clash_groups_ui.htm`：「分组管理」弹窗与配套样式
* `po/zh_Hans/ssr-plus.po`：清理失效条目

累计净减约 1200 行；`clash_groups_ui.htm` 从 1851 行降到约 1230 行。

### ④ 统一 Clash 面板：左侧三页签（1403）

原来「Mihomo 面板」弹窗只有策略组一块内容，客户端规则要从主页面另一个按钮进，组件更新则是
LuCI 侧边栏的独立页面 —— 三处来回跳。现在合并成一个弹窗，左侧导航只保留三项：
**代理组 / 客户端规则 / 组件更新**（不做 URL 过滤、证书管理等与 Mihomo 面板无关的入口）。

| 文件 | 改动 |
|---|---|
| `luasrc/view/shadowsocksr/clash_main_panel.htm` | 弹窗主体改为「左导航 + 右页签」两栏：`PANES` / `MODE_BUTTONS` / `MODE_LOADERS` 三个表驱动切换，标题栏按钮跟着页签走（只有规则页显示 新增/导入/导出/清空/保存）。窄屏（≤900px）自动退化成顶部横排 |
| 同上 | 组件更新页签直接 `<%+shadowsocksr/component%>` 复用现有页面，**不复制代码**；配套 CSS 把它套进 `scui` 设计变量，与卡片流观感一致、跟随明暗主题 |
| `luasrc/view/shadowsocksr/component.htm` | 增加懒加载：`window.ssrComponentLazy` 为真时不自动请求，改由面板在切到该页签时调 `window.ssrComponentInit()`；升级回调末尾挂 `window.ssrComponentPostUpgrade` 钩子 |

**保存后即时生效**：客户端规则保存 / 清空时，若该节点正是当前主节点，后端本来就会
`/etc/init.d/shadowsocksr restart`（`reapplied` 字段回传给前端显示「已保存并生效」）。
组件升级完成后走同一个钩子自动 reload —— 内核（xray / mihomo / naiveproxy）与 Geo 库
调 `clash_refresh` 重载服务，主程序（LuCI 包）则刷新页面。

### ⑤ 排障增强（1402 引入 → 1403 重写时丢失 → 1901 重新并入）

| 文件 | 改动 |
|---|---|
| `root/etc/init.d/shadowsocksr` | `ln_start_bin()` 支持 `SSR_BIN_LOG`（原本一律 `>/dev/null 2>&1`，核心启动即退出时日志里一行线索都没有）；新增 `start_mihomo_with_log()`：留一份输出 + 启动后回看进程是否存活，挂了就把真实原因逐行写进日志并返回失败，替代原来无条件的 `Mihomo Started!`。单节点与 `type=clash` 两条启动路径都走它 |
| `root/usr/share/shadowsocksr/update_components.sh` | `v2ray_geoip_upgrade()` / `v2ray_geosite_upgrade()` 补 `mkdir -p` 目标目录——只装了 mihomo、没装 xray-core 的机器上 `/usr/share/v2ray` 不存在，下载成功也会 `cp` 失败 |

> 1403 重写 Clash 面板时把这一批改动整段漏掉了（表现为 mihomo 起不来、日志里却一行原因都没有）。
> 1901 在同步上游 v196.19 的同时把它重新合了回来，并保留 1403 的 `apply_clash_autoselect()` 调用。

### ⑥ 打包可执行位修复（1902）

| 文件 | 改动 |
|---|---|
| `tools/build.py` | 修复 `git_file_modes()` 查表键缺失包名前缀：本仓库（包根=仓库根）模式下 `git ls-files -s .` 返回 `root/etc/init.d/shadowsocksr`，而 `stage_tree()` 的键是 `luci-app-ssr-plus/root/…`，查表必然落到默认 `0644`。monorepo（helloworld）模式路径恰好带前缀，所以此前未暴露 |
| `tools/build.py` | 新增 `check_exec_bits()` 语义校验：`etc/init.d/*` 与 `usr/share/shadowsocksr/*.sh` 必须带可执行位——原来只比对「包内 == stage」，两边同时丢位也能通过；`verify_apk()` 同步补上权限比对 |
| `tools/build.py` | `verify_ipk()` 改为直接读 tar 成员清单，不再 `extractall`（部分托管 Python 环境会拦截 `os.mkdir`，且更快） |

> 事故表现：1901 的 ipk 装上后 `opkg` 一切正常，但 `/etc/init.d/shadowsocksr` 是 `0644`，
> LuCI 显示「ShadowsocksR Plus+ 未运行」、点启动直接 `Permission denied`；
> `gfw2ipset.sh` / `chinaipset.sh` 同样不可执行，即使手动 chmod 启动也会刷一批 Permission denied 日志。
> 修复后本仓库与 monorepo 两条打包路线产出的包，init.d 与脚本都是 `0755`。

### ⑦ 主程序在线升级源改指本仓库（1903）

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/update_components.sh` | 新增常量 `MAINPROGRAM_REPO="xiaodandaodao/luci-app-ssr-plus"` 与 `get_mainprogram_latest_tag()`；`get_mainprogram_latest_info()` 由「取上游 `fw876/helloworld` 最新 tag + 其 Release 资产」改为「取本仓库最新 Release 的 tag + 资产」，asset 列表解析也放宽为通用 `/owner/repo/releases/download/<tag>/<asset>` 形式 |
| `luasrc/view/shadowsocksr/component.htm`、`po/zh_Hans/ssr-plus.po`、`po/templates/ssr-plus.pot` | 主程序条目的说明文案由「from fw876/helloworld releases」改为「from the xiaodandaodao/luci-app-ssr-plus GitHub releases」 |

> Xray / Mihomo / NaiveProxy 三个内核的二进制包仍由上游 `fw876/helloworld` Release 提供（本仓库不构建它们），
> 因此只有主程序这一条的源地址被替换。效果：「组件更新 → SSR Plus+ Main Program → 在线升级」
> 会从本仓库的 latest Release 取 `luci-app-ssr-plus_<ver>_all.ipk` 或 `luci-app-ssr-plus-<ver>.apk` 并安装。

### ⑧ 组件下载链路加固 + 死镜像替换（1903）

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/update_components.sh` | 新增 `download_url_once()` / `fetch_text_once()` / `github_proxy_urls()` / `jsdelivr_alt_url()`；`download_file()` 与 `fetch_text()` 在原地址（含用户所选镜像）失败后，自动依次改走 jsdelivr release 分支镜像与 GitHub 加速代理（`ghfast.top` / `ghproxy.net` / `ghproxy.cc` / `gh-proxy.com`，可用 `GH_PROXY_HOSTS` 覆盖）。慢速/被墙时不再直接 `Download failed` |
| 同上 | 镜像项 `ghproxy` 指向的 `mirror.ghproxy.com` 已停止服务（实测 `SSL connection timeout`），改为 `ghproxy.net` |
| `luasrc/view/shadowsocksr/component.htm`、`luasrc/model/cbi/shadowsocksr/component.lua` | 镜像下拉的显示名同步为 `ghproxy.net` / `testingcf.jsdelivr.net`（option 值与 uci 存储不变，兼容旧配置） |

> 起因：V2Ray GeoIP / V2Ray GeoSite 两个数据源「没法安装更新」。实测**源本身没问题**
> （`Loyalsoldier/geoip` 的 `geoip-only-cn-private.dat` 与 `Loyalsoldier/v2ray-rules-dat` 的 `geosite.dat`
> 都存在且可下载，jsdelivr `@release` 镜像也正常），卡点在下载通道：GitHub Release 资产会 302 到
> `objects.githubusercontent.com`，直连经常超时；而镜像选项里的 `mirror.ghproxy.com` 已经彻底死掉，
> 选中它等于所有组件都下不动。现在无论用户选哪个镜像，直连失败都会自动降级到可用通道，
> `/usr/share/v2ray/` 也不存在时由升级流程自动创建（1901 已修，本次复验通过）。

> 上游 `po/zh-cn/` 目录（同内容、旧命名）未同步改动，属已知差异，不影响 lmo 生成（本仓库用 `po/zh_Hans/`）。

### ⑨ 自定义节点组（轻量版）+ 面板小优化（未发版，随下次打包）

机场给的分组是按地区/流媒体切好的，用户真正想用的组合（「只留这几个香港节点」）订阅里没有。
§③ 曾经做过一版自定义分组（uci 存储 + 弹窗，约 500 行），因为臃肿被整体下线；这一版按
「代码量尽量少」重做，**约 220 行**，存储从 uci 换成一行文本，去掉全部地区识别逻辑。

三个设计取舍：

* **存储用纯文本而非 uci**：`/etc/ssrplus/custom_groups.conf`，一行一组
  `组名 \t select|url-test \t 关键字1,关键字2`。节点名带 emoji，塞进 uci 的 ini 语法容易踩坑；
  纯文本读写各 5 行，也方便手工编辑。
* **匹配用「关键字包含」而非节点名全等**：订阅一更新节点编号就变（`🇭🇰 Hong Kong | 07` → `| 08`），
  写死全等下次更新就空了；关键字 `hong kong` 永远命中。精确节点名本身就是关键字的子集，
  两种用法都不需要额外分支。
* **保存后热重载，不重启**：mihomo 原生 `PUT /configs?force=true` + `{"path": ...}`，
  实测 1 秒内生效、已有连接不中断（HTTP 204）。只有接口返回非 2xx 才兜底 `init.d restart`。

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/clash_yaml.lua` | 新增 `inject_custom_groups()`：读配置文件 → 按关键字从 `proxies` 里挑成员 → 生成 `select` / `url-test` 组插到 `proxy-groups` **最前**。放在 `inject_autoselect_groups()` 之后，自定义组不会再被挂一层影子组；成员复用既有的 `selectable_member()` 过滤伪节点；匹配不到节点的组直接跳过（空组会让 mihomo 拒绝整份配置）；组名冲突走 `make_unique_name()` |
| `luasrc/controller/shadowsocksr.lua` | 新增 `clash_custom_groups`（返回可选真实节点 + 已定义组及其命中数）与 `clash_custom_group_save`（写文件 → `merge` 重新生成 → `append_client_policy_rules` → 热重载）。另加 `is_info_node()`：`build_clash_group_view()` 里过滤掉 `Expire:` / `Traffic:` 这类伪节点，面板不再把它们显示成可选出口 |
| `luasrc/view/shadowsocksr/clash_groups_ui.htm` | 工具条「+ 自定义组」按钮 + 编辑区（组名、类型下拉、可手工改的关键字框、带筛选的节点勾选列表、已有组 chip 可改可删）。复用现有 `scui-*` 样式，节点 chip 选中态直接借 `is-current` |
| `luasrc/view/shadowsocksr/clash_main_panel.htm`、`clash_panel.htm` | 传入 `customGroups` / `customSave` 两个接口地址 |
| `root/etc/init.d/shadowsocksr` | overlay 加 `log-level: warning`（原来每条 TCP 连接写一行，实测日志 729KB 且持续增长）；新增 `clean_orphan_clash_cache()`，删掉不属于任何 clash 节点的旧订阅缓存（旧版按订阅链接 md5 命名的文件永远读不到，实测白占 1.2MB flash），`config_foreach` 拿不到节点列表时直接返回、一个都不删 |
| `po/zh_Hans/ssr-plus.po`、`po/templates/ssr-plus.pot` | 新增 15 条文案 |

> 热重载时 mihomo 日志会有一条 `Start TProxy server error: address already in use`。
> 这是**既有配置**导致的：overlay 里 `redir-port` 与 `tproxy-port` 都设为同一个端口，
> 后绑定的那个必然冲突。与本次改动无关，功能不受影响（实测重载后 12 条活跃连接正常、
> 端口仍在监听），因此未在本批改动里调整端口配置。

## 3. 本仓库新增的非上游文件

| 路径 | 说明 | 来源 / 许可 |
|---|---|---|
| `tools/build.py` | 在非 Linux 环境复刻 OpenWrt buildroot 的 ipk/apk 打包行为（`luci.mk` 安装规则 + `package.mk` 的 control/脚本模板），产物与源码树回环校验 | 本项目自研 |
| `tools/deps.sh` | 现场获取 `po2lmo` 与 `apk-static` 两个外部二进制 | 本项目自研 |
| `tools/sfh_hash.c` | **摘录自** `openwrt/luci` 的 `modules/luci-base/src/lib/lmo.c`（原作者 Paul Hsieh 的 hash 实现），单独成文件以摆脱 lemon 生成 `plural_formula.h` 的依赖 | Apache-2.0 |
| `tools/verify.sh` | 一键静态校验（Lua / shell / PO / 模板 / jsdom 冒烟） | 本项目自研 |
| `tools/check_luci_template.py` | LuCI `.htm` 模板校验：按 LuCI parser 规则重建 Lua chunk 解析 + 抽出 `<script>` 用 node 校验 JS | 本项目自研 |
| `tools/build_preview.py` | 把面板模板渲染成可离线打开的预览页（含明暗主题） | 本项目自研 |
| `tools/smoke_test.js` | jsdom 无头冒烟测试（25 项断言，可抓 `node --check` 抓不到的 TEXT 漏键 → 按钮显示 `undefined` 之类） | 本项目自研 |
| `tools/inject_pkg.py` | 无源码注入工具：把改动注入别人预编译的 ipk/apk（`make-payload` / `show` / `apply`） | 本项目自研 |
| `patches/` | 相对上游 v196.19 的统一 diff + 移植说明 | 本项目自研 |
| `payload/` | 注入载荷（9 个文件 + manifest.json），接收方无需源码 | 本项目自研 |

## 4. 构建期下载的第三方二进制（不入库）

由 `tools/deps.sh` 现场获取，仅供构建使用：

| 组件 | 来源 | 用途 / 许可 |
|---|---|---|
| `po2lmo` | 编译自 `openwrt/luci` → `modules/luci-base/src/po2lmo.c` + `lib/lmo.h` | `.po` → `.lmo`，Apache-2.0 |
| `apk`（apk-static） | Alpine Linux 官方仓库的 `apk-tools` 包 | 生成 `.apk`，GPL-2.0 |

## 5. 说明

- 本仓库**不是**上游官方发布渠道，产出物仅供自用/分发参考；请优先支持上游作者。
- 上游若更新，建议按仓库 README 的「同步上游」流程 rebase 并重放上述 14 个文件的改动。
- 若本仓库内容对上游权利人有任何不妥，可随时移除相关衍生部分。
