import { useCallback, useEffect, useMemo, useState } from 'react';

import type { ApplyOutcomeView } from './apply.ts';
import { describeApplyOutcome } from './apply.ts';
import { applyLead, fetchJobs, fetchLeads } from './api.ts';
import { ApplyDialog } from './components/ApplyDialog.tsx';
import { LeadCard } from './components/LeadCard.tsx';
import { Notice, type NoticeState } from './components/Notice.tsx';
import { formatLocalTime } from './format.ts';
import type { Job, Lead, LeadsResponse } from './types.ts';

/** 岗位列表为空时给用户的那句话（措辞照 API §2 与规格，不要改）。 */
const NO_JOBS_HINT = '投递记录必须挂在岗位上：先用 `jh job add <链接>` 录岗位';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 线索页面。
 *
 * 边界（红线，写在这里免得后来者加功能时越界）：
 *   · 线索的**唯一来源**是 `jh leads import`，页面不新增/不删除/不修改线索；
 *   · **唯一的写动作**是用户点一次「标记投递」→ 一次 POST；
 *   · 没有批量标记、没有自动投递、没有定时刷新、没有登录；
 *   · 线索分组直接用 `/api/leads` 返回的 `groups`，前端不重新分组。
 */
export default function App() {
  const [data, setData] = useState<LeadsResponse | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [leadsError, setLeadsError] = useState<string | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  /** 岗位列表的错误提示被用户关掉了（错误本身还留着，标记按钮仍然是灰的）。 */
  const [jobsErrorDismissed, setJobsErrorDismissed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);

  /** 正在标记的那条线索（null = 没开对话框）。 */
  const [target, setTarget] = useState<Lead | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<NoticeState | null>(null);

  // 读接口。两个 GET 各管各的失败：岗位列表读不出来不该让整个页面白屏。
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLeadsError(null);
    setJobsError(null);
    setJobsErrorDismissed(false);

    void (async () => {
      const [leadsResult, jobsResult] = await Promise.allSettled([fetchLeads(), fetchJobs()]);
      if (cancelled) {
        return;
      }
      if (leadsResult.status === 'fulfilled') {
        setData(leadsResult.value);
      } else {
        setLeadsError(errorText(leadsResult.reason));
      }
      if (jobsResult.status === 'fulfilled') {
        setJobs(jobsResult.value.jobs);
      } else {
        setJobs(null);
        setJobsError(errorText(jobsResult.reason));
      }
      setLoading(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  const reload = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  /** 岗位列表能不能用来标记。`null` = 还没读出来或读失败。 */
  const canApply = jobs !== null && jobs.length > 0;
  const applyDisabledReason = useMemo(() => {
    if (jobsError !== null) {
      return '岗位列表没读出来，先解决上面那条错误';
    }
    if (jobs === null) {
      return '岗位列表还在加载…';
    }
    return NO_JOBS_HINT;
  }, [jobs, jobsError]);

  /**
   * 用户确认后的一次写：一次点击 → 一次 POST → 原样显示后端 message → 刷新列表。
   * 没有循环、没有批量、没有自动重试。
   */
  async function handleConfirmApply(jobId: number) {
    if (target === null) {
      return;
    }
    const lead = target;
    setSubmitting(true);
    try {
      // 只传 job_id：status 用后端的默认值，channel 由后端按 source_type 推导
      const res = await applyLead(lead.id, { job_id: jobId });
      const outcome: ApplyOutcomeView = describeApplyOutcome(res);
      setNotice({ kind: 'result', leadName: res.lead_name, outcome });
      setTarget(null);
      reload(); // 刷新列表，让"已投"和投递信息跟上
    } catch (error) {
      setNotice({ kind: 'error', title: '标记投递失败', message: errorText(error) });
    } finally {
      setSubmitting(false);
    }
  }

  const totals = data?.totals ?? null;

  return (
    <div className="page">
      <header className="page-head">
        <div className="page-title">
          <h1>线索</h1>
          <p className="page-sub">
            只读页面：线索只能通过 <code>jh leads import</code> 录入；唯一的写动作是你点一次「标记投递」。
          </p>
        </div>

        <div className="page-head-right">
          <dl className="totals">
            <div className="total">
              <dt>共</dt>
              <dd>{totals === null ? '—' : `${totals.leads} 条`}</dd>
            </div>
            <div className="total">
              <dt>已投</dt>
              <dd>{totals === null ? '—' : `${totals.applied} 条`}</dd>
            </div>
            <div className="total">
              <dt>未检查</dt>
              <dd className={totals !== null && totals.unchecked > 0 ? 'total-unchecked' : undefined}>
                {totals === null ? '—' : `${totals.unchecked} 条`}
              </dd>
            </div>
          </dl>
          <div className="page-head-actions">
            <span className="generated">
              数据生成于 {data === null ? '—' : formatLocalTime(data.generated_at)}
            </span>
            <button type="button" className="btn" onClick={reload} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </div>
      </header>

      {notice !== null && <Notice notice={notice} onDismiss={() => setNotice(null)} />}

      {jobsError !== null && !jobsErrorDismissed && (
        <Notice
          notice={{ kind: 'error', title: '岗位列表没读出来（标记投递先不能用）', message: jobsError }}
          onDismiss={() => setJobsErrorDismissed(true)}
        />
      )}

      {!canApply && jobsError === null && jobs !== null && (
        <p className="banner banner-hint">{NO_JOBS_HINT}</p>
      )}

      {leadsError !== null && (
        <div className="banner banner-error">
          <p className="banner-title">线索没读出来</p>
          <p>{leadsError}</p>
          <button type="button" className="btn" onClick={reload}>
            重试
          </button>
        </div>
      )}

      {data === null && leadsError === null && <p className="muted">正在读取线索…</p>}

      {data !== null && (
        <main className="groups">
          {/* ★ 直接渲染接口给的 groups：前端不重新分组、不排序、不合并 */}
          {data.groups.map((group) => (
            <section className="group" key={String(group.source_type)}>
              <h2 className="group-head">
                {group.label}
                <span className="group-count">{group.count} 条</span>
              </h2>
              {group.leads.length === 0 ? (
                <p className="muted group-empty">这个分组里还没有线索。</p>
              ) : (
                group.leads.map((lead) => (
                  <LeadCard
                    key={lead.id}
                    lead={lead}
                    canApply={canApply}
                    applyDisabledReason={applyDisabledReason}
                    onMarkApplied={setTarget}
                  />
                ))
              )}
            </section>
          ))}
          {data.groups.length === 0 && (
            <p className="muted">
              接口没返回任何分组。线索用 <code>jh leads import &lt;file.json&gt;</code> 导入。
            </p>
          )}
        </main>
      )}

      <footer className="page-foot">
        <p>
          接口契约：<code>docs/P7-消费端API.md</code>。页面不做自动投递、不做批量标记、不增删改线索。
        </p>
      </footer>

      {target !== null && jobs !== null && jobs.length > 0 && (
        <ApplyDialog
          lead={target}
          jobs={jobs}
          busy={submitting}
          onCancel={() => setTarget(null)}
          onConfirm={handleConfirmApply}
        />
      )}
    </div>
  );
}
