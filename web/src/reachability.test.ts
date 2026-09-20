/**
 * 可达性三态的纯函数测试 —— `npm run test`。
 *
 * 为什么值得写：规格把「未检查 ≠ 失效」列为点名验收项，
 * 而这件事的全部逻辑就是 `state → 标签/颜色类名`。把它锁在测试里，
 * 以后有人"顺手"把 unchecked 画成红色，这里会先红。
 *
 * 用 `node --test` 直接跑 .ts（Node 原生类型擦除），**不引入 vitest/jest**。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Reachability } from './types.ts';
import {
  describeReachability,
  normalizeState,
  reachabilityClass,
  reachabilityTone,
  uncheckedReason,
} from './reachability.ts';

function reach(partial: Partial<Reachability>): Reachability {
  return {
    state: 'unchecked',
    label: '',
    http_status: null,
    checked_at: null,
    reason: '',
    ...partial,
  };
}

test('checked_ok → 可达 / 绿 / ok', () => {
  const view = describeReachability(
    reach({
      state: 'checked_ok',
      label: '可达',
      http_status: 200,
      checked_at: '2026-09-19T10:47:36+00:00',
      reason: '检查过：HTTP 200',
    }),
    'https://example.com/xinglan',
  );
  assert.equal(view.state, 'checked_ok');
  assert.equal(view.text, '可达');
  assert.equal(view.className, 'reach-ok');
  assert.equal(view.tone, 'ok');
  assert.equal(view.symbol, '✓');
  assert.equal(view.checkedAt, '2026-09-19T10:47:36+00:00');
});

test('checked_error → 用接口给的 "HTTP 404" / 琥珀 / error', () => {
  const view = describeReachability(
    reach({
      state: 'checked_error',
      label: 'HTTP 404',
      http_status: 404,
      checked_at: '2026-09-19T10:47:36+00:00',
      reason: '检查过：HTTP 404（入口可能已失效）',
    }),
    'https://example.com/xinglan',
  );
  assert.equal(view.text, 'HTTP 404', '文案要用接口的 label（带真实状态码），不要自己编');
  assert.equal(view.className, 'reach-error');
  assert.equal(view.tone, 'error');
  assert.equal(view.symbol, '!');
});

test('★ unchecked 显示「未检查」+ 中性灰，且绝不是 error 态', () => {
  const view = describeReachability(
    reach({ state: 'unchecked', label: '未检查', http_status: null, checked_at: null, reason: '有网址，但当时没检查（网络不通或没跑检查）' }),
    'https://example.invalid/careers',
  );
  assert.equal(view.text, '未检查');
  assert.equal(view.className, 'reach-unknown');
  assert.equal(view.tone, 'unknown');
  assert.equal(view.symbol, '?');

  // ★ 验收断言：未检查不许被合并成"失效"
  assert.notEqual(view.tone, 'error');
  assert.notEqual(view.className, 'reach-error');
  assert.notEqual(view.text, '失效');
  assert.notEqual(view.text, '不可达');
  assert.equal(view.checkedAt, null, '没检查过就不该有时间');
});

test('★ unchecked 的两种原因分得清：没网址 vs 有网址但没查', () => {
  const noUrl = describeReachability(
    reach({ state: 'unchecked', label: '未检查', reason: '没有官网网址，无法检查' }),
    null,
  );
  const hasUrlNotChecked = describeReachability(
    reach({ state: 'unchecked', label: '未检查', reason: '有网址，但当时没检查（网络不通或没跑检查）' }),
    'https://example.invalid/careers',
  );

  assert.equal(noUrl.reason, '没有官网网址，无法检查');
  assert.equal(hasUrlNotChecked.reason, '有网址，但当时没检查（网络不通或没跑检查）');
  assert.notEqual(noUrl.reason, hasUrlNotChecked.reason, '两种"未检查"必须能分辨');
  // 两条都得是中性态，不许因为"没检查"就被画成红
  assert.equal(noUrl.tone, 'unknown');
  assert.equal(hasUrlNotChecked.tone, 'unknown');
});

test('接口没给 reason 时按有没有网址兜底推导（只推"没检查"，不推"失效"）', () => {
  assert.equal(uncheckedReason('', null), '没有官网网址，无法检查');
  assert.equal(uncheckedReason('   ', null), '没有官网网址，无法检查');
  assert.equal(uncheckedReason(null, 'https://example.invalid/careers'), '有网址，但当时没检查（网络不通或没跑检查）');
  assert.equal(uncheckedReason(undefined, undefined), '没有官网网址，无法检查');
  // 接口给了就听接口的，不要覆盖
  assert.equal(uncheckedReason('后端自己的说法', null), '后端自己的说法');
});

test('不认识的 state 收敛到 unchecked（中性），不画成失效', () => {
  assert.equal(normalizeState('checked_timeout'), 'unchecked');
  assert.equal(normalizeState(null), 'unchecked');
  assert.equal(normalizeState(''), 'unchecked');
  assert.equal(reachabilityTone('whatever'), 'unknown');
  assert.equal(reachabilityClass('whatever'), 'reach-unknown');
});

test('label 缺失时用兜底文案，且三态各不相同', () => {
  assert.equal(describeReachability(reach({ state: 'checked_ok' }), 'https://a.example').text, '可达');
  assert.equal(describeReachability(reach({ state: 'checked_error', http_status: 500 }), 'https://a.example').text, '不可达');
  assert.equal(describeReachability(reach({ state: 'unchecked' }), null).text, '未检查');
});

test('reachability 整个字段缺失也不崩（按未检查渲染）', () => {
  const view = describeReachability(null, 'https://example.invalid/careers');
  assert.equal(view.state, 'unchecked');
  assert.equal(view.className, 'reach-unknown');
  assert.equal(view.reason, '有网址，但当时没检查（网络不通或没跑检查）');
});
