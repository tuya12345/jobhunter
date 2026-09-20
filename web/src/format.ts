/** 显示层的两个小工具。刻意不做"人性化相对时间"——投递记录要的是准确时间，不是"3 天前"。 */

const DATE_TIME = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'medium',
  hour12: false,
});

/**
 * 接口给的都是 UTC（ISO 8601 带时区），页面按**用户本地时区**显示（API §1.1）。
 * 认不出来的字符串就原样显示 —— 不假装它是个时间。
 */
export function formatLocalTime(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso.trim() === '') {
    return '—';
  }
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return DATE_TIME.format(parsed);
}

/** `null` / 空白 统一显示成 `—`，免得页面上出现 "null" 这种字。 */
export function orDash(value: string | null | undefined): string {
  const text = (value ?? '').trim();
  return text === '' ? '—' : text;
}
