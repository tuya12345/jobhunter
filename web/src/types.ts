/**
 * `docs/P7-消费端API.md` 的 TypeScript 抄本。
 *
 * ★ 字段名**一个都不许改**，也不要在这里"顺手优化"成别的名字 ——
 *   改接口必须由后端先改那份契约，然后这份文件跟着改。
 *   这里只声明页面真正读到的字段，没读的字段不写（写了也没人用）。
 */

/** 投递方式。`null` = 未分类（接口的 `groups[].source_type` 用 `null` 表示这一组）。 */
export type SourceType = '官网' | '内推' | 'HR直招' | null;

/**
 * 可达性**三态**（API §1.1）。
 * ★ `unchecked` 是「未检查」，不是「失效」；`checked_error` 才是"可能失效"。
 */
export type ReachabilityState = 'checked_ok' | 'checked_error' | 'unchecked';

export interface Reachability {
  state: ReachabilityState;
  /** 接口给的展示文案：`可达` / `HTTP 404`（实际状态码）/ `未检查`。 */
  label: string;
  http_status: number | null;
  checked_at: string | null;
  /**
   * 为什么是这个状态。`unchecked` 时它必须能区分两种原因：
   * 「没有官网网址，无法检查」vs「有网址，但当时没检查（网络不通或没跑检查）」。
   */
  reason: string;
}

/** 这条线索已经产生的投递记录（没有则整个对象是 `null`）。 */
export interface LeadApplication {
  application_id: number;
  job_id: number;
  job_label: string;
  /** 契约样例是字符串，但 POST 响应里 `channel_before` 可为 null → 这里按可空处理并在页面上兜底显示。 */
  channel: string | null;
  status: string;
  applied_at: string | null;
}

export interface Lead {
  id: number;
  name: string;
  external_id: number | null;
  official_url: string | null;
  source_platform: string | null;
  source_type: SourceType;
  /** 原始那句话 —— 页面必须整句显示，不许截断成一个词。 */
  evidence_text: string;
  evidence_url: string | null;
  note: string | null;
  collected_at: string | null;
  reachability: Reachability;
  application: LeadApplication | null;
}

export interface LeadGroup {
  source_type: SourceType;
  /** 分组标题（`官网` / `内推` / `HR直招` / `未分类`），页面直接用它，不要自己拼。 */
  label: string;
  count: number;
  leads: Lead[];
}

export interface LeadsTotals {
  leads: number;
  applied: number;
  unchecked: number;
}

export interface LeadsResponse {
  generated_at: string;
  totals: LeadsTotals;
  /** ★ 分组是后端给的：页面**直接照着渲染，不做二次分组**（API §1）。 */
  groups: LeadGroup[];
}

export interface Job {
  id: number;
  company: string;
  title: string;
  city: string | null;
  status: string;
  /** 已经拼好的展示名，如 `甲公司 · Python 后端开发实习生（深圳）`。 */
  label: string;
}

export interface JobsResponse {
  /** 空数组是合法状态 → 页面把「标记投递」置灰并提示先 `jh job add`。 */
  jobs: Job[];
}

/** POST /api/leads/{id}/apply 的请求体。 */
export interface ApplyRequest {
  job_id: number;
  /** 可省，默认 `applied`。页面不传，交给后端用自己的默认值。 */
  status?: string;
  /** 可省；省略时由线索的 `source_type` 推导。页面**不传**，避免造出第二个渠道口径。 */
  channel?: string | null;
}

/** POST /api/leads/{id}/apply 的响应。 */
export interface ApplyResponse {
  lead_id: number;
  lead_name: string;
  application_id: number;
  created: boolean;
  channel: string | null;
  channel_before: string | null;
  channel_changed: boolean;
  status: string;
  status_before: string | null;
  status_changed: boolean;
  /**
   * ★ 必须**原样**显示给用户：它区分了「新建 / 更新了已有记录（渠道 X → Y）/ 没有变化」。
   *   用一句"✓ 成功"盖掉它就是 ISSUE #57 的原症状。
   */
  message: string;
}

/** `GET /api/health`（页面暂未使用，先按契约放着，方便以后排查）。 */
export interface HealthResponse {
  ok: boolean;
  version: string;
  leads: number;
  jobs: number;
  frontend: boolean;
}
