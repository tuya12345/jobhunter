import { formatLocalTime, httpUrlOrNull, orDash } from '../format.ts';
import type { Lead } from '../types.ts';
import { ReachabilityBadge } from './ReachabilityBadge.tsx';

interface Props {
  lead: Lead;
  /** 能不能标记：岗位列表为空 / 还没读出来时为 false（置灰，不造假岗位）。 */
  canApply: boolean;
  /** 置灰的原因，同时作为 title 提示，让用户知道下一步该做什么。 */
  applyDisabledReason: string | null;
  onMarkApplied: (lead: Lead) => void;
}

/** 一条线索。**只读**：这里没有任何编辑/删除线索的入口。 */
export function LeadCard({ lead, canApply, applyDisabledReason, onMarkApplied }: Props) {
  const sourceTypeText = lead.source_type ?? '未分类'; // 接口的 null 就是"未分类"（groups 里也这么写）
  const application = lead.application;
  // ★ 只有 http/https 才渲染成可点链接（JH-10）；其余按纯文本显示，看得见但点不动。
  const officialUrl = httpUrlOrNull(lead.official_url);
  const evidenceUrl = httpUrlOrNull(lead.evidence_url);

  return (
    <article className="lead">
      <header className="lead-head">
        <h3 className="lead-name">{lead.name}</h3>
        <span className="chip chip-source" title="投递方式（source_type）">
          {sourceTypeText}
        </span>
        {application !== null && (
          <span className="chip chip-applied" title="这条线索已有投递记录">
            已投
          </span>
        )}
      </header>

      <dl className="lead-meta">
        <div className="meta-row">
          <dt>投递方式</dt>
          <dd>{sourceTypeText}</dd>
        </div>
        <div className="meta-row">
          <dt>来源平台</dt>
          <dd>{orDash(lead.source_platform)}</dd>
        </div>
        <div className="meta-row">
          <dt>官网链接</dt>
          <dd>
            {lead.official_url === null || lead.official_url.trim() === '' ? (
              <span className="muted">—（这条线索没有官网网址）</span>
            ) : officialUrl !== null ? (
              <a href={officialUrl} target="_blank" rel="noreferrer noopener">
                {officialUrl}
              </a>
            ) : (
              // ★ 非 http(s)（file: / javascript: / data: …）：**只显示，不可点**。
              //   后端导入时已经拒绝这类值，这里是"旧数据 + 将来有人绕过校验"的兜底。
              <span className="muted" title="不是 http/https 链接，已按纯文本显示（不可点击）">
                {lead.official_url}
              </span>
            )}
          </dd>
        </div>
        <div className="meta-row meta-row-wide">
          <dt>备注</dt>
          <dd>{orDash(lead.note)}</dd>
        </div>
        <div className="meta-row">
          <dt>可达性</dt>
          <dd>
            <ReachabilityBadge lead={lead} />
          </dd>
        </div>
      </dl>

      {/* ★ 原始那句话：整段显示，不截断、不加省略号（规格第三节第 4 条） */}
      <blockquote className="evidence" title="线索的原始那句话（evidence_text）">
        {lead.evidence_text}
      </blockquote>
      {lead.evidence_url !== null && lead.evidence_url.trim() !== '' && (
        <p className="evidence-src">
          原始出处：
          {evidenceUrl !== null ? (
            <a href={evidenceUrl} target="_blank" rel="noreferrer noopener">
              {evidenceUrl}
            </a>
          ) : (
            <span className="muted" title="不是 http/https 链接，已按纯文本显示（不可点击）">
              {lead.evidence_url}
            </span>
          )}
        </p>
      )}

      <footer className="lead-foot">
        <div className="lead-foot-left">
          {application === null ? (
            <span className="muted">还没有投递记录</span>
          ) : (
            <span className="applied-line">
              已投：<strong>{application.job_label}</strong>（渠道 {orDash(application.channel)} · 状态{' '}
              {orDash(application.status)}
              {application.applied_at !== null ? ` · ${formatLocalTime(application.applied_at)}` : ''}）
            </span>
          )}
          {lead.collected_at !== null && (
            <span className="muted small">线索采集于 {formatLocalTime(lead.collected_at)}</span>
          )}
        </div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!canApply}
          title={canApply ? '选择岗位后记一次投递' : (applyDisabledReason ?? undefined)}
          onClick={() => onMarkApplied(lead)}
        >
          标记投递…
        </button>
      </footer>
    </article>
  );
}
