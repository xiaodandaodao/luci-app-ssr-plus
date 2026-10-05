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
	check('工具栏含「分组管理」', toolbar.some((t) => t.includes('分组管理')), toolbar.join('/'));

	const cards = d.querySelectorAll('.scui-card');
	check('渲染出策略组卡片', cards.length >= 4, '数量=' + cards.length);

	const gmBtn = btns().find((b) => txt(b).includes('分组管理'));
	check('找到「分组管理」按钮', !!gmBtn);
	if (!gmBtn) return done();

	gmBtn.click();
	check('分组管理弹窗已打开', !!d.querySelector('.scui-gm.is-open'));
	await wait(400); // refresh() 走桩 XHR，内容是异步渲染的

	const note = d.querySelector('.scui-gm-note');
	check('显示地区分组状态', !!note && /地区自动分组/.test(txt(note)), txt(note).slice(0, 48));

	const items = d.querySelectorAll('.scui-gm-item');
	check('列出现有自定义分组', items.length === 2, '数量=' + items.length);
	const names = [...items].map((i) => txt(i.querySelector('.scui-gm-item-name')));
	check('分组名与模式徽标正确',
		names.includes('港新低延迟') && names.includes('流媒体备用'),
		names.join('/'));
	check('分组显示节点数而非 undefined',
		[...items].every((i) => /\d+ 个节点/.test(txt(i))),
		[...items].map((i) => txt(i.querySelector('.scui-gm-item-meta'))).join('/'));
	check('编辑/删除按钮文案正常',
		[...items].every((i) => /编辑/.test(txt(i)) && /删除/.test(txt(i))));

	const addBtn = btns('.scui-gm-actions').find((b) => txt(b).includes('新建分组'));
	check('有「新建分组」按钮', !!addBtn);
	if (!addBtn) return done();

	addBtn.click();
	const labels = [...d.querySelectorAll('.scui-gm-label')].map(txt);
	check('编辑器含「分组名称」字段', labels.includes('分组名称'), labels.join('/'));
	check('编辑器含「模式」字段', labels.includes('模式'), labels.join('/'));

	const nodeRows = d.querySelectorAll('.scui-gm-node');
	check('节点多选列表已渲染', nodeRows.length >= 20, '数量=' + nodeRows.length);

	const sub = btns('.scui-gm-subhead').map(txt);
	check('有「全选」按钮', sub.some((t) => t.includes('全选')), sub.join('/'));
	check('有「清空」按钮', sub.some((t) => t.includes('清空')), sub.join('/'));

	const acts = btns('.scui-gm-actions').map(txt);
	check('有「保存」按钮', acts.some((t) => t.includes('保存')), acts.join('/'));
	check('有「取消」按钮', acts.some((t) => t.includes('取消')), acts.join('/'));

	// ---- 走一遍新建流程：填名 -> 勾节点 -> 保存
	const nameInput = d.querySelector('.scui-gm-input');
	const firstBox = d.querySelector('.scui-gm-node input[type=checkbox]');
	const saveBtn = btns('.scui-gm-actions').find((b) => txt(b).includes('保存'));
	check('表单控件齐全', !!nameInput && !!firstBox && !!saveBtn);
	if (nameInput && firstBox && saveBtn) {
		nameInput.value = '测试分组';
		nameInput.dispatchEvent(new win.Event('input', { bubbles: true }));
		firstBox.checked = true;
		firstBox.dispatchEvent(new win.Event('change', { bubbles: true }));
		saveBtn.click();
		await wait(400);

		const after = d.querySelectorAll('.scui-gm-item');
		check('保存后新分组进入列表',
			[...after].some((i) => txt(i).includes('测试分组')),
			'数量=' + after.length);
		check('保存流程无异常', errors.length === 0, errors.join(' | '));
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
