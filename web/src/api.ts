/**
 * 页面**唯一**的网络入口。
 *
 * ★ 规则：除了这个文件，任何组件都不许直接写 `fetch` ——
 *   否则字段名/错误处理会各写一份，就又是"同一份数据两个口径"。
 * ★ 页面只调这三个接口（读两个、写一个），没有别的数据来源。
 */

import type { ApplyRequest, ApplyResponse, JobsResponse, LeadsResponse } from './types.ts';

/** 带中文可操作提示的接口错误。 */
export class ApiError extends Error {
  readonly path: string;
  readonly status: number | null;

  constructor(message: string, path: string, status: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.path = path;
    this.status = status;
  }
}

/** 把 FastAPI 的错误体翻成一句中文提示。
 *  `detail` 是字符串（404/400）时直接用；是数组（422 校验错）时逐条拼出来，
 *  绝不吞掉后端给的信息。 */
function errorMessageFromBody(body: unknown, status: number, path: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string' && detail.trim() !== '') {
    return `接口返回 ${status}：${detail}`;
  }
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      const msg = (item as { msg?: unknown } | null)?.msg;
      const loc = (item as { loc?: unknown } | null)?.loc;
      const where = Array.isArray(loc) ? loc.join('.') : '';
      return where === '' ? String(msg) : `${where}: ${String(msg)}`;
    });
    return `接口返回 ${status}：${parts.join('；')}`;
  }
  return `接口返回 ${status}（${path}）`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    // 连不上：最常见的原因就是后端没起 —— 给一句能直接照做的话，不要只抛 "Failed to fetch"
    throw new ApiError(`连不上本地服务（${path}）。先在另一个终端跑：jh leads serve`, path);
  }

  const text = await res.text();
  let body: unknown = null;
  if (text !== '') {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!res.ok) {
    throw new ApiError(errorMessageFromBody(body, res.status, path), path, res.status);
  }
  if (body === null) {
    throw new ApiError(`${path} 没返回 JSON（拿到的是空响应），后端可能没起好`, path, res.status);
  }
  return body as T;
}

/** `GET /api/leads` —— 线索列表（已按 `source_type` 分好组）。 */
export function fetchLeads(): Promise<LeadsResponse> {
  return request<LeadsResponse>('/api/leads');
}

/** `GET /api/jobs` —— 标记投递时选岗位用的只读列表。 */
export function fetchJobs(): Promise<JobsResponse> {
  return request<JobsResponse>('/api/jobs');
}

/**
 * `POST /api/leads/{id}/apply` —— **页面唯一的写动作**。
 * 只由用户点击触发，一次点击一次调用：没有批量、没有自动、没有重试队列。
 */
export function applyLead(leadId: number, body: ApplyRequest): Promise<ApplyResponse> {
  return request<ApplyResponse>(`/api/leads/${leadId}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
