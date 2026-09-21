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

/**
 * ★ 只把 `http` / `https` 当作**可以点的链接**（只读代码审查 JH-10）。
 *
 * 后端已经在导入快照时按同一口径拒绝非 HTTP(S)（契约在
 * `leads_svc._SnapshotLead._only_http_urls`），这里是**第二道**：
 *
 *   · 库里可能还躺着导入校验之前写进去的旧值（迁移不会回扫用户数据）；
 *   · 接口返回什么就该显示什么，但"显示"不等于"渲染成可点的 `href`"——
 *     一个 `file:///C:/…` 或 `javascript:…` 变成 `href` 就完全变了性质。
 *
 * 拿不准的一律按**纯文本**显示（看得见、点不动），而不是猜它是安全的。
 */
export function httpUrlOrNull(value: string | null | undefined): string | null {
  const text = (value ?? '').trim();
  if (text === '') {
    return null;
  }
  // 用 URL 解析而不是正则：`HTTP://`、`https:example.com` 这类写法各有各的坑，
  // 交给浏览器自己的解析器，只看它认出来的 protocol。
  try {
    const parsed = new URL(text);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? text : null;
  } catch {
    return null;
  }
}
