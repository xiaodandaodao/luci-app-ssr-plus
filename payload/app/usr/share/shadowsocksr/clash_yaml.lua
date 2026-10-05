#!/usr/bin/lua

require "nixio"
require "nixio.fs"
require "luci.model.uci"

local ok_lyaml, lyaml = pcall(require, "lyaml")
if not ok_lyaml then
	io.stderr:write("lyaml_not_found\n")
	os.exit(2)
end

local uci = require "luci.model.uci".cursor()
local ok_jsonc, jsonc = pcall(require, "luci.jsonc")

local function read_file(path)
	local data = nixio.fs.readfile(path)
	if not data or data == "" then
		return nil
	end
	return data
end

local function write_file(path, data)
	return nixio.fs.writefile(path, data)
end

local function load_yaml(path)
	local raw = read_file(path)
	if not raw then
		return nil, "read_failed"
	end

	local ok, parsed = pcall(lyaml.load, raw)
	if not ok or type(parsed) ~= "table" then
		return nil, "parse_failed"
	end

	return parsed
end

local function dump_yaml(path, data)
	local ok, rendered = pcall(lyaml.dump, { data })
	if not ok or not rendered then
		return nil, "dump_failed"
	end

	write_file(path, rendered)
	return true
end

local function split_filter_words(text)
	local items = {}
	for part in tostring(text or ""):gmatch("[^/]+") do
		if part ~= "" then
			items[#items + 1] = part
		end
	end
	return items
end

local function trim(value)
	return tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function parse_csv_line(line)
	local cols = {}
	local cur = ""
	local in_quote = false
	local i = 1

	while i <= #line do
		local ch = line:sub(i, i)
		if ch == '"' then
			if in_quote and line:sub(i + 1, i + 1) == '"' then
				cur = cur .. '"'
				i = i + 1
			else
				in_quote = not in_quote
			end
		elseif ch == "," and not in_quote then
			cols[#cols + 1] = cur
			cur = ""
		else
			cur = cur .. ch
		end
		i = i + 1
	end

	cols[#cols + 1] = cur
	return cols
end

local function read_clash_client_rules_csv(sid)
	local rows = {}
	sid = trim(sid)
	if sid == "" then
		return rows
	end

	local csv_path = string.format("/etc/ssrplus/clash/%s.csv", sid)
	local raw = read_file(csv_path)
	if not raw or raw == "" then
		return rows
	end

	local first = true
	for line in tostring(raw):gsub("\r", ""):gmatch("[^\n]+") do
		local text = trim(line)
		if text ~= "" then
			if first and text:lower() == "enabled,client,policy,remarks,client_mac" then
				first = false
			else
				local cols = parse_csv_line(line)
				if #cols >= 4 then
					rows[#rows + 1] = {
						enabled = cols[1],
						ip_addr = trim(cols[2] or ""),
						policy_group = trim(cols[3] or ""),
						remarks = trim(cols[4] or ""),
						client_mac = trim(cols[5] or "")
					}
				end
			end
		end
	end

	return rows
end

local function has_proxy_sections(doc)
	return type(doc.proxies) == "table" or type(doc["proxy-providers"]) == "table"
end

local function validate(path)
	local doc = load_yaml(path)
	if not doc then
		return false
	end
	return has_proxy_sections(doc)
end

local function filter(path, filter_words)
	local doc, err = load_yaml(path)
	if not doc then
		io.stderr:write(err or "parse_failed", "\n")
		return false
	end

	local words = split_filter_words(filter_words)
	if #words == 0 then
		return true
	end

	local removed = {}
	local proxies = {}
	for _, proxy in ipairs(doc.proxies or {}) do
		local name = tostring(proxy.name or "")
		local matched = false
		for _, word in ipairs(words) do
			if name:find(word, 1, true) then
				matched = true
				removed[name] = true
				break
			end
		end
		if not matched then
			proxies[#proxies + 1] = proxy
		end
	end
	doc.proxies = proxies

	for _, group in ipairs(doc["proxy-groups"] or {}) do
		if type(group.proxies) == "table" then
			local kept = {}
			for _, name in ipairs(group.proxies) do
				if not removed[tostring(name)] then
					kept[#kept + 1] = name
				end
			end
			group.proxies = kept
		end
	end

	local count = 0
	for _ in pairs(removed) do
		count = count + 1
	end

	dump_yaml(path, doc)
	io.stdout:write(tostring(count), "\n")
	return true
end

local function deep_merge(dst, src)
	if type(dst) ~= "table" or type(src) ~= "table" then
		return src
	end

	for k, v in pairs(src) do
		if type(v) == "table" and type(dst[k]) == "table" then
			dst[k] = deep_merge(dst[k], v)
		else
			dst[k] = v
		end
	end

	return dst
end

local function strip_runtime_conflicts(doc)
	doc.tun = nil
	doc.listeners = nil
	doc["redir-port"] = nil
	doc["tproxy-port"] = nil
	doc["socks-port"] = nil
	doc["mixed-port"] = nil
	doc.port = nil
	doc["external-controller"] = nil
	doc.secret = nil
	doc["allow-lan"] = nil
	if type(doc.dns) == "table" then
		doc.dns["fake-ip-range"] = nil
		doc.dns["fake-ip-filter"] = nil
	end
end

local function group_requires_candidates(group)
	local gtype = tostring(group and group.type or ""):lower()
	return gtype == "select"
		or gtype == "fallback"
		or gtype == "load-balance"
		or gtype == "url-test"
		or gtype == "relay"
end

local function has_nonempty_sequence(value)
	return type(value) == "table" and next(value) ~= nil
end

local function fill_empty_proxy_groups(doc)
	local changed = 0
	for _, group in ipairs(doc["proxy-groups"] or {}) do
		if type(group) == "table"
			and group_requires_candidates(group)
			and not has_nonempty_sequence(group.proxies)
			and not has_nonempty_sequence(group.use)
		then
			group.proxies = { "DIRECT" }
			changed = changed + 1
		end
	end
	return changed
end

-- ---------------------------------------------------------------------------
-- 智能分组：地区分组 + 自定义分组 + url-test 自动选择
--
-- 不少订阅的 Clash YAML 只有一个「大平铺」代理组（成员是全部节点），没有任何节点分组。
-- 这种情况下 url-test 自动选择只能在整个节点池里挑最快，用户没法「按地区自动选最快」。
--
-- 开启 mihomo_urltest 后：
--   1) 若 YAML 自带节点分组（存在「成员全是真实节点、且不含全部节点」的组），尊重原样；
--   2) 否则按节点名识别地区（香港01 / 香港02 → 中国香港），为每个地区生成一个 url-test 组
--      —— 组内自动选最快，用户只需在分流组里选「地区」即可；
--   3) 生成「🚀 自动选择（全部节点）」组，保留全池自动选最快的能力；
--   4) 读取 uci 里的自定义分组（custom_group section），同样生成组；
--   5) 把上述新组名插入每个 select 分流组的成员列表（真实节点之前），
--      于是每个分流组既能直接选地区，也能选自定义组。
-- ---------------------------------------------------------------------------

local URLTEST_DEFAULT_URL = "https://www.gstatic.com/generate_204"
local AUTO_SELECT_GROUP_NAME = "🚀 自动选择（全部节点）"
local REGION_FALLBACK_NAME = "🌐 其他"

