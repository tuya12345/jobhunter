/**
 * 标记投递反馈的纯函数测试 —— `npm run test`。
 *
 * 为什么值得写：ISSUE #57 的原症状是"静默改写渠道，输出和第一次一模一样"。
 * 这里锁住三件事：
 *   1. 三种结果（新建 / 更新 / 没有变化）在页面上是**三种**不同的样子；
 *   2. 接口的 `message` 是**原样**透出的，没有被替换成"✓ 成功"；
 *   3. 渠道/状态真的变了时，页面必须列出 "X → Y"。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ApplyResponse } from './types.ts';
import { classifyApplyOutcome, describeApplyOutcome } from './apply.ts';

function response(partial: Partial<ApplyResponse>): ApplyResponse {
  return {
    lead_id: 1,
    lead_name: '星澜科技',
    application_id: 1,
    created: true,
    channel: '官网',
    channel_before: null,
    channel_changed: false,
    status: 'applied',
    status_before: 'pending',
    status_changed: true,
    message: '已记入投递看板：新建记录 #1（渠道 官网，状态 applied）',
    ...partial,
  };
}

test('第一次点：新建', () => {
  const view = describeApplyOutcome(response({ created: true }));
  assert.equal(view.kind, 'created');
  assert.equal(view.chip, '新建了投递记录');
  assert.equal(view.className, 'outcome-created');
  // ★ message 原样透出
  assert.equal(view.message, '已记入投递看板：新建记录 #1（渠道 官网，状态 applied）');
});

test('★ 第二次点且渠道被改写：必须说出 "官网 → 内推"，不能和第一次长得一样', () => {
  const res = response({
    created: false,
    channel: '内推',
    channel_before: '官网',
    channel_changed: true,
    status_before: 'pending',
    status_changed: true,
    message: '更新了已有记录 #1：渠道 官网 → 内推；状态 pending → applied',
  });
  const view = describeApplyOutcome(res);

  assert.equal(view.kind, 'updated');
  assert.equal(view.chip, '更新了已有记录');
  assert.equal(view.className, 'outcome-updated');
  // message 是后端的原话，逐字一致（不许被前端改写/截断/加"✓"）
  assert.equal(view.message, res.message);
  // 结构化事实里也要能看见渠道变化
  assert.ok(
    view.facts.some((f) => f === '渠道 官网 → 内推'),
    `facts 里必须能看见渠道变化，实际：${JSON.stringify(view.facts)}`,
  );
  assert.ok(view.facts.some((f) => f === '状态 pending → applied'));

  // ★ 反 #57：更新态与新建态在页面上必须不同
  const createdView = describeApplyOutcome(response({ created: true }));
  assert.notEqual(view.kind, createdView.kind);
  assert.notEqual(view.chip, createdView.chip);
  assert.notEqual(view.className, createdView.className);
  assert.notEqual(view.message, createdView.message);
});

test('第二次点且什么都没变：说"没有变化"', () => {
  const res = response({
    created: false,
    channel: '内推',
    channel_before: '内推',
    channel_changed: false,
    status_before: 'applied',
    status_changed: false,
    message: '没有变化：记录 #1 已经是 渠道 内推 / 状态 applied',
  });
  const view = describeApplyOutcome(res);

  assert.equal(view.kind, 'unchanged');
  assert.equal(view.chip, '没有变化');
  assert.equal(view.className, 'outcome-unchanged');
  assert.equal(view.message, res.message);
  assert.ok(view.facts.some((f) => f === '渠道 内推（未变）'));
  assert.ok(view.facts.some((f) => f === '状态 applied（未变）'));
});

test('三种结果的标签/类名两两不同', () => {
  const kinds = [
    describeApplyOutcome(response({ created: true })),
    describeApplyOutcome(response({ created: false, channel_changed: true })),
    describeApplyOutcome(response({ created: false, channel_changed: false, status_changed: false })),
  ];
  assert.equal(new Set(kinds.map((k) => k.chip)).size, 3);
  assert.equal(new Set(kinds.map((k) => k.className)).size, 3);
});

test('新建时 status_before 是 null：写成"（无）"，不要写成"（无渠道）"', () => {
  const view = describeApplyOutcome(
    response({ created: true, status_before: null, status_changed: true, channel_changed: false }),
  );
  assert.ok(view.facts.some((f) => f === '状态 （无） → applied'), JSON.stringify(view.facts));
  assert.ok(!view.facts.join(' ').includes('状态 （无渠道）'), '状态为空时不能借用渠道的措辞');
});

test('classifyApplyOutcome：只有真的变了才算更新', () => {
  assert.equal(classifyApplyOutcome({ created: true, channel_changed: true, status_changed: true }), 'created');
  assert.equal(classifyApplyOutcome({ created: false, channel_changed: true, status_changed: false }), 'updated');
  assert.equal(classifyApplyOutcome({ created: false, channel_changed: false, status_changed: true }), 'updated');
  assert.equal(classifyApplyOutcome({ created: false, channel_changed: false, status_changed: false }), 'unchanged');
});

test('channel 为 null 时不显示成 "null" 字面量', () => {
  const view = describeApplyOutcome(
    response({ created: false, channel: null, channel_before: null, channel_changed: false, status_changed: false }),
  );
  assert.ok(!view.facts.join(' ').includes('null'), `facts 里不该出现 null：${JSON.stringify(view.facts)}`);
  assert.ok(view.facts.some((f) => f === '渠道 （无渠道）（未变）'));
});

test('接口没给 message 时如实说"没给"，不编一句假的成功文案', () => {
  const view = describeApplyOutcome(response({ message: '' }));
  assert.ok(view.message.includes('没有返回 message'));
  assert.ok(!view.message.includes('成功'));
});
