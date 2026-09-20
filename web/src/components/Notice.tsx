import type { ApplyOutcomeView } from '../apply.ts';

/** 页面顶部的一条提示。要么是标记投递的结果，要么是某处出错。 */
export type NoticeState =
  | { kind: 'result'; leadName: string; outcome: ApplyOutcomeView }
  | { kind: 'error'; title: string; message: string };

interface Props {
  notice: NoticeState;
  onDismiss: () => void;
}

/**
 * 提示条。
 *
 * ★ 这里是 ISSUE #57 的对症处：`message` 是**接口原文**，
 *   不做任何替换、截断或"美化"；另外附一个由结构化字段推出的标签
 *   （新建 / 更新了已有记录 / 没有变化），让三种结果一眼可辨。
 *   `role="status"` 让读屏软件也会念出来。
 */
export function Notice({ notice, onDismiss }: Props) {
  const isError = notice.kind === 'error';
  return (
    <div
      className={isError ? 'notice notice-error' : 'notice notice-result'}
      role="status"
      aria-live="polite"
    >
      <div className="notice-body">
        {isError ? (
          <>
            <p className="notice-title">{notice.title}</p>
            <p className="notice-message">{notice.message}</p>
          </>
        ) : (
          <>
            <p className="notice-title">
              「{notice.leadName}」
              <span className={`outcome-chip ${notice.outcome.className}`}>{notice.outcome.chip}</span>
            </p>
            {/* ↓↓↓ 后端的原话，逐字显示 */}
            <p className="notice-message">{notice.outcome.message}</p>
            <p className="notice-facts">{notice.outcome.facts.join('；')}</p>
          </>
        )}
      </div>
      <button type="button" className="notice-close" onClick={onDismiss} aria-label="关闭提示">
        ×
      </button>
    </div>
  );
}