-- 地区识别表：{ 显示名, 关键词(逗号分隔) }
-- 匹配时统一按「关键词长度降序」进行，长词优先，避免 US 命中 RUSSIA、IN 命中 Finland；
-- 纯 ASCII 关键词要求词边界，避免 IN 命中 Haidian、US 命中 Russia 这类误判。
local REGION_DEFS = {
	{ "🇨🇳 中国香港", "中国香港,香港,港区,港岛,九龙,新界,HongKong,Hong Kong,HKG,HK" },
	{ "🇨🇳 中国台湾", "中国台湾,台湾,台北,新北,台中,台南,高雄,彰化,Taiwan,Taipei,TW" },
	{ "🇨🇳 中国澳门", "中国澳门,澳门,Macau,Macao,MO" },
	{ "🇯🇵 日本", "日本,东京,大阪,埼玉,名古屋,Japan,Tokyo,Osaka,JP" },
	{ "🇰🇷 韩国", "韩国,首尔,韩区,Korea,Seoul,KR" },
	{ "🇸🇬 新加坡", "新加坡,狮城,Singapore,SG" },
	{ "🇺🇸 美国", "美国,洛杉矶,圣何塞,硅谷,西雅图,达拉斯,芝加哥,迈阿密,拉斯维加斯,凤凰城,纽约,United States,UnitedStates,LosAngeles,SanJose,Seattle,NewYork,Dallas,USA,US" },
	{ "🇬🇧 英国", "英国,伦敦,曼彻斯特,United Kingdom,Britain,London,GB,UK" },
	{ "🇩🇪 德国", "德国,法兰克福,柏林,Germany,Frankfurt,Berlin,DE" },
	{ "🇫🇷 法国", "法国,巴黎,France,Paris,FR" },
	{ "🇳🇱 荷兰", "荷兰,阿姆斯特丹,Netherlands,Amsterdam,NL" },
	{ "🇮🇹 意大利", "意大利,米兰,罗马,Italy,Milan,Rome,IT" },
	{ "🇪🇸 西班牙", "西班牙,马德里,Spain,Madrid,ES" },
	{ "🇹🇷 土耳其", "土耳其,伊斯坦布尔,Turkey,Istanbul,TR" },
	{ "🇷🇺 俄罗斯", "俄罗斯,莫斯科,俄区,Russia,Moscow,RU" },
	{ "🇨🇦 加拿大", "加拿大,多伦多,蒙特利尔,Canada,Toronto,CA" },
	{ "🇦🇺 澳大利亚", "澳大利亚,澳洲,悉尼,墨尔本,Australia,Sydney,Melbourne,AU" },
	{ "🇲🇾 马来西亚", "马来西亚,马来,吉隆坡,Malaysia,KualaLumpur,MY" },
	{ "🇵🇭 菲律宾", "菲律宾,马尼拉,Philippines,Manila,PH" },
	{ "🇮🇳 印度", "印度,孟买,班加罗尔,India,Mumbai,Bangalore,IN" },
	{ "🇮🇩 印尼", "印度尼西亚,印尼,雅加达,Indonesia,Jakarta,ID" },
	{ "🇹🇭 泰国", "泰国,曼谷,Thailand,Bangkok,TH" },
	{ "🇻🇳 越南", "越南,河内,胡志明,Vietnam,Hanoi,VN" },
	{ "🇧🇷 巴西", "巴西,圣保罗,Brazil,SaoPaulo,BR" },
	{ "🇦🇷 阿根廷", "阿根廷,Argentina,AR" },
	{ "🇲🇽 墨西哥", "墨西哥,Mexico,MX" },
	{ "🇦🇪 迪拜", "迪拜,阿联酋,酋长国,Dubai,Emirates,UAE,AE" },
	{ "🇪🇬 埃及", "埃及,Egypt,EG" },
	{ "🇮🇱 以色列", "以色列,Israel,IL" },
	{ "🇨🇭 瑞士", "瑞士,苏黎世,Switzerland,Zurich,CH" },
	{ "🇸🇪 瑞典", "瑞典,斯德哥尔摩,Sweden,Stockholm,SE" },
	{ "🇫🇮 芬兰", "芬兰,赫尔辛基,Finland,Helsinki,FI" },
	{ "🇳🇴 挪威", "挪威,Norway,Oslo,NO" },
	{ "🇩🇰 丹麦", "丹麦,Denmark,Copenhagen,DK" },
	{ "🇵🇱 波兰", "波兰,华沙,Poland,Warsaw,PL" },
	{ "🇺🇦 乌克兰", "乌克兰,Ukraine,Kyiv,Kiev,UA" },
	{ "🇮🇪 爱尔兰", "爱尔兰,都柏林,Ireland,Dublin,IE" },
	{ "🇦🇹 奥地利", "奥地利,维也纳,Austria,Vienna,AT" },
	{ "🇧🇪 比利时", "比利时,Belgium,Brussels,BE" },
	{ "🇨🇿 捷克", "捷克,Czech,Prague,CZ" },
	{ "🇷🇴 罗马尼亚", "罗马尼亚,Romania,Bucharest,RO" },
	{ "🇭🇺 匈牙利", "匈牙利,Hungary,Budapest,HU" },
	{ "🇬🇷 希腊", "希腊,Greece,Athens,GR" },
	{ "🇵🇹 葡萄牙", "葡萄牙,里斯本,Portugal,Lisbon,PT" },
	{ "🇿🇦 南非", "南非,约翰内斯堡,SouthAfrica,Johannesburg,ZA" },
	{ "🇨🇱 智利", "智利,Chile,Santiago,CL" },
	{ "🇵🇪 秘鲁", "秘鲁,Peru,Lima,PE" },
	{ "🇨🇴 哥伦比亚", "哥伦比亚,Colombia,Bogota,CO" },
	{ "🇵🇰 巴基斯坦", "巴基斯坦,Pakistan,Karachi,PK" },
	{ "🇳🇬 尼日利亚", "尼日利亚,Nigeria,Lagos,NG" },
	{ "🇳🇿 新西兰", "新西兰,奥克兰,NewZealand,Auckland,NZ" },
	{ "🇸🇦 沙特", "沙特,利雅得,Saudi,Riyadh,SA" },
	{ "🇰🇿 哈萨克斯坦", "哈萨克,哈萨克斯坦,Kazakhstan,KZ" },
	{ "🇲🇳 蒙古", "蒙古,Mongolia,MN" },
	{ "🇰🇭 柬埔寨", "柬埔寨,金边,Cambodia,KH" },
	{ "🇲🇲 缅甸", "缅甸,仰光,Myanmar,Yangon,MM" },
	{ "🇳🇵 尼泊尔", "尼泊尔,Nepal,NP" },
	{ "🇱🇰 斯里兰卡", "斯里兰卡,SriLanka,LK" },
	{ "🇧🇩 孟加拉", "孟加拉,Bangladesh,BD" },
	{ "🇨🇳 中国", "回国,大陆,上海,北京,广州,深圳,杭州,成都,重庆,武汉,南京,天津,苏州,China,CN" },
	{ REGION_FALLBACK_NAME, "" }
}

-- 一次性把 REGION_DEFS 展开成「关键词 → 地区」的有序匹配表
local REGION_MATCHERS = (function()
	local list = {}
	local seq = 0
	for order, def in ipairs(REGION_DEFS) do
		for keyword in tostring(def[2] or ""):gmatch("[^,]+") do
			keyword = trim(keyword)
			if keyword ~= "" then
				seq = seq + 1
				list[#list + 1] = {
					kw = keyword,
					low = keyword:lower(),
					ascii = keyword:match("^[%a%d]+$") ~= nil,
					name = def[1],
					order = order,
					seq = seq
				}
			end
		end
	end
	table.sort(list, function(a, b)
		if #a.kw ~= #b.kw then
			return #a.kw > #b.kw
		end
		if a.order ~= b.order then
			return a.order < b.order
		end
		return a.seq < b.seq
	end)
	return list
end)()

-- 从节点名推断地区显示名；识别不到返回 nil
local function region_of(name)
	local raw = tostring(name or "")
	if raw == "" then
		return nil
	end
	local low = raw:lower()

	for _, item in ipairs(REGION_MATCHERS) do
		if item.ascii then
			local from = 1
			while true do
				local s, e = low:find(item.low, from, true)
				if not s then
					break
				end
				local before = s > 1 and low:sub(s - 1, s - 1) or ""
				local after = low:sub(e + 1, e + 1)
				if not before:match("[%a%d]") and not after:match("[%a%d]") then
					return item.name
				end
				from = s + 1
			end
		elseif low:find(item.low, 1, true) then
			return item.name
		end
	end

	return nil
end

local function uci_subscribe_option(option, default)
	local value = uci:get_first("shadowsocksr", "server_subscribe", option)
	if value == nil or value == "" then
		return default
	end
	return value
end

local function get_urltest_options()
	if tostring(uci_subscribe_option("mihomo_urltest", "0")) ~= "1" then
		return nil
	end

	local url = tostring(uci_subscribe_option("mihomo_urltest_url", ""))
	if url == "" then
		url = tostring(uci_subscribe_option("url_test_url", ""))
	end
	if url == "" then
		url = URLTEST_DEFAULT_URL
	end

	local interval = tonumber(uci_subscribe_option("mihomo_urltest_interval", "300")) or 300
	if interval < 30 then
		interval = 30
	end

	local tolerance = tonumber(uci_subscribe_option("mihomo_urltest_tolerance", "50")) or 50
	if tolerance < 0 then
		tolerance = 0
	end

	return { url = url, interval = interval, tolerance = tolerance }
end

local function collect_proxy_names(doc)
	local names = {}
	for _, proxy in ipairs(doc.proxies or {}) do
		if type(proxy) == "table" then
			local name = tostring(proxy.name or "")
			if name ~= "" then
				names[name] = true
			end
		end
	end
	return names
