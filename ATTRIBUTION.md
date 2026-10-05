# 来源与改动说明（ATTRIBUTION）

本仓库是 [`luci-app-ssr-plus`](https://github.com/fw876/helloworld) 的**衍生构建仓库**：
上游源码 + 本文列出的功能改动 + 一套自持的打包/校验工具链。
所有上游代码的权利归原作者，许可证见 [LICENSE](./LICENSE)（GPL-3.0）。

## 1. 上游基线

| 项目 | 值 |
|---|---|
| 上游仓库 | `https://github.com/fw876/helloworld` |
| 基线 tag | **v196.13** |
| 基线 commit | `d88e25f`（xray-core: enable UPX best compression by default） |
| 包版本 | `luci-app-ssr-plus` `196-r17`（上游 v196.13 为 `196-r13`） |
| 许可证 | GPL-3.0（随仓库保留上游 `LICENSE` 原文，未改动） |

上游 monorepo 里 `luci-app-ssr-plus/` 是一个子目录，本仓库把它**提升为仓库根**：

```
上游 fw876/helloworld/luci-app-ssr-plus/   →   本仓库 /
  Makefile                                       Makefile
  luasrc/  po/  root/                            luasrc/  po/  root/
```

因此 `patches/` 里的补丁仍是**上游视角**的路径（`luci-app-ssr-plus/…`，用于打在上游 / fork 的 helloworld 树上），
若要直接打在本仓库根，请用 `patch -p2` / `git apply -p2`。

## 2. 相对上游的功能改动（10 个文件）

对应 `patches/ssrplus-smart-grouping-196-r17.patch`，三批改动：

### ① 面板延迟可视化

| 文件 | 改动 |
|---|---|
| `luasrc/controller/shadowsocksr.lua` | `clash_groups` RPC 扩充返回：`delays` 映射、每组成员延迟、`resolved`（顺 `now` 链递归到真实出口节点）；新增 `clash_delay_test` RPC（调 mihomo `GET /group/<name>/delay`，超时值 0 转为 false 表示超时） |
| `luasrc/view/shadowsocksr/clash_groups_ui.htm` | **新增**共享 UI 组件：渲染自动选择/当前选中、实际出口、成员延迟 chip（按延迟升序，绿<200ms / 黄<500ms / 橙<1s / 红≥1s / 灰无数据）、单组「测速」与「全部测速」 |
| `luasrc/view/shadowsocksr/clash_main_panel.htm` | 引入上述组件 + 注入 `urls:{}` |
| `luasrc/view/shadowsocksr/clash_panel.htm` | 同上（独立页面版） |

### ② url-test 自动选最快节点

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/clash_yaml.lua` | 为每个叶子组生成 url-test 孪生组并置顶（幂等），开关 `mihomo_urltest`（默认关） |
| `luasrc/model/cbi/shadowsocksr/servers.lua` | 新增开关选项及说明 |
| `root/usr/share/shadowsocksr/shadowsocksr.config` | 新增选项默认值 |

### ③ 智能地区分组 + 自定义分组

| 文件 | 改动 |
|---|---|
| `root/usr/share/shadowsocksr/clash_yaml.lua` | 核心：`inject_smart_groups()` / `REGION_DEFS` / `region_of()` / `has_node_subgroups()`。订阅 YAML 未自带节点分组时，按节点名里的地区关键词自动生成地区 url-test 组 + 全局自动选择组，并把组名注入各 select 分流组（ASCII 关键词按词边界匹配，中文按子串） |
| `luasrc/controller/shadowsocksr.lua` | 新增 `clash_custom_groups` / `clash_custom_group_save` / `clash_custom_group_delete` 三个 RPC，写 uci `custom_group` 并触发重启/reapply |
| `luasrc/view/shadowsocksr/clash_groups_ui.htm` | 工具栏新增「分组管理」：新建/编辑/删除分组（组名 + 节点多选 + 自动/手动模式），并显示地区分组运行状态 |
| `luasrc/model/cbi/shadowsocksr/servers.lua` | `mihomo_urltest` 重命名为「Mihomo 智能分组与自动选择」；新增 `mihomo_auto_regions`(默认 1) / `mihomo_auto_regions_min`(2) / `mihomo_auto_regions_max`(60) |
| `root/etc/init.d/shadowsocksr` | `prepare_clash_runtime_config()` 中调用 + 日志 |
| `root/usr/share/shadowsocksr/shadowsocksr.config` | 新增选项默认值 |
| `po/zh_Hans/ssr-plus.po` | 新增文案翻译 |
| `Makefile` | `PKG_RELEASE` `16` → `r17` |

> 上游 `po/zh-cn/` 目录（同内容、旧命名）未同步改动，属已知差异，不影响 lmo 生成（本仓库用 `po/zh_Hans/`）。

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
| `patches/` | 相对上游 v196.13 的统一 diff + 移植说明 | 本项目自研 |
| `payload/` | 注入载荷（9 个文件 + manifest.json），接收方无需源码 | 本项目自研 |

## 4. 构建期下载的第三方二进制（不入库）

由 `tools/deps.sh` 现场获取，仅供构建使用：

| 组件 | 来源 | 用途 / 许可 |
|---|---|---|
| `po2lmo` | 编译自 `openwrt/luci` → `modules/luci-base/src/po2lmo.c` + `lib/lmo.h` | `.po` → `.lmo`，Apache-2.0 |
| `apk`（apk-static） | Alpine Linux 官方仓库的 `apk-tools` 包 | 生成 `.apk`，GPL-2.0 |

## 5. 说明

- 本仓库**不是**上游官方发布渠道，产出物仅供自用/分发参考；请优先支持上游作者。
- 上游若更新，建议按仓库 README 的「同步上游」流程 rebase 并重放上述 10 个文件的改动。
- 若本仓库内容对上游权利人有任何不妥，可随时移除相关衍生部分。
