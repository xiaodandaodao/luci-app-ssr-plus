#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 LuCI 模板 clash_groups_ui.htm 渲染成一份可离线打开的静态预览页。

不改路由器也能直接看到 Mihomo 面板的最终视觉与交互（含明暗主题切换）。
用法：build_preview.py [--tpl 模板] [--out 目标 html]
"""
import argparse
import json
import os
import re

TAG = re.compile(r"<%([=+:#-]?)(.*?)%>", re.S)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TPL = os.path.join(REPO, "luasrc", "view", "shadowsocksr", "clash_groups_ui.htm")

ZH = {
    "Ready.": "就绪。", "Loading...": "加载中…", "Testing delay...": "正在测速…",
    "Delay test failed.": "测速失败。", "Switching...": "正在切换…",
    "Switched successfully.": "切换成功。", "Switch failed.": "切换失败。",
    "This Clash total node is not active. Apply this node first.": "当前 Clash 总节点未启用，请先应用该节点。",
    "No switchable proxy groups found.": "未找到可切换的策略组。",
    "No groups match the filter.": "没有匹配的策略组。", "Timeout": "超时",
    "Current:": "当前：", "Auto Selected:": "自动选择：", "Effective Node:": "实际出口：",
    "Speed Test": "测速", "Test All Groups": "全部测速", "Refresh": "刷新",
    "Filter groups...": "筛选策略组…", "Proxy Groups": "策略组", "Nodes": "节点",
    "Show all %d nodes": "展开全部 %d 个节点", "Show less": "收起",
    "Expand": "展开", "Collapse": "收起", "Switch to this node": "切换到该节点",
    "Fastest:": "最快：", "Selector": "选择器", "Direct": "直连", "Reject": "拒绝",
    "Proxy": "代理", "Domestic": "国内", "GlobalTV": "国际流媒体", "AsianTV": "亚洲流媒体",
    "Others": "其他", "GLOBAL": "全局",     "url-test": "自动测速", "fallback": "故障转移",
    "load-balance": "负载均衡", "relay": "中继", "Compatible": "兼容", "Pass": "放行",

    # 分组管理 / 自定义分组（r16+）
    "Group Manager": "分组管理", "Custom Groups": "自定义分组", "New Group": "新建分组",
    "Group Name": "分组名称", "e.g. My Hong Kong": "例如：我的香港节点",
    "Mode": "模式", "Auto (fastest)": "自动（最快）", "Manual": "手动选择",
    "Select Nodes": "选择节点", "Select All": "全选", "Clear": "清空",
    "Selected: %d": "已选：%d 个", "%d nodes": "%d 个节点",
    "No custom groups yet.": "还没有自定义分组。",
    "No nodes match the filter.": "没有匹配的节点。", "Filter nodes...": "筛选节点…",
    "Close": "关闭", "Edit": "编辑", "Delete": "删除", "Save": "保存", "Cancel": "取消",
    "Delete this custom group?": "确定删除这个自定义分组？",
    "Group name is required.": "请填写分组名称。",
    "Pick at least one node.": "请至少选择一个节点。",
    "Custom group saved.": "自定义分组已保存。",
    "Custom group saved and applied.": "自定义分组已保存并生效。",
    "Custom group deleted.": "自定义分组已删除。",
    "Custom group deleted and applied.": "自定义分组已删除并生效。",
    "Failed to save the custom group.": "保存自定义分组失败。",
    "Failed to delete the custom group.": "删除自定义分组失败。",
    "Region auto-grouping is ON": "地区自动分组：已开启",
    "Region auto-grouping is OFF": "地区自动分组：已关闭",
    "Nodes are grouped by region (at least %d nodes each); every region group picks its own fastest node, and the group names are added to each route group.":
        "按地区把节点自动分组（每组至少 %d 个节点）；每个地区组自动选择该地区最快的节点，"
        "组名会被加入各分流组的成员里，分流组可一次性选中整个地区。",
    'Turn on "Mihomo Smart Grouping and Auto Select" on the Servers page first.':
        "请先到「服务器」页开启「Mihomo 智能分组与自动选择」。",
}


def render(src):
    def sub(m):
        prefix, body = m.group(1), m.group(2)
        if body.endswith("-"):
            body = body[:-1]
        if prefix in ("", "-"):
            return ""
        if prefix == "=":
            return "0"
        if prefix == ":":
            return ZH.get(body, body)
        return ""
    return TAG.sub(sub, src)


def member(name, delay, type_="Shadowsocks", leaf=None, leaf_delay=None, auto=False, is_group=False):
    return {"name": name, "type": type_, "delay": delay, "leaf": leaf,
            "leaf_delay": leaf_delay, "is_group": is_group, "auto": auto}


def auto_member(leaf, leaf_delay):
    return member("自动选择", None, "url-test", leaf, leaf_delay, True, True)


def group(name, type_, now, members, resolved=None, resolved_delay=None, delay=None):
    return {
        "name": name, "type": type_, "now": now,
        "all": [m["name"] for m in members], "members": members,
        "resolved": resolved if resolved is not None else now,
        "resolved_delay": resolved_delay, "delay": delay,
        "auto": type_ in ("url-test", "fallback", "load-balance", "relay", "smart"),
    }


def build_mock():
    """构造贴近真实订阅结构的假数据，含快/中/慢/超时四类延迟。"""
    gs = []

    gs.append(group("策略组 A", "Selector", "节点 SG-01", [
        auto_member("节点 SG-01", 55),
        member("节点 HK-01", 57),
        member("节点 JP-01", 78),
        member("节点 TW-01", 103),
        member("节点 AU-01", 139),
        member("节点 US-01", 273),
        member("节点 DE-01", None),
    ], resolved="节点 SG-01", resolved_delay=55, delay=55))

    gs.append(group("策略组 B", "Selector", "节点 TW-01", [
        member("节点 HK-01", 57),
        member("节点 TW-01", 103),
        auto_member("节点 SG-01", 55),
    ], resolved="节点 TW-01", resolved_delay=103, delay=103))

    gs.append(group("自动选优", "url-test", "节点 US-05", [
        auto_member("节点 US-05", 7230),
        member("节点 US-02", 812),
        member("节点 SG-02", 64),
        member("节点 JP-02", 121),
        member("节点 KR-01", 187),
        member("节点 UK-01", 455),
        member("节点 CA-01", 1024),
        member("节点 FR-01", 1330),
        member("节点 BR-01", None),
    ], resolved="节点 US-05", resolved_delay=7230, delay=7230))

    gs.append(group("故障转移", "fallback", "节点 JP-03", [
        auto_member("节点 JP-03", 92),
        member("节点 SG-03", 71),
        member("节点 HK-02", 148),
        member("节点 TW-02", None),
    ], resolved="节点 JP-03", resolved_delay=92, delay=92))

    gs.append(group("直连 / 拒绝", "Selector", "DIRECT", [
        member("DIRECT", 1),
        member("REJECT", 1),
    ], resolved="DIRECT", resolved_delay=1, delay=1))

    gs.append(group("长列表组（演示折叠）", "Selector", "节点 HK-03", [
        member("节点 HK-03", 43),
        member("节点 SG-04", 66),
        member("节点 JP-04", 88),
        member("节点 US-03", 210),
        member("节点 UK-02", 320),
        member("节点 DE-02", 410),
        member("节点 FR-02", 520),
        member("节点 NL-01", 610),
        member("节点 CA-02", 780),
        member("节点 AU-02", 900),
        member("节点 KR-02", 1100),
        member("节点 IN-01", 1250),
        member("节点 BR-02", 1400),
        member("节点 ZA-01", 2600),
        member("节点 RU-01", None),
    ], resolved="节点 HK-03", resolved_delay=43, delay=43))

    return {
        "active": True,
        "sid": "demo",
        "test_url": "https://www.gstatic.com/generate_204",
        "test_interval": "300",
        "groups": gs,
    }


# 分组管理弹窗用的节点池 + 已存自定义分组（贴近真实 payload）
GROUP_NODES = [
    "节点 HK-01", "节点 HK-02", "节点 HK-03", "节点 SG-01", "节点 SG-02", "节点 SG-03",
    "节点 JP-01", "节点 JP-02", "节点 JP-03", "节点 TW-01", "节点 TW-02", "节点 KR-01",
    "节点 KR-02", "节点 US-01", "节点 US-02", "节点 US-03", "节点 UK-01", "节点 UK-02",
    "节点 DE-01", "节点 DE-02", "节点 FR-01", "节点 FR-02", "节点 NL-01", "节点 CA-01",
    "节点 CA-02", "节点 AU-01", "节点 AU-02", "节点 IN-01", "节点 BR-01", "节点 BR-02",
    "节点 ZA-01", "节点 RU-01",
]


def build_group_manager_mock():
    return {
        "active": True,
        "auto_regions": "1",
        "auto_regions_min": "2",
        "nodes": GROUP_NODES,
        "groups": [
            {"id": "cg1", "name": "港新低延迟", "mode": "auto", "enabled": True,
             "nodes": ["节点 HK-01", "节点 HK-02", "节点 HK-03", "节点 SG-01", "节点 SG-02", "节点 SG-03"]},
            {"id": "cg2", "name": "流媒体备用", "mode": "manual", "enabled": True,
             "nodes": ["节点 JP-01", "节点 US-02", "节点 UK-01"]},
        ],
    }


PAGE = """<!doctype html>
<html lang="zh-CN" data-darkmode="false">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mihomo 策略组面板 · 预览</title>
<style>
  html, body { margin: 0; padding: 0; }
  body {
    background: #eef0f5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
    color: #1c2130;
  }
  html[data-darkmode="true"] body { background: #0b0d12; color: #eceff5; }
  .pv-shell { max-width: 1080px; margin: 0 auto; padding: 28px 20px 60px; }
  .pv-head { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
  .pv-title { font-size: 17px; font-weight: 650; letter-spacing: .2px; }
  .pv-sub { font-size: 12.5px; opacity: .6; }
  .pv-spacer { flex: 1 1 auto; }
  .pv-toggle {
    font: inherit; font-size: 12.5px; padding: 6px 14px; border-radius: 999px;
    border: 1px solid #d5d9e4; background: #fff; color: #1c2130; cursor: pointer;
  }
  html[data-darkmode="true"] .pv-toggle { background: #171a21; border-color: #353c4a; color: #eceff5; }
  .pv-card {
    background: #fff; border: 1px solid #e2e5ec; border-radius: 16px;
    box-shadow: 0 18px 44px -30px rgba(20,26,48,.5); overflow: hidden;
  }
  html[data-darkmode="true"] .pv-card { background: #13161c; border-color: #262b36; }
  .pv-card-body { padding: 14px; }
  .pv-note { margin-top: 14px; font-size: 12px; opacity: .55; line-height: 1.7; }
</style>
__COMPONENT_STYLE__
</head>
<body>
<div class="pv-shell">
  <div class="pv-head">
    <div>
      <div class="pv-title">Mihomo 策略组面板</div>
      <div class="pv-sub">luci-app-ssr-plus 196-r1301 · 静态预览（离线渲染，数据为模拟）</div>
    </div>
    <div class="pv-spacer"></div>
    <button class="pv-toggle" id="pv-theme" type="button">切换深色</button>
  </div>
  <div class="pv-card">
    <div class="pv-card-body">
      <div class="ssr-clash-ui" id="pv-wrap">
        <div id="pv-status" class="scui-status"></div>
        <div id="pv-mount"></div>
      </div>
    </div>
  </div>
  <div class="pv-note">
    交互可用：筛选框按组名/节点名实时过滤 · 单组测速 · 全部测速 · 点击节点切换 · 展开/收起长列表 ·
    点右上「分组管理」可试用地区自动分组状态与自定义分组的增删改。
  </div>
</div>

<script type="text/javascript">
__COMPONENT_SCRIPT__
</script>

<script type="text/javascript">
(function() {
  var MOCK = __MOCK_JSON__;

  // ---------------------------------------------------------- 假 XHR
  function jitter(value) {
    if (value === null || value === undefined) { return value; }
    var n = Number(value);
    if (!isFinite(n) || n <= 1) { return n; }
    // 偶发超时，模拟真实测速里的 no-data
    if (Math.random() < 0.06) { return null; }
    return Math.max(8, Math.round(n * (0.75 + Math.random() * 0.6)));
  }

  function freshMock() {
    var copy = JSON.parse(JSON.stringify(MOCK));
    copy.groups.forEach(function(g) {
      (g.members || []).forEach(function(m) {
        m.delay = jitter(m.delay);
        if (m.leaf_delay !== null && m.leaf_delay !== undefined) { m.leaf_delay = jitter(m.leaf_delay); }
      });
      if (g.resolved_delay !== null && g.resolved_delay !== undefined) { g.resolved_delay = jitter(g.resolved_delay); }
      if (g.delay !== null && g.delay !== undefined) { g.delay = jitter(g.delay); }
    });
    return copy;
  }

  // 分组管理：在内存里模拟一份可增删改的数据
  var GM = __GM_MOCK_JSON__;

  var stubXHR = {
    get: function(url, params, cb) {
      var u = String(url);
      var payload;
      if (u.indexOf('customGroups') >= 0) {
        payload = JSON.parse(JSON.stringify(GM));        // 深拷贝，避免被面板改动污染
      } else if (u.indexOf('delay') >= 0) {
        payload = { success: true, delays: {} };
      } else {
        payload = freshMock();
      }
      window.setTimeout(function() { cb(null, payload); }, 160);
    },
    post: function(url, params, cb) {
      var u = String(url);
      window.setTimeout(function() {
        if (u.indexOf('customGroupDelete') >= 0) {
          GM.groups = GM.groups.filter(function(g) { return g.id !== params.id; });
          cb(null, { success: true, groups: GM.groups });
        } else if (u.indexOf('customGroupSave') >= 0) {
          var nodes = [];
          Object.keys(params).forEach(function(k) {
            if (k.indexOf('node_') === 0) { nodes.push(params[k]); }
          });
          nodes.sort(function(a, b) {
            var ia = parseInt(a.replace('node_', ''), 10), ib = parseInt(b.replace('node_', ''), 10);
            return ia - ib;
          });
          var group = { id: params.id || ('cg' + (GM.groups.length + 10)), name: params.name,
                        mode: params.mode, enabled: params.enabled !== '0', nodes: nodes };
          var hit = -1;
          GM.groups.forEach(function(g, i) { if (g.id === group.id) { hit = i; } });
          if (hit >= 0) { GM.groups[hit] = group; } else { GM.groups.push(group); }
          cb(null, { success: true, groups: GM.groups });
        } else {
          cb(null, { success: true });
        }
      }, 120);
    }
  };

  var ui = window.ssrClashUI.create({
    mount: document.getElementById('pv-mount'),
    statusEl: document.getElementById('pv-status'),
    sid: 'demo',
    xhr: stubXHR,
    urls: {
      groups: 'preview://groups',
      switch: 'preview://switch',
      delayTest: 'preview://delay',
      customGroups: 'preview://customGroups',
      customGroupSave: 'preview://customGroupSave',
      customGroupDelete: 'preview://customGroupDelete'
    }
  });

  // 测速/切换走本地模拟：延迟随机抖动，让画面动起来
  var origLoad = ui.load;
  ui.load = origLoad;

  document.getElementById('pv-theme').onclick = function() {
    var html = document.documentElement;
    var dark = html.getAttribute('data-darkmode') === 'true';
    html.setAttribute('data-darkmode', dark ? 'false' : 'true');
    this.textContent = dark ? '切换深色' : '切换浅色';
  };

  ui.load();
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tpl", default=DEFAULT_TPL)
    ap.add_argument("--out", default=os.path.join(REPO, "out", "panel-preview.html"))
    args = ap.parse_args()

    src = open(args.tpl, encoding="utf-8").read()
    rendered = render(src)

    style = re.search(r"<style[^>]*>(.*?)</style>", rendered, re.S).group(1)
    script = re.search(r'<script[^>]*>(.*?)</script>', rendered, re.S).group(1)

    page = (PAGE.replace("__COMPONENT_STYLE__", style)
                .replace("__COMPONENT_SCRIPT__", script)
                .replace("__MOCK_JSON__", json.dumps(build_mock(), ensure_ascii=False))
                .replace("__GM_MOCK_JSON__", json.dumps(build_group_manager_mock(), ensure_ascii=False)))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print("preview written: %s (%d bytes)" % (args.out, len(page)))


if __name__ == "__main__":
    main()
