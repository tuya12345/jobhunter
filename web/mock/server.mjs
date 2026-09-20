/**
 * 假后端（**只在开发时用**，不是产品的一部分）。
 *
 * 为什么有它：`jh leads serve`（真后端）还没落地时，页面也得能开发、能验收。
 * 它零依赖（只用 node:http），实现的接口与 `docs/P7-消费端API.md` 一一对应，
 * 数据用的是 `docs/leads-snapshot-sample.json` 里那 4 条虚构线索 —— 字段名/空值语义
 * 都照契约抄，方便对着契约挑毛病。
 *
 *   node mock/server.mjs                 # 8787 端口，同时提供 /api/* 与 web/dist 静态文件
 *   MOCK_NO_JOBS=1 node mock/server.mjs  # 岗位列表为空 → 验证「标记投递」置灰那条路径
 *
 * ★ 它**不是**第二个写入口：进程内存里的投递记录，重启就没了；真实投递记录只由
 *   `jh leads apply` / 页面这一个 POST 走 apply_svc 落库。
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.env.PORT ?? 8787);
const HOST = process.env.HOST ?? '127.0.0.1';
const NO_JOBS = process.env.MOCK_NO_JOBS === '1';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const DIST = resolve(HERE, '..', 'dist');

const NOW = () => new Date().toISOString();

// ---------------------------------------------------------------- 假数据（全部虚构）

/** @type {{id:number, name:string, external_id:number, official_url:string|null, source_platform:string|null, source_type:string|null, evidence_text:string, evidence_url:string|null, note:string|null, http_status:number|null, checked_at:string|null, collected_at:string}[]} */
const COMPANIES = [
  {
    id: 1,
    name: '星澜科技',
    external_id: 1,
    official_url: 'https://example.com/xinglan',
    source_platform: '小红书',
    source_type: '官网',
    evidence_text:
      '刷小红书看到「星澜科技」在招 AI Agent 方向，官网投递 https://example.com/xinglan',
    evidence_url: null,
    note: 'AI Agent 方向',
    http_status: 404,
    checked_at: '2026-09-19T10:47:36+00:00',
    collected_at: '2026-09-19T10:47:17+00:00',
  },
  {
    id: 2,
    name: '云枢智能',
    external_id: 2,
    official_url: null,
    source_platform: null,
    source_type: '内推',
    evidence_text: '有人内推「云枢智能」，简历发 hr@example.com，说是 NLP 组，base 深圳',
    evidence_url: null,
    note: 'NLP 组，base 深圳；内推人邮箱 hr@example.com',
    http_status: null, // ← 没有网址，所以没检查
    checked_at: null,
    collected_at: '2026-09-19T10:47:17+00:00',
  },
  {
    id: 3,
    name: '明澈数据',
    external_id: 3,
    official_url: null,
    source_platform: '知乎',
    source_type: 'HR直招',
    evidence_text: '【知乎】「明澈数据」HR 直招，加微信 hr_mingche，做后端开发，急招',
    evidence_url: null,
    note: '后端开发，急招；HR 微信 hr_mingche',
    http_status: null, // ← 没有网址，所以没检查
    checked_at: null,
    collected_at: '2026-09-19T10:47:17+00:00',
  },
  {
    id: 4,
    name: '虚构网络',
    external_id: 4,
    official_url: 'https://example.invalid/careers',
    source_platform: '贴吧',
    source_type: '官网',
    evidence_text: '贴吧看到「虚构网络」招 Python 实习生，招聘页 https://example.invalid/careers',
    evidence_url: null,
    note: 'Python 实习生',
    http_status: null, // ← 有网址，但当时没检查（网络不通）
    checked_at: null,
    collected_at: '2026-09-19T10:47:17+00:00',
  },
  {
    // ★ 这一条**不在** docs/leads-snapshot-sample.json 里，是为了让开发时能一眼看到
    //   第三种状态（checked_ok / 可达，绿色）而加的 —— 真实快照里 4 条只有 404 和未检查。
    id: 5,
    name: '青梧云',
    external_id: 5,
    official_url: 'https://example.com/qingwu',
    source_platform: '官网',
    source_type: '官网',
    evidence_text: '官网招聘页「青梧云」在招后端开发（Python），投递入口 https://example.com/qingwu',
    evidence_url: null,
    note: '后端开发（Python）',
    http_status: 200,
    checked_at: '2026-09-19T10:48:02+00:00',
    collected_at: '2026-09-19T10:47:17+00:00',
  },
];

