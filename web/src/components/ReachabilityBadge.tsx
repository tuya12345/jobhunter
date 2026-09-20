import { formatLocalTime } from '../format.ts';
import { describeReachability } from '../reachability.ts';
import type { Lead } from '../types.ts';

/**
 * 可达性徽标 + 一句「为什么」。
 *
 * ★ 三态必须长得不一样（三态的文字 / 颜色类名 / 符号都由 `reachability.ts` 的纯函数给）：
 *   可达（绿）/ HTTP 404 这类可能失效（琥珀）/ **未检查（中性灰）**。
 * ★ `unchecked` 时 `reason` 是**可见文字**而不是 tooltip —— 用户要能分辨
 *   「没有官网网址」和「有网址但当时没检查」这两件不同的事。
 */
export function ReachabilityBadge({ lead }: { lead: Lead }) {
  const view = describeReachability(lead.reachability, lead.official_url);

  return (
    <div className="reach-cell">
      <span className={`reach ${view.className}`} data-state={view.state}>
        <span className="reach-symbol" aria-hidden="true">
          {view.symbol}
        </span>
        <span className="reach-text">{view.text}</span>
      </span>
      <span className="reach-reason">{view.reason}</span>
      {view.checkedAt !== null && (
        <span className="reach-checked">检查于 {formatLocalTime(view.checkedAt)}</span>
      )}
    </div>
  );
}
