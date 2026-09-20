/**
 * 可达性三态 → 标签/颜色/符号 的**纯映射**。
 *
 * 这是本页面最容易做错、也是规格里点名验收的一条：
 *   ★★ 「未检查」（`unchecked`）**不能**显示成「失效」。
 *   只有 `checked_error` 才算"可能失效"，`unchecked` 必须走中性灰。
 *
 * 所以判定逻辑集中在这一个文件里、做成纯函数，好让 `node --test` 直接验
 * （见 `reachability.test.ts`）—— UI 里只剩"照着 className 贴 class"。
 *
 * 本文件必须保持**可擦除语法**（不用 enum / namespace），
 * 因为测试是用 `node --test` 直接跑 .ts 的（Node 原生类型擦除，不引入 vitest）。
 */

import type { Reachability, ReachabilityState } from './types.ts';

/** 页面用的色调：`ok` 绿 / `error` 琥珀（唯一"可能失效"的一态）/ `unknown` 中性灰。 */
export type ReachabilityTone = 'ok' | 'error' | 'unknown';

export interface ReachabilityView {
  state: ReachabilityState;
  /** 主标签文字：优先用接口给的 `label`（`checked_error` 时它带实际状态码），缺失才用兜底文案。 */
  text: string;
  /** 颜色类名，与 `styles.css` 里的 `.reach-ok` / `.reach-error` / `.reach-unknown` 一一对应。 */
  className: string;
  tone: ReachabilityTone;
  /** 状态符号：让三态在色盲/黑白打印下也分得开。 */
  symbol: string;
  /** 一句话说明「为什么是这个状态」；`unchecked` 时它必须能区分两种没检查的原因。 */
  reason: string;
  /** 检查时间（原始 ISO 字符串），交给调用方按本地时区显示；没检查过就是 `null`。 */
  checkedAt: string | null;
}

const TONE: Record<ReachabilityState, ReachabilityTone> = {
  checked_ok: 'ok',
  checked_error: 'error',
  unchecked: 'unknown',
};

const CLASS_NAME: Record<ReachabilityState, string> = {
  checked_ok: 'reach-ok',
  checked_error: 'reach-error',
  unchecked: 'reach-unknown',
};

const FALLBACK_TEXT: Record<ReachabilityState, string> = {
  checked_ok: '可达',
  checked_error: '不可达',
  unchecked: '未检查',
};

const SYMBOL: Record<ReachabilityState, string> = {
  checked_ok: '✓',
  checked_error: '!',
  unchecked: '?',
};

/**
 * 把接口给的 `state` 收敛到三态。
 * 不认识的取值一律当 `unchecked`：宁可说"不知道"，也不能把不确定的东西画成"失效"。
 */
export function normalizeState(state: string | null | undefined): ReachabilityState {
  if (state === 'checked_ok' || state === 'checked_error' || state === 'unchecked') {
    return state;
  }
  return 'unchecked';
}

export function reachabilityTone(state: string | null | undefined): ReachabilityTone {
  return TONE[normalizeState(state)];
}

export function reachabilityClass(state: string | null | undefined): string {
  return CLASS_NAME[normalizeState(state)];
}

/**
 * `unchecked` 的**两种原因**必须分得清（规格第四节里的 #2/#3 对比 #4）：
 *   · 没有 `official_url`  → 「没有官网网址，无法检查」
 *   · 有 `official_url`    → 「有网址，但当时没检查（网络不通或没跑检查）」
 * 接口给了 `reason` 就用接口的（它才是权威）；没给才按"有没有网址"如实推导，
 * 推导**只说"没检查"**，绝不推成"失效"。
 */
export function uncheckedReason(
  reason: string | null | undefined,
  officialUrl: string | null | undefined,
): string {
  const fromApi = (reason ?? '').trim();
  if (fromApi !== '') {
    return fromApi;
  }
  return officialUrl === null || officialUrl === undefined || officialUrl.trim() === ''
    ? '没有官网网址，无法检查'
    : '有网址，但当时没检查（网络不通或没跑检查）';
}

/** 把接口的一条 `reachability` 翻成页面直接能用的视图对象。 */
export function describeReachability(
  reachability: Reachability | null | undefined,
  officialUrl: string | null | undefined,
): ReachabilityView {
  const state = normalizeState(reachability?.state);
  const label = (reachability?.label ?? '').trim();
  const apiReason = (reachability?.reason ?? '').trim();

  return {
    state,
    text: label !== '' ? label : FALLBACK_TEXT[state],
    className: CLASS_NAME[state],
    tone: TONE[state],
    symbol: SYMBOL[state],
    reason:
      state === 'unchecked'
        ? uncheckedReason(apiReason === '' ? null : apiReason, officialUrl)
        : apiReason !== ''
          ? apiReason
          : FALLBACK_TEXT[state],
    checkedAt: reachability?.checked_at ?? null,
  };
}