end

local function collect_group_types(doc)
	local types = {}
	for _, group in ipairs(doc["proxy-groups"] or {}) do
		if type(group) == "table" then
			local name = tostring(group.name or "")
			if name ~= "" then
				types[name] = tostring(group.type or ""):lower()
			end
		end
	end
	return types
end

local function collect_used_names(doc)
	local used = {}
	for name in pairs(collect_proxy_names(doc)) do
		used[name] = true
	end
	for name in pairs(collect_group_types(doc)) do
		used[name] = true
	end
	return used
end

local function make_unique_name(base, used)
	local name = tostring(base or "")
	if name == "" then
		name = "Proxy Group"
	end
	if not used[name] then
		used[name] = true
		return name
	end
	local index = 2
	while used[name .. " " .. index] do
		index = index + 1
	end
	local unique = name .. " " .. index
	used[unique] = true
	return unique
end

-- YAML 是否已经自带节点分组：存在一个「成员全是真实节点、且不是全部节点」的组
local function has_node_subgroups(doc, proxy_names)
	local total = 0
	for _ in pairs(proxy_names) do
		total = total + 1
	end
	if total < 4 then
		return false
	end

	for _, group in ipairs(doc["proxy-groups"] or {}) do
		if type(group) == "table" and type(group.proxies) == "table" then
			local count = #group.proxies
			if count >= 2 and count < total then
				local all_nodes = true
				for _, member in ipairs(group.proxies) do
					if not proxy_names[tostring(member or "")] then
						all_nodes = false
						break
					end
				end
				if all_nodes then
					return true
				end
			end
		end
	end

	return false
end

-- 分组配置：mihomo_urltest 是总开关，mihomo_auto_regions 控制地区自动分组
local function get_smart_group_options()
	local urltest = get_urltest_options()
	if not urltest then
		return nil
	end

	local min_nodes = tonumber(uci_subscribe_option("mihomo_auto_regions_min", "2")) or 2
	if min_nodes < 1 then
		min_nodes = 1
	end

	local max_groups = tonumber(uci_subscribe_option("mihomo_auto_regions_max", "60")) or 60
	if max_groups < 1 then
		max_groups = 1
	end

	return {
		urltest = urltest,
		auto_regions = tostring(uci_subscribe_option("mihomo_auto_regions", "1")) == "1",
		min_nodes = min_nodes,
		max_groups = max_groups
	}
end