const JOBS = NO_JOBS
  ? []
  : [
      {
        id: 3,
        company: '甲公司',
        title: 'Python 后端开发实习生',
        city: '深圳',
        status: 'pending',
        label: '甲公司 · Python 后端开发实习生（深圳）',
      },
    ];

/** 投递记录只存在内存里（假的）。 */
const APPLICATIONS = new Map(); // `${leadId}:${jobId}` -> {...}
let nextApplicationId = 1;

const APPLY_STATUSES = ['pending', 'viewed', 'applied', 'replied', 'interview', 'offer', 'rejected'];

// ---------------------------------------------------------------- 契约里的派生逻辑（只在假后端里实现）

function reachabilityOf(lead) {
  if (lead.http_status === null) {
    return {
      state: 'unchecked',
      label: '未检查',
      http_status: null,
      checked_at: null,
      reason:
        lead.official_url === null
          ? '没有官网网址，无法检查'
          : '有网址，但当时没检查（网络不通或没跑检查）',
    };
  }
  if (lead.http_status >= 200 && lead.http_status < 400) {
    return {
      state: 'checked_ok',
      label: '可达',
      http_status: lead.http_status,
      checked_at: lead.checked_at,
      reason: `检查过：HTTP ${lead.http_status}`,
    };
  }
  return {
    state: 'checked_error',
    label: `HTTP ${lead.http_status}`,
    http_status: lead.http_status,
    checked_at: lead.checked_at,
    reason: `检查过：HTTP ${lead.http_status}（入口可能已失效）`,
  };
}

function applicationOf(leadId) {
  for (const app of APPLICATIONS.values()) {
    if (app.leadId === leadId) {
      const job = JOBS.find((j) => j.id === app.jobId);
      return {
        application_id: app.id,
        job_id: app.jobId,
        job_label: job?.label ?? `岗位 #${app.jobId}`,
        channel: app.channel,
        status: app.status,
        applied_at: app.appliedAt,
      };
    }
  }
  return null;
}

/** 分组顺序与标签照 `docs/P7-消费端API.md` §1：官网 / 内推 / HR直招 / 未分类。 */
function buildGroups() {
  const order = ['官网', '内推', 'HR直招', null];
  const labelOf = (sourceType) => sourceType ?? '未分类';
  return order.map((sourceType) => {
    const leads = COMPANIES.filter((c) => c.source_type === sourceType).map((c) => ({
      id: c.id,
      name: c.name,
      external_id: c.external_id,
      official_url: c.official_url,
      source_platform: c.source_platform,
      source_type: c.source_type,
      evidence_text: c.evidence_text,
      evidence_url: c.evidence_url,
      note: c.note,
      collected_at: c.collected_at,
      reachability: reachabilityOf(c),
      application: applicationOf(c.id),
    }));
    return { source_type: sourceType, label: labelOf(sourceType), count: leads.length, leads };
  });
}

// ---------------------------------------------------------------- HTTP

function sendJson(res, status, body) {
  const payload = JSON.stringify(body, null, 2);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(payload);
}

function sendHtml(res, status, html) {
  res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(html);
}

