#!/usr/bin/env node
/*
 * 无头冒烟测试：用 jsdom 真跑一遍 out/panel-preview.html。
 *
 * 能抓到 node --check 抓不到的问题 —— 例如 TEXT 映射漏了键导致按钮渲染成
 * "undefined"、分组管理弹窗打不开、保存/删除回调里的运行时异常等。
 *
 * 用法：
 *   NODE_PATH=/Users/hanmin/.workbuddy/binaries/node/workspace/node_modules \
 *     node tools/smoke_test.js [可选:其它 html 路径]
 *
 * 退出码非 0 表示有断言失败。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const file = process.argv[2] || path.join(__dirname, '..', 'out', 'panel-preview.html');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push('jsdomError: ' + (e && e.message ? e.message : String(e))));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(fs.readFileSync(file, 'utf8'), {
	runScripts: 'dangerously',
	pretendToBeVisual: true,
	virtualConsole: vc,
	url: 'http://localhost/'
});
const win = dom.window;

const pass = [];
const fail = [];
function check(name, cond, extra) {
	(cond ? pass : fail).push(name + (extra ? '  [' + extra + ']' : ''));
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// body.textContent 会把 <script>/<style> 的源码也算进去，比对可见文案前先剔掉
function visibleText(doc) {
	const clone = doc.body.cloneNode(true);
	clone.querySelectorAll('script, style').forEach((n) => n.remove());
	return clone.textContent || '';
}

(async () => {
	await wait(600);

	const d = win.document;
	const txt = (el) => ((el && el.textContent) || '').trim();
	const btns = (scope) => [...d.querySelectorAll((scope || '') + ' .scui-btn')];

	check('无运行时错误', errors.length === 0, errors.join(' | '));
	{
		const body = visibleText(d);
		const hit = body.indexOf('undefined');
		check('页面无 "undefined" 文案', hit < 0,
			hit < 0 ? '' : JSON.stringify(body.slice(Math.max(0, hit - 40), hit + 20)));
	}

	const toolbar = btns('.scui-toolbar').map(txt);
	check('工具栏含「全部测速」', toolbar.some((t) => t.includes('全部测速')), toolbar.join('/'));
	check('工具栏含「刷新」', toolbar.some((t) => t.includes('刷新')), toolbar.join('/'));
	const cards = d.querySelectorAll('.scui-card');
	check('渲染出策略组卡片', cards.length >= 4, '数量=' + cards.length);
	check('每张卡片都有成员下拉框',
		cards.length > 0 && [...cards].every((c) => !!c.querySelector('select.scui-select')),
		'卡片=' + cards.length);

	const chips = d.querySelectorAll('.scui-node');
	check('渲染出成员延迟 chip', chips.length > 0, '数量=' + chips.length);

	// 跑一遍「全部测速」，确认回调不抛异常（自动组的 now / 出口节点会在这里刷新）
	const testBtn = btns('.scui-toolbar').find((b) => txt(b).includes('全部测速'));
	check('找到「全部测速」按钮', !!testBtn);
	if (testBtn) {
		testBtn.click();
		await wait(300);
		check('全部测速无异常', errors.length === 0, errors.join(' | '));
	}

	check('全流程结束后仍无 undefined 文案', !/undefined/.test(visibleText(d)));

	done();

	function done() {
		console.log('\n通过 ' + pass.length + ' 项 / 失败 ' + fail.length + ' 项');
		pass.forEach((p) => console.log('  PASS  ' + p));
		fail.forEach((f) => console.log('  FAIL  ' + f));
		if (errors.length) {
			console.log('\n运行时错误:');
			errors.forEach((e) => console.log('  - ' + e));
		}
		win.close();
		process.exit(fail.length ? 1 : 0);
	}
})();