-- 读取 uci 里的自定义分组（config custom_group）
local function read_custom_groups()
	local list = {}
	local all = uci:get_all("shadowsocksr") or {}

	for _, section in pairs(all) do
		if type(section) == "table" and tostring(section[".type"] or "") == "custom_group" then
			local name = trim(section.name)
			if name ~= "" and tostring(section.enabled or "1") ~= "0" then
				local nodes = {}
				for part in tostring(section.nodes or ""):gmatch("[^,]+") do
					local value = trim(part)
					if value ~= "" then
						nodes[#nodes + 1] = value
					end
				end
				if #nodes > 0 then
					list[#list + 1] = {
						name = name,
						nodes = nodes,
						mode = (tostring(section.mode or "auto") == "manual") and "manual" or "auto"
					}
				end
			end
		end
	end

	table.sort(list, function(a, b)
		return tostring(a.name) < tostring(b.name)
	end)

	return list
end

-- 按地区把节点分桶（沿用 YAML 里节点的原始顺序）
local function collect_region_buckets(doc, proxy_names, min_nodes, max_groups)
	local buckets = {}
	local order = {}

	for _, proxy in ipairs(doc.proxies or {}) do
		if type(proxy) == "table" then
			local name = tostring(proxy.name or "")
			if name ~= "" and proxy_names[name] then
				local region = region_of(name) or REGION_FALLBACK_NAME
				if not buckets[region] then
					buckets[region] = {}
					order[#order + 1] = region
				end
				table.insert(buckets[region], name)
			end
		end
	end

	local list = {}
	for _, region in ipairs(order) do
		local members = buckets[region]
		if #members >= min_nodes then
			list[#list + 1] = { name = region, members = members }
		end
	end

	-- 节点多的地区排前面
	table.sort(list, function(a, b)
		if #a.members ~= #b.members then
			return #a.members > #b.members
		end
		return tostring(a.name) < tostring(b.name)
	end)

	if #list > max_groups then
		local trimmed = {}
		for index = 1, max_groups do
			trimmed[index] = list[index]
		end
		list = trimmed
	end

	return list
end

local function inject_smart_groups(doc)
	local smart = get_smart_group_options()
	if not smart then
		return 0, 0
	end

	local groups = doc["proxy-groups"]
	if type(groups) ~= "table" or #groups == 0 then
		return 0, 0
	end

	local proxy_names = collect_proxy_names(doc)
	local total_nodes = 0
	for _ in pairs(proxy_names) do
		total_nodes = total_nodes + 1
	end
	if total_nodes < 2 then
		return 0, 0
	end

	local used = collect_used_names(doc)
	local added_groups = {}
	local region_count = 0

	local function copy_members(members)
		-- 拷贝一份，避免多张表共享同一张表让 lyaml 输出 YAML 锚点/别名
		local copied = {}
		for index, member in ipairs(members) do
			copied[index] = member
		end
		return copied
	end

	local function add_group(base_name, gtype, members, extra)
		local group = {
			name = make_unique_name(base_name, used),
			type = gtype,
			proxies = copy_members(members)
		}
		if extra then
			for key, value in pairs(extra) do
				group[key] = value
			end
		end
		added_groups[#added_groups + 1] = group
		return group
	end

	local autotest_extra = {
		url = smart.urltest.url,
		interval = smart.urltest.interval,
		tolerance = smart.urltest.tolerance
	}

	-- 1) 地区自动分组：只在 YAML 没有自带节点分组时启用
	if smart.auto_regions and not has_node_subgroups(doc, proxy_names) then
		for _, bucket in ipairs(collect_region_buckets(doc, proxy_names, smart.min_nodes, smart.max_groups)) do
			add_group(bucket.name, "url-test", bucket.members, autotest_extra)
			region_count = region_count + 1
		end
	end

	-- 2) 自定义分组
	for _, custom in ipairs(read_custom_groups()) do
		local members = {}
		for _, node in ipairs(custom.nodes) do
			if proxy_names[node] then
				members[#members + 1] = node
			end
		end
		if #members > 0 then
			if custom.mode == "manual" or #members < 2 then
				add_group(custom.name, "select", members)
			else
				add_group(custom.name, "url-test", members, autotest_extra)
			end
		end
	end

	-- 3) 全局自动选择（全部节点）
	local all_nodes = {}
	for _, proxy in ipairs(doc.proxies or {}) do
		if type(proxy) == "table" then
			local name = tostring(proxy.name or "")
			if name ~= "" and proxy_names[name] then
				all_nodes[#all_nodes + 1] = name
			end
		end
	end
	if #all_nodes >= 2 then
		add_group(AUTO_SELECT_GROUP_NAME, "url-test", all_nodes, autotest_extra)
	end

	if #added_groups == 0 then
		return 0, 0
	end

	local added_names = {}
	for _, group in ipairs(added_groups) do
		added_names[#added_names + 1] = group.name
	end

	-- 4) 把新组名插进每个 select 分流组（放在第一个真实节点之前）
	for _, group in ipairs(groups) do
		if type(group) == "table"
			and tostring(group.type or ""):lower() == "select"
			and type(group.proxies) == "table"
			and not has_nonempty_sequence(group.use)
		then
			local at = nil
			local existing = {}
			for index, member in ipairs(group.proxies) do
				local name = tostring(member or "")
				existing[name] = true
				if not at and proxy_names[name] then
					at = index
				end
			end
			if at then
				local offset = 0
				for _, name in ipairs(added_names) do
					if name ~= group.name and not existing[name] then
						offset = offset + 1
						table.insert(group.proxies, at + offset - 1, name)
						existing[name] = true
					end
				end
			end
		end
	end

	-- 5) 新组统一插到 proxy-groups 最前面，方便在面板里一眼看到
	local result = {}
	for _, group in ipairs(added_groups) do
		result[#result + 1] = group
	end
	for _, group in ipairs(groups) do
		result[#result + 1] = group
	end
	doc["proxy-groups"] = result

	return #added_groups, region_count
end

local function strip_incompatible_script_rules(doc)
	local kept = {}
	local removed = 0
	local has_script_rule = false

	for _, rule in ipairs(doc.rules or {}) do
		local text = tostring(rule or "")
		if text:match("^SCRIPT,") then
			removed = removed + 1
		else
			kept[#kept + 1] = rule
			if text:match("^SCRIPT,") then
				has_script_rule = true
			end
		end
	end

	if removed > 0 then
		doc.rules = kept
	end

	if not has_script_rule then
		doc.script = nil
	end

	return removed
end

local function prepare(input_path, output_path)
	local doc, err = load_yaml(input_path)
	if not doc then
		io.stderr:write(err or "parse_failed", "\n")
		return false
	end
	if not has_proxy_sections(doc) then
		io.stderr:write("missing_proxy_sections\n")
		return false
	end

	strip_runtime_conflicts(doc)
	local filled_groups = fill_empty_proxy_groups(doc)
	local stripped_rules = strip_incompatible_script_rules(doc)
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end

	write_file(output_path, rendered)
	io.stdout:write(string.format("filled_groups=%d stripped_script_rules=%d\n", filled_groups, stripped_rules))
	return true
end

local function merge(raw_path, overlay_path, output_path)
	local raw_doc, raw_err = load_yaml(raw_path)
	if not raw_doc then
		io.stderr:write(raw_err or "parse_failed", "\n")
		return false
	end

	local overlay_doc, overlay_err = load_yaml(overlay_path)
	if not overlay_doc then
		io.stderr:write(overlay_err or "parse_failed", "\n")
		return false
	end

	strip_runtime_conflicts(raw_doc)
	local filled_groups = fill_empty_proxy_groups(raw_doc)
	local stripped_rules = strip_incompatible_script_rules(raw_doc)
	local injected_groups, injected_regions = inject_smart_groups(raw_doc)
	local merged = deep_merge(raw_doc, overlay_doc)
	local ok, rendered = pcall(lyaml.dump, { merged })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end

	write_file(output_path, rendered)
	io.stdout:write(string.format(
		"filled_groups=%d stripped_script_rules=%d injected_urltest=%d injected_regions=%d\n",
		filled_groups, stripped_rules, injected_groups, injected_regions))
	return true
end

local function append_client_policy_rules(runtime_path, sid)
	local doc, err = load_yaml(runtime_path)
	if not doc then
		io.stderr:write(err or "parse_failed", "\n")
		return false
	end

	local valid_policies = {}
	for _, proxy in ipairs(doc.proxies or {}) do
		if type(proxy) == "table" and proxy.name and proxy.name ~= "" then
			valid_policies[tostring(proxy.name)] = true
		end
	end
	for _, group in ipairs(doc["proxy-groups"] or {}) do
		if type(group) == "table" and group.name and group.name ~= "" then
			valid_policies[tostring(group.name)] = true
		end
	end

	local custom_rules = {}
	for _, section in ipairs(read_clash_client_rules_csv(sid)) do
		if tostring(section.enabled or "0") == "1" then
			local ip_addr = tostring(section.ip_addr or "")
			local policy_group = tostring(section.policy_group or "")
			if ip_addr ~= "" and policy_group ~= "" and valid_policies[policy_group] then
				if not ip_addr:find("/", 1, true) then
					ip_addr = ip_addr .. "/32"
				end
				custom_rules[#custom_rules + 1] = string.format("SRC-IP-CIDR,%s,%s", ip_addr, policy_group)
			end
		end
	end

	if #custom_rules == 0 then
		io.stdout:write("client_rules=0\n")
		return true
	end

	local existing_rules = {}
	for _, rule in ipairs(doc.rules or {}) do
		local text = tostring(rule or "")
		if not text:match("^SRC%-IP%-CIDR,") then
			existing_rules[#existing_rules + 1] = rule
		end
	end

	doc.rules = {}
	for _, rule in ipairs(custom_rules) do
		doc.rules[#doc.rules + 1] = rule
	end
	for _, rule in ipairs(existing_rules) do
		doc.rules[#doc.rules + 1] = rule
	end

	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end

	write_file(runtime_path, rendered)
	io.stdout:write(string.format("client_rules=%d\n", #custom_rules))
	return true
end

local function bool_enabled(value)
	return value == "1" or value == 1 or value == true or value == "true"
end

local function split_csv(value)
	local items = {}
	for part in tostring(value or ""):gmatch("[^,%s]+") do
		items[#items + 1] = part
	end
	return items
end

local function get_server_field(sid, option, default)
	local value = uci:get("shadowsocksr", sid, option)
	if value == nil or value == "" then
		return default
	end
	return value
end

local function get_filter_aaaa()
	local value = uci:get_first("shadowsocksr", "global", "filter_aaaa", "1")
	if value == nil or value == "" then
		value = uci:get_first("shadowsocksr", "global", "mosdns_ipv6", "1")
	end
	return value
end

local function build_tuic_runtime_doc(sid, local_port, socks_port, mode)
	local server = get_server_field(sid, "server", "")
	local server_port = tonumber(get_server_field(sid, "server_port", "0")) or 0
	local tuic_ip = get_server_field(sid, "tuic_ip", "")
	local tls_host = get_server_field(sid, "tls_host", "")
	local ipstack_prefer = get_server_field(sid, "ipstack_prefer", "")
	local dns_mode = uci:get_first("shadowsocksr", "global", "pdnsd_enable", "0")

	local proxy = {
		name = sid,
		type = "tuic",
		server = server,
		port = server_port,
		uuid = get_server_field(sid, "tuic_uuid", ""),
		password = get_server_field(sid, "tuic_passwd", ""),
		["udp-relay-mode"] = get_server_field(sid, "udp_relay_mode", "native"),
		["congestion-controller"] = get_server_field(sid, "congestion_control", "cubic"),
		["skip-cert-verify"] = bool_enabled(get_server_field(sid, "insecure", "0")),
		["disable-sni"] = bool_enabled(get_server_field(sid, "disable_sni", "0")),
		["reduce-rtt"] = bool_enabled(get_server_field(sid, "zero_rtt_handshake", "0"))
	}

	if tuic_ip ~= "" then
		proxy.ip = tuic_ip
	end
	if tls_host ~= "" then
		proxy.sni = tls_host
	end

	local alpn = split_csv(get_server_field(sid, "tuic_alpn", ""))
	if #alpn > 0 then
		proxy.alpn = alpn
	end

	local heartbeat = tonumber(get_server_field(sid, "heartbeat", "0"))
	if heartbeat and heartbeat > 0 then
		proxy["heartbeat-interval"] = heartbeat * 1000
	end

	local timeout = tonumber(get_server_field(sid, "timeout", "0"))
	if timeout and timeout > 0 then
		proxy["request-timeout"] = timeout * 1000
	end

	local max_udp_packet_size = tonumber(get_server_field(sid, "tuic_max_package_size", "0"))
	if max_udp_packet_size and max_udp_packet_size > 0 then
		proxy["max-udp-relay-packet-size"] = max_udp_packet_size
	end

	if ipstack_prefer ~= "" then
		proxy["ip-version"] = ipstack_prefer == "v6first" and "ipv6-prefer" or "ipv4-prefer"
	end

	local listen_port = tonumber(local_port)
	local socks_listen = tonumber(socks_port)

	local doc = {
		["allow-lan"] = true,
		["bind-address"] = "0.0.0.0",
		mode = "rule",
		["log-level"] = "silent",
		["find-process-mode"] = "off",
		["unified-delay"] = true,
		["tcp-concurrent"] = true,
		["routing-mark"] = 255,
		proxies = { proxy },
		["proxy-groups"] = {
			{
				name = "PROXY",
				type = "select",
				proxies = { sid }
			}
		},
		rules = { "MATCH,PROXY" },
		tun = { enable = false },
		profile = { ["store-selected"] = true },
		dns = {
			enable = dns_mode == "7",
			["enhanced-mode"] = "redir-host",
			listen = "127.0.0.1:5335",
			ipv6 = get_filter_aaaa() ~= "1"
		}
	}

	if mode == "socks" then
		doc["socks-port"] = listen_port
	else
		doc["redir-port"] = listen_port
		doc["tproxy-port"] = listen_port
		if socks_listen and socks_listen > 0 then
			doc["socks-port"] = socks_listen
		end
	end

	if doc["socks-port"] and doc["socks-port"] > 0 then
		local socks5_auth = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_auth", "noauth")
		if socks5_auth == "password" then
			local socks5_user = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_user", "")
			local socks5_pass = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_pass", "")

			if socks5_user == "" or socks5_pass == "" then
				io.stderr:write("警告：SOCKS5 代理未完整配置用户名或密码，已自动降级为无认证模式 (noauth)！\n")
			else
				doc["authentication"] = {
					string.format("%s:%s", socks5_user, socks5_pass)
				}
			end
		end
	end


	return doc
end

local function generate_tuic_runtime(sid, output_path, local_port, socks_port, mode)
	local doc = build_tuic_runtime_doc(sid, local_port, socks_port, mode)
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end
	write_file(output_path, rendered)
	return true
end

local function parse_plugin_opts(value)
	local result = {}
	for part in tostring(value or ""):gmatch("[^;]+") do
		local key, val = part:match("^%s*([^=]+)=?(.*)%s*$")
		if key and key ~= "" then
			result[key] = val or ""
		end
	end
	return result
end

local function split_host_port(value)
	local text = tostring(value or "")
	if text == "" then
		return "", ""
	end
	local host, port = text:match("^%[(.-)%]:(%d+)$")
	if host and port then
		return host, port
	end
	host, port = text:match("^(.-):(%d+)$")
	if host and port then
		return host, port
	end
	return text, ""
end

local function bool_default(value, default)
	if value == nil or value == "" then
		return default
	end
	return bool_enabled(value)
end

local function number_or_nil(value)
	if value == nil or value == "" then
		return nil
	end
	return tonumber(value)
end

local function string_or_nil(value)
	if value == nil or value == "" then
		return nil
	end
	return tostring(value)
end

local build_shadowsocks_plugin

local function first_nonempty(...)
	for i = 1, select("#", ...) do
		local value = select(i, ...)
		if value ~= nil and value ~= "" then
			return value
		end
	end
	return nil
end

local function split_alpn(value)
	local items = {}
	for part in tostring(value or ""):gmatch("[^,;|%s]+") do
		items[#items + 1] = part
	end
	return items
end

local function parse_wireguard_reserved(sid)
	local raw = get_server_field(sid, "reserved", nil)
	local values = {}
	local bytes = {}

	if raw == nil or raw == "" then
		return nil
	end
	if type(raw) == "table" then
		values = raw
	else
		values = { raw }
	end
	for _, item in ipairs(values) do
		local text = tostring(item or "")
		if text ~= "" then
			if not text:match("[^%d,]+") then
				for byte in text:gmatch("%d+") do
					bytes[#bytes + 1] = tonumber(byte)
				end
			else
				local decoded = nixio.bin.b64decode(text)
				if decoded then
					for i = 1, #decoded do
						bytes[#bytes + 1] = decoded:byte(i)
					end
				end
			end
		end
	end
	return #bytes > 0 and bytes or nil
end

local function split_local_addresses(value)
	local items = {}
	if type(value) == "table" then
		for _, item in ipairs(value) do
			if item and item ~= "" then
				items[#items + 1] = tostring(item)
			end
		end
	elseif value and value ~= "" then
		for item in tostring(value):gmatch("[^,%s]+") do
			items[#items + 1] = item
		end
	end
	return items
end

local function split_wireguard_addresses(value)
	local ip, ipv6
	for _, item in ipairs(split_local_addresses(value)) do
		if item:find(":", 1, true) then
			ipv6 = ipv6 or item
		else
			ip = ip or item
		end
	end
	return ip, ipv6
end

local function apply_v2ray_tls_options(proxy, sid)
	local tls = get_server_field(sid, "tls", "0")
	local reality = get_server_field(sid, "reality", "0")
	if tls ~= "1" and reality ~= "1" then
		return
	end

	proxy.tls = true
	proxy.servername = string_or_nil(get_server_field(sid, "tls_host", ""))
	proxy["client-fingerprint"] = string_or_nil(get_server_field(sid, "fingerprint", ""))
	proxy.fingerprint = string_or_nil(get_server_field(sid, "tls_CertSha", ""))
	proxy["skip-cert-verify"] = bool_enabled(get_server_field(sid, "insecure", "0"))

	local alpn = split_alpn(get_server_field(sid, "tls_alpn", ""))
	if #alpn > 0 then
		proxy.alpn = alpn
	end

	if reality == "1" then
		proxy["reality-opts"] = {
			["public-key"] = string_or_nil(get_server_field(sid, "reality_publickey", "")),
			["short-id"] = string_or_nil(get_server_field(sid, "reality_shortid", "")),
			["support-x25519mlkem768"] = bool_enabled(get_server_field(sid, "enable_mldsa65verify", "0"))
		}
	end

	if get_server_field(sid, "enable_ech", "0") == "1" then
		proxy["ech-opts"] = {
			enable = true,
			config = string_or_nil(get_server_field(sid, "ech_config", ""))
		}
	end
end

local function apply_trojan_tls_options(proxy, sid)
	proxy.sni = string_or_nil(get_server_field(sid, "tls_host", ""))
	proxy["client-fingerprint"] = string_or_nil(get_server_field(sid, "fingerprint", ""))
	proxy.fingerprint = string_or_nil(get_server_field(sid, "tls_CertSha", ""))
	proxy["skip-cert-verify"] = bool_enabled(get_server_field(sid, "insecure", "0"))

	local alpn = split_alpn(get_server_field(sid, "tls_alpn", ""))
	if #alpn > 0 then
		proxy.alpn = alpn
	end

	if get_server_field(sid, "reality", "0") == "1" then
		proxy["reality-opts"] = {
			["public-key"] = string_or_nil(get_server_field(sid, "reality_publickey", "")),
			["short-id"] = string_or_nil(get_server_field(sid, "reality_shortid", ""))
		}
	end
end

local function apply_v2ray_transport_options(proxy, sid)
	local transport = get_server_field(sid, "transport", "raw")
	if transport == "raw" or transport == "tcp" or transport == "" then
		return
	end

	if transport == "ws" then
		proxy.network = "ws"
		proxy["ws-opts"] = {
			path = string_or_nil(get_server_field(sid, "ws_path", "")),
			headers = first_nonempty(get_server_field(sid, "ws_host", ""), get_server_field(sid, "tls_host", "")) and {
				Host = first_nonempty(get_server_field(sid, "ws_host", ""), get_server_field(sid, "tls_host", ""))
			} or nil
		}
	elseif transport == "httpupgrade" then
		proxy.network = "ws"
		proxy["ws-opts"] = {
			path = string_or_nil(get_server_field(sid, "httpupgrade_path", "")),
			headers = first_nonempty(get_server_field(sid, "httpupgrade_host", ""), get_server_field(sid, "tls_host", "")) and {
				Host = first_nonempty(get_server_field(sid, "httpupgrade_host", ""), get_server_field(sid, "tls_host", ""))
			} or nil,
			["v2ray-http-upgrade"] = true
		}
	elseif transport == "h2" then
		local host = first_nonempty(get_server_field(sid, "h2_host", ""), get_server_field(sid, "tls_host", ""))
		proxy.network = "h2"
		proxy["h2-opts"] = {
			host = host and split_alpn(host) or nil,
			path = string_or_nil(get_server_field(sid, "h2_path", ""))
		}
	elseif transport == "grpc" then
		proxy.network = "grpc"
		proxy["grpc-opts"] = {
			["grpc-service-name"] = string_or_nil(get_server_field(sid, "serviceName", ""))
		}
	elseif transport == "xhttp" and proxy.type == "vless" then
		proxy.network = "xhttp"
		proxy["xhttp-opts"] = {
			path = string_or_nil(get_server_field(sid, "xhttp_path", "")),
			host = string_or_nil(get_server_field(sid, "xhttp_host", "")),
			mode = string_or_nil(get_server_field(sid, "xhttp_mode", ""))
		}
	end
end

local function can_mihomo_handle_v2ray_transport(protocol, sid)
	local transport = get_server_field(sid, "transport", "raw")
	if transport == "" or transport == "raw" or transport == "tcp" then
		if get_server_field(sid, "tcp_guise", "none") == "http" then
			return false
		end
		return true
	end
	if protocol == "socks" or protocol == "http" then
		return false
	end
	if transport == "ws" or transport == "httpupgrade" or transport == "h2" or transport == "grpc" then
		return true
	end
	return protocol == "vless" and transport == "xhttp"
end

local function build_v2ray_mihomo_proxy(sid)
	local node_type = get_server_field(sid, "type", "")
	local protocol = get_server_field(sid, "v2ray_protocol", "vmess")
	local proxy = {
		name = sid,
		server = get_server_field(sid, "server", ""),
		port = tonumber(get_server_field(sid, "server_port", "0")) or 0,
		udp = true,
		tfo = bool_enabled(get_server_field(sid, "fast_open", "0"))
	}

	if node_type == "socks5" then
		proxy.type = "socks5"
		if get_server_field(sid, "auth_enable", "0") == "1" then
			proxy.username = string_or_nil(get_server_field(sid, "username", ""))
			proxy.password = string_or_nil(get_server_field(sid, "password", ""))
		end
	elseif protocol == "vmess" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "vmess"
		proxy.uuid = first_nonempty(get_server_field(sid, "vmess_id", ""), get_server_field(sid, "vmess_uuid", "")) or ""
		proxy.alterId = tonumber(get_server_field(sid, "alter_id", "0")) or 0
		proxy.cipher = first_nonempty(get_server_field(sid, "security", ""), get_server_field(sid, "vmess_method", ""), "auto")
		apply_v2ray_tls_options(proxy, sid)
		apply_v2ray_transport_options(proxy, sid)
	elseif protocol == "vless" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "vless"
		proxy.uuid = get_server_field(sid, "vmess_id", "")
		proxy.flow = string_or_nil(get_server_field(sid, "tls_flow", ""))
		proxy.encryption = get_server_field(sid, "vless_encryption", "")
		apply_v2ray_tls_options(proxy, sid)
		apply_v2ray_transport_options(proxy, sid)
	elseif protocol == "trojan" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "trojan"
		proxy.password = get_server_field(sid, "password", "")
		apply_trojan_tls_options(proxy, sid)
		apply_v2ray_transport_options(proxy, sid)
	elseif protocol == "shadowsocks" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "ss"
		proxy.cipher = get_server_field(sid, "encrypt_method_ss", "none")
		proxy.password = get_server_field(sid, "password", "")
		build_shadowsocks_plugin(proxy, sid)
	elseif protocol == "hysteria2" then
		proxy.type = "hysteria2"
		proxy.password = get_server_field(sid, "hy2_auth", "")
		proxy.ports = string_or_nil(get_server_field(sid, "port_range", ""))
		proxy.up = string_or_nil(get_server_field(sid, "uplink_capacity", "")) and (get_server_field(sid, "uplink_capacity", "") .. " Mbps") or nil
		proxy.down = string_or_nil(get_server_field(sid, "downlink_capacity", "")) and (get_server_field(sid, "downlink_capacity", "") .. " Mbps") or nil
		proxy.sni = string_or_nil(get_server_field(sid, "tls_host", ""))
		proxy.fingerprint = string_or_nil(get_server_field(sid, "tls_CertSha", ""))
		proxy["skip-cert-verify"] = bool_enabled(get_server_field(sid, "insecure", "0"))
		local alpn = split_alpn(get_server_field(sid, "tls_alpn", ""))
		if #alpn > 0 then
			proxy.alpn = alpn
		end
		if get_server_field(sid, "flag_obfs", "0") == "1" then
			local obfs_type = get_server_field(sid, "obfs_type", "")
			proxy.obfs = string_or_nil(obfs_type)
			proxy["obfs-password"] = string_or_nil(get_server_field(sid, "salamander", ""))
			if obfs_type == "gecko" then
				local min = tonumber(get_server_field(sid, "obfs_MinPacketSize", "")) or 512
				local max = tonumber(get_server_field(sid, "obfs_MaxPacketSize", "")) or 1200
				if min <= 0 or min > max or max > 2048 then
					min = 512
					max = 1200
				end
				proxy["obfs-min-packet-size"] = min
				proxy["obfs-max-packet-size"] = max
			end
		end
	elseif protocol == "socks" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "socks5"
		if get_server_field(sid, "socks_ver", "5") ~= "5" then
			return nil
		end
		if get_server_field(sid, "auth_enable", "0") == "1" then
			proxy.username = string_or_nil(get_server_field(sid, "username", ""))
			proxy.password = string_or_nil(get_server_field(sid, "password", ""))
		end
		apply_v2ray_tls_options(proxy, sid)
	elseif protocol == "http" then
		if not can_mihomo_handle_v2ray_transport(protocol, sid) then
			return nil
		end
		proxy.type = "http"
		if get_server_field(sid, "auth_enable", "0") == "1" then
			proxy.username = string_or_nil(get_server_field(sid, "username", ""))
			proxy.password = string_or_nil(get_server_field(sid, "password", ""))
		end
		apply_v2ray_tls_options(proxy, sid)
	elseif protocol == "wireguard" then
		local ip, ipv6 = split_wireguard_addresses(get_server_field(sid, "local_addresses", ""))
		proxy.type = "wireguard"
		proxy["private-key"] = get_server_field(sid, "private_key", "")
		proxy["public-key"] = get_server_field(sid, "peer_pubkey", "")
		proxy["pre-shared-key"] = string_or_nil(get_server_field(sid, "preshared_key", ""))
		proxy.ip = ip
		proxy.ipv6 = ipv6
		proxy["allowed-ips"] = split_local_addresses(get_server_field(sid, "allowedips", "0.0.0.0/0"))
		proxy.reserved = parse_wireguard_reserved(sid)
		proxy["persistent-keepalive"] = number_or_nil(get_server_field(sid, "keepaliveperiod", ""))
		proxy.mtu = number_or_nil(get_server_field(sid, "mtu", ""))
	elseif protocol == "snell" then
		proxy.type = "snell"
		proxy.psk = get_server_field(sid, "snell_psk", "")
		proxy.version = number_or_nil(get_server_field(sid, "snell_version", ""))
		local obfs_mode = string_or_nil(get_server_field(sid, "snell_obfs", ""))
		local obfs_host = string_or_nil(get_server_field(sid, "snell_obfs_host", ""))
		if obfs_mode or obfs_host then
			proxy["obfs-opts"] = {
				mode = obfs_mode,
				host = obfs_host
			}
		end
	else
		return nil
	end

	if not proxy.server or proxy.server == "" or not proxy.port or proxy.port == 0 then
		return nil
	end
	if proxy.type == "snell" and (not proxy.psk or proxy.psk == "") then
		return nil
	end
	return proxy
end

local function build_single_proxy_runtime_doc(proxy, local_port, socks_port, mode)
	local dns_mode = uci:get_first("shadowsocksr", "global", "pdnsd_enable", "0")
	local listen_port = tonumber(local_port)
	local socks_listen = tonumber(socks_port)
	local doc = {
		["allow-lan"] = true,
		["bind-address"] = "0.0.0.0",
		mode = "rule",
		["log-level"] = "silent",
		["find-process-mode"] = "off",
		["unified-delay"] = true,
		["tcp-concurrent"] = true,
		["routing-mark"] = 255,
		proxies = { proxy },
		["proxy-groups"] = {
			{
				name = "PROXY",
				type = "select",
				proxies = { proxy.name }
			}
		},
		rules = { "MATCH,PROXY" },
		tun = { enable = false },
		profile = { ["store-selected"] = true },
		dns = {
			enable = dns_mode == "7",
			["enhanced-mode"] = "redir-host",
			listen = "127.0.0.1:5335",
			ipv6 = get_filter_aaaa() ~= "1"
		}
	}

	if mode == "socks" then
		doc["socks-port"] = listen_port
	else
		doc["redir-port"] = listen_port
		doc["tproxy-port"] = listen_port
		if socks_listen and socks_listen > 0 then
			doc["socks-port"] = socks_listen
		end
	end
	if doc["socks-port"] and doc["socks-port"] > 0 then
		local socks5_auth = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_auth", "noauth")
		if socks5_auth == "password" then
			local socks5_user = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_user", "")
			local socks5_pass = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_pass", "")

			if socks5_user == "" or socks5_pass == "" then
				io.stderr:write("警告：SOCKS5 代理未完整配置用户名或密码，已自动降级为无认证模式 (noauth)！\n")
			else
				doc["authentication"] = {
					string.format("%s:%s", socks5_user, socks5_pass)
				}
			end
		end
	end


	return doc
end

local function pick_plugin_opt(plugin_opts, ...)
	for i = 1, select("#", ...) do
		local key = select(i, ...)
		local value = plugin_opts[key]
		if value ~= nil and value ~= "" then
			return value
		end
	end
	return nil
end

local function parse_plugin_headers(plugin_opts)
	local headers = {}
	local raw_headers = pick_plugin_opt(plugin_opts, "headers", "header")

	if raw_headers and ok_jsonc and jsonc then
		local decoded = jsonc.parse(raw_headers)
		if type(decoded) == "table" then
			for key, value in pairs(decoded) do
				headers[tostring(key)] = tostring(value)
			end
		end
	end

	if raw_headers and next(headers) == nil then
		for part in tostring(raw_headers):gmatch("[^|,]+") do
			local key, value = part:match("^%s*([^=:]+)%s*[:=]%s*(.-)%s*$")
			if key and key ~= "" and value and value ~= "" then
				headers[key] = value
			end
		end
	end

	for key, value in pairs(plugin_opts) do
		local header_name = key:match("^headers[%.:](.+)$")
			or key:match("^header[%.:](.+)$")
			or key:match("^header_(.+)$")
		if header_name and header_name ~= "" and value ~= "" then
			headers[header_name] = value
		end
	end

	return next(headers) and headers or nil
end

local function get_plugin_client_fingerprint(sid, plugin_opts)
	return string_or_nil(
		pick_plugin_opt(
			plugin_opts,
			"client-fingerprint",
			"client_fingerprint",
			"fingerprint"
		) or get_server_field(sid, "fingerprint", "")
	)
end

local function normalize_plugin_name(plugin)
	local value = tostring(plugin or ""):lower()
	if value == "" or value == "none" then
		return ""
	end
	if value == "simple-obfs" then
		return "obfs-local"
	end
	if value == "obfs" then
		return "obfs-local"
	end
	if value == "shadowtls" then
		return "shadow-tls"
	end
	if value == "gost" then
		return "gost-plugin"
	end
	if value == "kcp-tun" then
		return "kcptun"
	end
	return value
end

function build_shadowsocks_plugin(proxy, sid)
	local plugin = normalize_plugin_name(get_server_field(sid, "plugin", ""))
	local plugin_opts = parse_plugin_opts(get_server_field(sid, "plugin_opts", ""))

	if plugin == "" then
		return
	end

	if plugin == "obfs-local" then
		proxy.plugin = "obfs"
		proxy["plugin-opts"] = {
			mode = pick_plugin_opt(plugin_opts, "obfs", "mode") or "http",
			host = string_or_nil(pick_plugin_opt(plugin_opts, "obfs-host", "obfs_host", "host"))
		}
		return
	end

	if plugin == "v2ray-plugin" or plugin == "xray-plugin" then
		proxy.plugin = "v2ray-plugin"
		proxy["plugin-opts"] = {
			mode = pick_plugin_opt(plugin_opts, "mode") or "websocket",
			tls = bool_default(pick_plugin_opt(plugin_opts, "tls"), false),
			fingerprint = string_or_nil(pick_plugin_opt(plugin_opts, "fingerprint")),
			["skip-cert-verify"] = bool_default(pick_plugin_opt(plugin_opts, "skip-cert-verify", "skip_cert_verify", "insecure"), false),
			host = string_or_nil(pick_plugin_opt(plugin_opts, "host")),
			path = string_or_nil(pick_plugin_opt(plugin_opts, "path")),
			mux = bool_default(pick_plugin_opt(plugin_opts, "mux"), false),
			headers = parse_plugin_headers(plugin_opts),
			["v2ray-http-upgrade"] = bool_default(pick_plugin_opt(plugin_opts, "v2ray-http-upgrade", "v2ray_http_upgrade"), false)
		}
		return
	end

	if plugin == "gost-plugin" then
		proxy.plugin = "gost-plugin"
		proxy["plugin-opts"] = {
			mode = pick_plugin_opt(plugin_opts, "mode") or "websocket",
			tls = bool_default(pick_plugin_opt(plugin_opts, "tls"), false),
			fingerprint = string_or_nil(pick_plugin_opt(plugin_opts, "fingerprint")),
			["skip-cert-verify"] = bool_default(pick_plugin_opt(plugin_opts, "skip-cert-verify", "skip_cert_verify", "insecure"), false),
			host = string_or_nil(pick_plugin_opt(plugin_opts, "host")),
			path = string_or_nil(pick_plugin_opt(plugin_opts, "path")),
			mux = bool_default(pick_plugin_opt(plugin_opts, "mux"), false),
			headers = parse_plugin_headers(plugin_opts)
		}
		return
	end

	if plugin == "shadow-tls" then
		local host, port = split_host_port(pick_plugin_opt(plugin_opts, "host") or "")
		local version
		if plugin_opts.v3 == "1" or plugin_opts.version == "3" then
			version = 3
		elseif plugin_opts.v2 == "1" or plugin_opts.version == "2" then
			version = 2
		elseif plugin_opts.v1 == "1" or plugin_opts.version == "1" then
			version = 1
		end
		proxy.plugin = "shadow-tls"
		proxy["client-fingerprint"] = get_plugin_client_fingerprint(sid, plugin_opts)
		proxy["plugin-opts"] = {
			host = host ~= "" and host or nil,
			port = number_or_nil(port),
			password = string_or_nil(pick_plugin_opt(plugin_opts, "passwd", "password")),
			version = version
		}
		return
	end

	if plugin == "restls" then
		proxy.plugin = "restls"
		proxy["client-fingerprint"] = get_plugin_client_fingerprint(sid, plugin_opts)
		proxy["plugin-opts"] = {
			host = string_or_nil(pick_plugin_opt(plugin_opts, "host")),
			password = string_or_nil(pick_plugin_opt(plugin_opts, "passwd", "password")),
			["version-hint"] = string_or_nil(pick_plugin_opt(plugin_opts, "version-hint", "version_hint")),
			["restls-script"] = string_or_nil(pick_plugin_opt(plugin_opts, "restls-script", "restls_script"))
		}
		return
	end

	if plugin == "kcptun" then
		proxy.plugin = "kcptun"
		proxy["plugin-opts"] = {
			key = string_or_nil(pick_plugin_opt(plugin_opts, "key", "passwd", "password")),
			crypt = string_or_nil(pick_plugin_opt(plugin_opts, "crypt")),
			mode = string_or_nil(pick_plugin_opt(plugin_opts, "mode")),
			conn = number_or_nil(pick_plugin_opt(plugin_opts, "conn")),
			autoexpire = number_or_nil(pick_plugin_opt(plugin_opts, "autoexpire")),
			scavengettl = number_or_nil(pick_plugin_opt(plugin_opts, "scavengettl")),
			mtu = number_or_nil(pick_plugin_opt(plugin_opts, "mtu")),
			ratelimit = number_or_nil(pick_plugin_opt(plugin_opts, "ratelimit")),
			sndwnd = number_or_nil(pick_plugin_opt(plugin_opts, "sndwnd")),
			rcvwnd = number_or_nil(pick_plugin_opt(plugin_opts, "rcvwnd")),
			datashard = number_or_nil(pick_plugin_opt(plugin_opts, "datashard")),
			parityshard = number_or_nil(pick_plugin_opt(plugin_opts, "parityshard")),
			dscp = number_or_nil(pick_plugin_opt(plugin_opts, "dscp")),
			nocomp = bool_default(pick_plugin_opt(plugin_opts, "nocomp"), false),
			acknodelay = bool_default(pick_plugin_opt(plugin_opts, "acknodelay"), false),
			nodelay = number_or_nil(pick_plugin_opt(plugin_opts, "nodelay")),
			interval = number_or_nil(pick_plugin_opt(plugin_opts, "interval")),
			resend = number_or_nil(pick_plugin_opt(plugin_opts, "resend")),
			sockbuf = number_or_nil(pick_plugin_opt(plugin_opts, "sockbuf")),
			smuxver = number_or_nil(pick_plugin_opt(plugin_opts, "smuxver")),
			smuxbuf = number_or_nil(pick_plugin_opt(plugin_opts, "smuxbuf")),
			framesize = number_or_nil(pick_plugin_opt(plugin_opts, "framesize")),
			streambuf = number_or_nil(pick_plugin_opt(plugin_opts, "streambuf")),
			keepalive = number_or_nil(pick_plugin_opt(plugin_opts, "keepalive"))
		}
		return
	end

	proxy.plugin = plugin
	if next(plugin_opts) then
		proxy["plugin-opts"] = plugin_opts
	end
end

local function build_kcptun_plugin(proxy, sid)
	if not bool_enabled(get_server_field(sid, "kcp_enable", "0")) then
		return
	end

	proxy.plugin = "kcptun"
	proxy.port = tonumber(get_server_field(sid, "kcp_port", "0")) or proxy.port
	proxy["plugin-opts"] = {
		key = get_server_field(sid, "kcp_password", ""),
		mode = "fast",
		mtu = 1350
	}
end

local function build_shadowsocks_runtime_doc(sid, local_port, socks_port, mode)
	local dns_mode = uci:get_first("shadowsocksr", "global", "pdnsd_enable", "0")
	local server = get_server_field(sid, "server", "")
	local server_port = tonumber(get_server_field(sid, "server_port", "0")) or 0
	local method = get_server_field(sid, "encrypt_method_ss", "none")
	local password = get_server_field(sid, "password", "")
	local proxy = {
		name = sid,
		type = "ss",
		server = server,
		port = server_port,
		cipher = method,
		password = password,
		udp = true,
		tfo = bool_enabled(get_server_field(sid, "fast_open", "0"))
	}

	if get_server_field(sid, "type", "") == "ss" then
		build_kcptun_plugin(proxy, sid)
	end
	if proxy.plugin == nil then
		build_shadowsocks_plugin(proxy, sid)
	end

	local doc = {
		["allow-lan"] = true,
		["bind-address"] = "0.0.0.0",
		mode = "rule",
		["log-level"] = "silent",
		["find-process-mode"] = "off",
		["unified-delay"] = true,
		["tcp-concurrent"] = true,
		["routing-mark"] = 255,
		proxies = { proxy },
		["proxy-groups"] = {
			{
				name = "PROXY",
				type = "select",
				proxies = { sid }
			}
		},
		rules = { "MATCH,PROXY" },
		tun = { enable = false },
		profile = { ["store-selected"] = true },
		dns = {
			enable = dns_mode == "7",
			["enhanced-mode"] = "redir-host",
			listen = "127.0.0.1:5335",
			ipv6 = get_filter_aaaa() ~= "1"
		}
	}

	local listen_port = tonumber(local_port)
	local socks_listen = tonumber(socks_port)
	if mode == "socks" then
		doc["socks-port"] = listen_port
	else
		doc["redir-port"] = listen_port
		doc["tproxy-port"] = listen_port
		if socks_listen and socks_listen > 0 then
			doc["socks-port"] = socks_listen
		end
	end

	if doc["socks-port"] and doc["socks-port"] > 0 then
		local socks5_auth = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_auth", "noauth")
		if socks5_auth == "password" then
			local socks5_user = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_user", "")
			local socks5_pass = uci:get_first("shadowsocksr", "socks5_proxy", "socks5_pass", "")

			if socks5_user == "" or socks5_pass == "" then
				io.stderr:write("警告：SOCKS5 代理未完整配置用户名或密码，已自动降级为无认证模式 (noauth)！\n")
			else
				doc["authentication"] = {
					string.format("%s:%s", socks5_user, socks5_pass)
				}
			end
		end
	end


	return doc
end

local function generate_shadowsocks_runtime(sid, output_path, local_port, socks_port, mode)
	local doc = build_shadowsocks_runtime_doc(sid, local_port, socks_port, mode)
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end
	write_file(output_path, rendered)
	return true
end

local function generate_v2ray_runtime(sid, output_path, local_port, socks_port, mode)
	local proxy = build_v2ray_mihomo_proxy(sid)
	if not proxy then
		io.stderr:write("unsupported_or_invalid_v2ray_node\n")
		return false
	end
	local doc = build_single_proxy_runtime_doc(proxy, local_port, socks_port, mode)
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end
	write_file(output_path, rendered)
	return true
end

local function build_shadowsocks_server_doc(sid)
	local server_port = tonumber(get_server_field(sid, "server_port", "0")) or 0
	local method = get_server_field(sid, "encrypt_method_ss", "aes-128-gcm")
	local password = get_server_field(sid, "password", "")
	local listener = {
		name = sid,
		type = "shadowsocks",
		listen = "::",
		port = server_port,
		cipher = method,
		password = password,
		udp = true,
		tfo = bool_enabled(get_server_field(sid, "fast_open", "0"))
	}

	local plugin = normalize_plugin_name(get_server_field(sid, "plugin", ""))
	if plugin == "obfs-local" then
		local plugin_opts = parse_plugin_opts(get_server_field(sid, "plugin_opts", ""))
		listener.obfs = plugin_opts.obfs or plugin_opts.mode or "http"
		listener.obfs_opts = {
			mode = plugin_opts.obfs or plugin_opts.mode or "http",
			host = plugin_opts["obfs-host"] or plugin_opts.obfs_host or plugin_opts.host or nil
		}
	end

	return {
		["allow-lan"] = true,
		["bind-address"] = "*",
		["log-level"] = "silent",
		["find-process-mode"] = "off",
		listeners = { listener }
	}
end

local function generate_shadowsocks_server(sid, output_path)
	local doc = build_shadowsocks_server_doc(sid)
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end
	write_file(output_path, rendered)
	return true
end

local function build_mihomo_listener_doc(sid)
	local ltype = get_server_field(sid, "type", "")
	local server_port = tonumber(get_server_field(sid, "server_port", "0")) or 0
	local listener = {
		name = sid,
		type = ltype,
		listen = "::",
		port = server_port
	}

	if ltype == "vmess" then
		listener.users = {
			{
				username = "1",
				uuid = get_server_field(sid, "uuid", ""),
				alterId = tonumber(get_server_field(sid, "alter_id", "0")) or 0
			}
		}
	elseif ltype == "vless" then
		listener.users = {
			{
				username = "1",
				uuid = get_server_field(sid, "uuid", ""),
				flow = string_or_nil(get_server_field(sid, "flow", ""))
			}
		}
	elseif ltype == "trojan" then
		listener.users = {
			{
				username = "1",
				password = get_server_field(sid, "trojan_password", "")
			}
		}
	elseif ltype == "shadowsocks" then
		listener.type = "shadowsocks"
		listener.cipher = get_server_field(sid, "security", "chacha20-ietf-poly1305")
		listener.password = get_server_field(sid, "ss_password", "")
		listener.udp = true
	else
		return nil
	end

	local network = get_server_field(sid, "network", "tcp")
	if network == "ws" then
		listener["ws-path"] = string_or_nil(get_server_field(sid, "ws_path", "/"))
	elseif network == "grpc" then
		listener["grpc-service-name"] = string_or_nil(get_server_field(sid, "grpc_service", ""))
	end

	local cert = get_server_field(sid, "certpath", "")
	local key = get_server_field(sid, "keypath", "")
	if cert ~= "" and key ~= "" then
		listener.certificate = cert
		listener["private-key"] = key
	end

	return {
		["allow-lan"] = true,
		["bind-address"] = "*",
		["log-level"] = "silent",
		["find-process-mode"] = "off",
		listeners = { listener }
	}
end

local function generate_mihomo_listener(sid, output_path)
	local doc = build_mihomo_listener_doc(sid)
	if not doc then
		io.stderr:write("unsupported_listener\n")
		return false
	end
	local ok, rendered = pcall(lyaml.dump, { doc })
	if not ok or not rendered then
		io.stderr:write("dump_failed\n")
		return false
	end
	write_file(output_path, rendered)
	return true
end

local action = arg[1]
if action == "validate" then
	os.exit(validate(arg[2]) and 0 or 1)
elseif action == "filter" then
	os.exit(filter(arg[2], arg[3]) and 0 or 1)
elseif action == "prepare" then
	os.exit(prepare(arg[2], arg[3]) and 0 or 1)
elseif action == "merge" then
	os.exit(merge(arg[2], arg[3], arg[4]) and 0 or 1)
elseif action == "append_client_policy_rules" then
	os.exit(append_client_policy_rules(arg[2], arg[3]) and 0 or 1)
elseif action == "tuic" then
	os.exit(generate_tuic_runtime(arg[2], arg[3], arg[4], arg[5], arg[6]) and 0 or 1)
elseif action == "ss" then
	os.exit(generate_shadowsocks_runtime(arg[2], arg[3], arg[4], arg[5], arg[6]) and 0 or 1)
elseif action == "v2ray" then
	os.exit(generate_v2ray_runtime(arg[2], arg[3], arg[4], arg[5], arg[6]) and 0 or 1)
elseif action == "ss_server" then
	os.exit(generate_shadowsocks_server(arg[2], arg[3]) and 0 or 1)
elseif action == "v2ray_server" then
	os.exit(generate_mihomo_listener(arg[2], arg[3]) and 0 or 1)
else
	io.stderr:write("usage: clash_yaml.lua validate <yaml> | filter <yaml> <words> | prepare <input> <output> | merge <raw> <overlay> <output> | append_client_policy_rules <runtime_yaml> <sid> | tuic <sid> <output> <local_port> [socks_port] [mode] | ss <sid> <output> <local_port> [socks_port] [mode] | v2ray <sid> <output> <local_port> [socks_port] [mode] | ss_server <sid> <output> | v2ray_server <sid> <output>\n")
	os.exit(1)
end