async function readJsonBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  if (text.trim() === '') {
    return {};
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function handleApply(req, res, leadId) {
  const lead = COMPANIES.find((c) => c.id === leadId);
  if (lead === undefined) {
    sendJson(res, 404, { detail: `线索 #${leadId} 不存在：先用 jh leads import 导入线索快照` });
    return;
  }

  const body = await readJsonBody(req);
  if (body === null) {
    sendJson(res, 400, { detail: '请求体不是合法 JSON' });
    return;
  }

  const job = JOBS.find((j) => j.id === body.job_id);
  if (body.job_id === undefined || job === undefined) {
    sendJson(res, 404, { detail: `岗位 #${body.job_id} 不存在：先用 jh job add <链接> 录岗位` });
    return;
  }

  const status = body.status ?? 'applied';
  if (!APPLY_STATUSES.includes(status)) {
    sendJson(res, 400, { detail: `状态 ${status} 不认识，可用：${APPLY_STATUSES.join('/')}` });
    return;
  }

  const channel = body.channel ?? lead.source_type ?? '其他'; // 省略时由线索的 source_type 推导
  const key = `${lead.id}:${job.id}`;
  const existing = APPLICATIONS.get(key);

  if (existing === undefined) {
    const created = {
      id: nextApplicationId++,
      leadId: lead.id,
      jobId: job.id,
      channel,
      status,
      appliedAt: NOW(),
    };
    APPLICATIONS.set(key, created);
    sendJson(res, 200, {
      lead_id: lead.id,
      lead_name: lead.name,
      application_id: created.id,
      created: true,
      channel,
      channel_before: null,
      channel_changed: false,
      status,
      status_before: null,
      status_changed: true,
      message: `已记入投递看板：新建记录 #${created.id}（渠道 ${channel}，状态 ${status}）`,
    });
    return;
  }

  const channelChanged = existing.channel !== channel;
  const statusChanged = existing.status !== status;
  let message;
  if (!channelChanged && !statusChanged) {
    message = `没有变化：记录 #${existing.id} 已经是 渠道 ${existing.channel} / 状态 ${existing.status}`;
  } else {
    const parts = [];
    if (channelChanged) {
      parts.push(`渠道 ${existing.channel} → ${channel}`);
    }
    if (statusChanged) {
      parts.push(`状态 ${existing.status} → ${status}`);
    }
    message = `更新了已有记录 #${existing.id}：${parts.join('；')}`;
  }

  const before = { channel: existing.channel, status: existing.status };
  existing.channel = channel;
  existing.status = status;

  sendJson(res, 200, {
    lead_id: lead.id,
    lead_name: lead.name,
    application_id: existing.id,
    created: false,
    channel,
    channel_before: before.channel,
    channel_changed: channelChanged,
    status,
    status_before: before.status,
    status_changed: statusChanged,
    message,
  });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.map': 'application/json; charset=utf-8',
};

async function serveStatic(res, urlPath) {
  const indexHtml = join(DIST, 'index.html');
  const requested = urlPath.endsWith('/') ? `${urlPath}index.html` : urlPath; // `/` → `/index.html`
  const candidate = normalize(join(DIST, requested));
  const inDist = candidate.startsWith(DIST) && candidate !== DIST;
  let file = inDist ? candidate : indexHtml;

  if (!existsSync(file) && extname(urlPath) === '') {
    file = indexHtml; // SPA 兜底
  }
  if (!existsSync(file)) {
    // 与 docs/P7-消费端API.md §5 同样的行为：可读提示，不是白屏、不是裸 500
    sendHtml(
      res,
      503,
      '<!doctype html><meta charset="utf-8"><h1>前端还没构建</h1><p>先跑 <code>npm ci &amp;&amp; npm run build</code>（在 <code>web/</code> 目录）。</p>',
    );
    return;
  }
  const content = await readFile(file);
  res.writeHead(200, { 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream' });
  res.end(content);
}

const server = createServer((req, res) => {
  void (async () => {
    const url = new URL(req.url ?? '/', `http://${req.headers.host ?? `${HOST}:${PORT}`}`);
    const path = url.pathname;

    if (path === '/api/health') {
      sendJson(res, 200, {
        ok: true,
        version: '0.1.0-mock',
        leads: COMPANIES.length,
        jobs: JOBS.length,
        frontend: existsSync(join(DIST, 'index.html')),
      });
      return;
    }

    if (path === '/api/leads' && req.method === 'GET') {
      const groups = buildGroups();
      const all = groups.flatMap((g) => g.leads);
      sendJson(res, 200, {
        generated_at: NOW(),
        totals: {
          leads: all.length,
          applied: all.filter((l) => l.application !== null).length,
          unchecked: all.filter((l) => l.reachability.state === 'unchecked').length,
        },
        groups,
      });
      return;
    }

    if (path === '/api/jobs' && req.method === 'GET') {
      sendJson(res, 200, { jobs: JOBS });
      return;
    }

    const applyMatch = /^\/api\/leads\/(\d+)\/apply$/.exec(path);
    if (applyMatch !== null && req.method === 'POST') {
      await handleApply(req, res, Number(applyMatch[1]));
      return;
    }

    if (path.startsWith('/api/')) {
      sendJson(res, 404, { detail: `假后端没有这个接口：${req.method} ${path}` });
      return;
    }

    await serveStatic(res, path);
  })().catch((error) => {
    sendJson(res, 500, { detail: `假后端自己崩了：${String(error)}` });
  });
});

server.listen(PORT, HOST, () => {
  console.log(`[假后端] http://${HOST}:${PORT}  ← 这是 mock，不是 jh leads serve`);
  console.log(`[假后端] 静态目录：${DIST}${existsSync(join(DIST, 'index.html')) ? '' : '（还没有 dist/index.html，先 npm run build）'}`);
  console.log(`[假后端] 岗位列表：${NO_JOBS ? '空（MOCK_NO_JOBS=1，用于验证按钮置灰）' : `${JOBS.length} 条`}`);
});
