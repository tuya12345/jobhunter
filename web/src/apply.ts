/**
 * 把 `POST /api/leads/{id}/apply` 的响应翻成页面提示。
 *
 * ★ 这一条是 ISSUE #57 的对症处理：
 *   `leads apply` 当时静默改写了渠道，输出和第一次一模一样，用户看不出来。
 *   所以页面**必须**显示接口给的 `message` 原文，并且额外用结构化字段
 *   （`created` / `channel_changed` / `status_changed`）给一个可扫读的标签：
 *   新建 / 更新了已有记录 / 没有变化 —— 三种结果在页面上长得不一样。
 */

import type { ApplyResponse } from './types.ts';

export type ApplyOutcomeKind = 'created' | 'updated' | 'unchanged' | 'conflict';

export interface ApplyOutcomeView {
  kind: ApplyOutcomeKind;
  /** 小标签文字，三种结果各不相同。 */
  chip: string;
  /** 标签的类名（与 styles.css 对应）。 */
  className: string;
  /** ★ 接口返回的 `message`，**原样**透出，不许替换成"✓ 成功"。 */
  message: string;
  /** 结构化事实，跟着 message 一起显示，便于一眼看出渠道/状态有没有被改写。 */
  facts: string[];
}

/** 四件事：新建了没有 / 改了什么 / 什么都没改 / **归属属于另一条线索**（没记上）。 */
export function classifyApplyOutcome(
  res: Pick<ApplyResponse, 'created' | 'channel_changed' | 'status_changed' | 'owned_by_other_lead'>,
): ApplyOutcomeKind {
  if (res.created) {
    return 'created';
  }
  // ★ 归属冲突优先于"更新/没有变化"：这种情况下**线索页什么都不会变**，
  //   把它说成"更新了已有记录"就是 JH-02 那一类失真（页面与实际不符）。
  if (res.owned_by_other_lead) {
    return 'conflict';
  }
  return res.channel_changed || res.status_changed ? 'updated' : 'unchanged';
}

function describeChannel(channel: string | null | undefined): string {
  const value = (channel ?? '').trim();
  return value === '' ? '（无渠道）' : value;
}

/** `status_before` 为 null（新建）时不要写成"（无渠道）"，那是渠道的说法。 */
function describeStatus(status: string | null | undefined): string {
  const value = (status ?? '').trim();
  return value === '' ? '（无）' : value;
}

/** 结构化事实（只在真的变了/真的有值时列出，不制造噪音）。 */
function outcomeFacts(res: ApplyResponse): string[] {
  const facts: string[] = [`记录 #${res.application_id}`];
  if (res.owned_by_other_lead) {
    // 归属没改 → 渠道/状态就说"未变"，不要摆出一堆"更新"的样子
    facts.push('线索归属：属于另一条线索（未改动）');
  }
  if (res.channel_changed) {
    facts.push(`渠道 ${describeChannel(res.channel_before)} → ${describeChannel(res.channel)}`);
  } else {
    facts.push(`渠道 ${describeChannel(res.channel)}（未变）`);
  }
  if (res.status_changed) {
    facts.push(`状态 ${describeStatus(res.status_before)} → ${describeStatus(res.status)}`);
  } else {
    facts.push(`状态 ${describeStatus(res.status)}（未变）`);
  }
  return facts;
}

const CHIP: Record<ApplyOutcomeKind, string> = {
  created: '新建了投递记录',
  updated: '更新了已有记录',
  unchanged: '没有变化',
  conflict: '这条线索没有记上（归属属于另一条线索）',
};

const CHIP_CLASS: Record<ApplyOutcomeKind, string> = {
  created: 'outcome-created',
  updated: 'outcome-updated',
  unchanged: 'outcome-unchanged',
  conflict: 'outcome-conflict',
};

export function describeApplyOutcome(res: ApplyResponse): ApplyOutcomeView {
  const kind = classifyApplyOutcome(res);
  const message = (res.message ?? '').trim();
  return {
    kind,
    chip: CHIP[kind],
    className: CHIP_CLASS[kind],
    // 接口没给 message 时**不编一句**：如实说接口没给，免得把契约缺口伪装成正常输出。
    message: message !== '' ? res.message : '接口没有返回 message（契约要求必须有，请检查后端）',
    facts: outcomeFacts(res),
  };
}
