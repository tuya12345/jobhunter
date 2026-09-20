import { useEffect, useState } from 'react';

import type { Job, Lead } from '../types.ts';

interface Props {
  lead: Lead;
  jobs: Job[];
  /** 请求进行中：锁住确认按钮，防手抖点两次（后端也幂等，这里只是别让它看起来像卡住）。 */
  busy: boolean;
  onCancel: () => void;
  onConfirm: (jobId: number) => void;
}

/**
 * 「点一下 → 选一个岗位」。
 *
 * 为什么必须有这一步：`applications.job_id` 是 NOT NULL，一条线索的投递必须挂在某个岗位上
 * （API §1.2）。**页面绝不自己造岗位** —— 岗位列表为空时这个对话框根本打不开，
 * 调用方会把按钮置灰并提示先 `jh job add`。
 *
 * 一次确认 = 一次 POST，没有批量、没有自动重试。
 */
export function ApplyDialog({ lead, jobs, busy, onCancel, onConfirm }: Props) {
  // 已有投递记录时默认选中原来那个岗位：重复点就是"更新已有记录"，正是要如实反馈的场景
  const [selected, setSelected] = useState<number | null>(lead.application?.job_id ?? null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) {
        onCancel();
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [busy, onCancel]);

  return (
    <div
      className="modal-backdrop"
      onClick={() => {
        if (!busy) {
          onCancel();
        }
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="apply-dialog-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="apply-dialog-title">把「{lead.name}」记成一次投递</h2>
        <p className="modal-hint">
          投递记录必须挂在岗位上，请选一个岗位。渠道不在这里填：后端按线索的投递方式（
          {lead.source_type ?? '未分类'}）推导，页面不碰它。
        </p>

        <ul className="job-list">
          {jobs.map((job) => (
            <li key={job.id}>
              <label className={selected === job.id ? 'job-option job-option-selected' : 'job-option'}>
                <input
                  type="radio"
                  name="apply-job"
                  value={job.id}
                  checked={selected === job.id}
                  disabled={busy}
                  onChange={() => setSelected(job.id)}
                />
                <span className="job-label">{job.label}</span>
                <span className="chip chip-status">{job.status}</span>
              </label>
            </li>
          ))}
        </ul>

        {lead.application !== null && (
          <p className="modal-warn">
            这条线索已有投递记录 #{lead.application.application_id}（渠道 {lead.application.channel ?? '—'} ·
            状态 {lead.application.status}）。再记一次是<strong>更新</strong>那条记录，不会新增第二条。
          </p>
        )}

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || selected === null}
            onClick={() => {
              if (selected !== null) {
                onConfirm(selected);
              }
            }}
          >
            {busy ? '提交中…' : '确认标记投递'}
          </button>
        </div>
      </div>
    </div>
  );
}
