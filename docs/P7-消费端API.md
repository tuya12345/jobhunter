# P7 消费端 API 契约（后端 ↔ 页面）

> 需求来源：`docs/P7-消费端规格.md`（验证会话整理，用户 2026-09-19 拍板）。
> **这份是开发会话定下的实现契约**：页面（`web/`）与后端（`src/jobhunter/web/`）之间**只走这一份**。
> 改接口必须同时改这里 —— 否则就是"同一份数据两个口径"的老毛病。

---

## 0. 形态

- `jh leads serve [--host 127.0.0.1] [--port 8787]` 起一个 **FastAPI** 应用。
- **默认只绑 `127.0.0.1`**（红线：本地隐私工具，绑 `0.0.0.0` 会让同局域网的人看到线索）。
  端口默认 **8787**（8765 被 StageBoard 占用）。
- 同一个进程同时提供：
  - `GET /api/*` —— 下面这些 JSON 接口；
  - `/` 与静态资源 —— `web/dist` 里的前端构建产物（SPA）。
- **不做鉴权**（本地工具，只监听回环地址）；`docs/COMPLIANCE.md` 有专门一节说明。
- **不做自动投递**：唯一的写动作是"用户点一下 → 记一次投递"。

---

## 1. `GET /api/leads`

线索列表，**按 `source_type` 分组**（页面直接照着渲染，不做二次分组）。

```json
{
  "generated_at": "2026-09-20T02:10:00+00:00",
  "totals": { "leads": 4, "applied": 1, "unchecked": 2 },
  "groups": [
    {
      "source_type": "官网",
      "label": "官网",
      "count": 1,
      "leads": [
        {
          "id": 1,
          "name": "星澜科技",
          "external_id": 1,
          "official_url": "https://example.com/xinglan",
          "source_platform": "小红书",
          "source_type": "官网",
          "evidence_text": "刷小红书看到「星澜科技」在招 AI Agent 方向…",
          "evidence_url": null,
          "note": "AI Agent 方向",
          "collected_at": "2026-09-19T10:47:00+00:00",
          "reachability": {
            "state": "checked_error",
            "label": "HTTP 404",
            "http_status": 404,
            "checked_at": "2026-09-19T10:47:00+00:00",
            "reason": "检查过：HTTP 404（入口可能已失效）"
          },
          "application": {
            "application_id": 1,
            "job_id": 3,
            "job_label": "甲公司 · Python 后端开发实习生（深圳）",
            "channel": "官网",
            "status": "applied",
            "applied_at": "2026-09-20T01:00:00+00:00"
          }
        }
      ]
    },
    { "source_type": null, "label": "未分类", "count": 0, "leads": [] }
  ]
}
```

### 1.1 可达性是**三态**（页面必须区分，不许合并成"失效"）

| `state` | `label` | 触发条件 | 页面颜色约定 |
|---|---|---|---|
| `checked_ok` | `可达` | `200 ≤ http_status < 400` | 绿 |
| `checked_error` | `HTTP 404`（或实际状态码） | `http_status ≥ 400` | 琥珀/红（**只有这一态算"可能失效"**） |
| `unchecked` | `未检查` | `http_status is null` | **灰（中性）——不能画成红/失效** |

`unchecked` 的 `reason` 会说明**为什么没检查**，两种原因要分得清（规格里第 2/3 条 vs 第 4 条线索）：

- 没有 `official_url` → 「没有官网网址，无法检查」
- 有 `official_url` 但没检查过 → 「有网址，但当时没检查（网络不通或没跑检查）」

> 采集端的口径：`http_status` 为空 = **未检查**，不是"失效"。导入时**不做本地化转换**，
> 库里一律 UTC；页面按用户本地时区显示。

### 1.2 `application` 字段

这条线索**已经产生的投递记录**（没有则 `null`）。因为 `applications.job_id` 是 NOT NULL，
一条线索的"标记投递"必须落在**某个岗位**上（与采集端 `lh leads apply --job <id>` 同一语义）。
页面的标记动作因此要选一个岗位，见 §3。

字段与语义（★ 这几条是前端实测后补明确的，别让页面去猜）：

| 字段 | 说明 |
|---|---|
| `application` | **最近一次**投递记录（按 `applications.id` 倒序取第一条），没有则 `null` |
| `applications_count` | 这条线索一共产生过几条投递记录。★ 一条线索**可以**投多个岗位，所以它可能 > 1；此时 `application` 给的是最新那条 —— 页面若想强调"投过 N 次"，用它 |
| `application.application_id` / `job_id` / `job_label` | 记录 id、岗位 id、岗位的可读标签（`公司 · 职位 · 城市`） |
| `application.channel` | 渠道；**可能为 `null`**（线索没写投递方式、也没显式指定时） |
| `application.status` | `pending` / `applied` / `viewed` / `replied` / … （与 `jh apply` 同一套状态） |
| `application.applied_at` | 标成 `applied` 的时间（UTC ISO），没到那一步就是 `null` |
| `created_at` | **导入进本库**的时间（与 `collected_at` 不同：后者是采集端采到它的时间） |

**关联口径**：靠 `applications.company_id`（迁移 0005）**显式记录**"这条投递来自哪条线索"，
**不是**按渠道反推 —— 渠道只有四五个取值，反推会把同渠道的其它线索也点亮成"已投"。

---

## 2. `GET /api/jobs`

标记投递时要选的岗位（只读，只给 `id / company / title / city / status / label`）。
**这是只读列表，不是"看板页面"** —— P8 才做看板。

```json
{
  "jobs": [
    { "id": 3, "company": "甲公司", "title": "Python 后端开发实习生",
      "city": "深圳", "status": "pending",
      "label": "甲公司 · Python 后端开发实习生 · 深圳" }
  ]
}
```

`label` 的拼法是 `公司 · 职位 · 城市`（有哪个拼哪个，全空时退化成 `岗位 #<id>`）——
**页面只渲染 `label`**，不要自己去拼（省得两处口径不一致）。

空数组是合法状态：页面要提示「投递记录必须挂在岗位上 → 先用 `jh job add <链接>` 录岗位」，
并把标记按钮置灰（**不要**因此去造一个岗位）。

---

## 3. `POST /api/leads/{lead_id}/apply`

把一条线索记成**某个岗位的一次投递**。**这一个接口是页面唯一的写动作。**

请求体：

```json
{ "job_id": 3, "status": "applied", "channel": null }
```

- `status` 可省，默认 `applied`（取值同 `jh apply`：`pending/viewed/applied/replied/...`）。
- `channel` 可省：省略时**由线索的 `source_type` 推导**（官网 / 内推 / HR直招）。

响应（200）——**新建**：

```json
{
  "lead_id": 1,
  "lead_name": "星澜科技",
  "application_id": 1,
  "job_id": 3,
  "created": true,
  "channel": "官网",
  "channel_before": null,
  "channel_changed": false,
  "status": "applied",
  "status_before": "pending",
  "status_changed": true,
  "message": "已记入投递看板：新建记录 #1（线索「星澜科技」，渠道 官网，状态 applied）"
}
```

响应（200）——**第二次点同一条（更新了东西）**：

```json
{
  "created": false,
  "channel": "内推",
  "channel_before": "官网",
  "channel_changed": true,
  "status": "applied",
  "status_before": "applied",
  "status_changed": false,
  "message": "更新了已有记录 #1：渠道 官网 → 内推"
}
```

响应（200）——**第二次点同一条（什么都没变）**：

```json
{
  "created": false,
  "channel_changed": false,
  "status_changed": false,
  "message": "没有变化：记录 #1 已经是（渠道 官网，状态 applied）—— 重复点不会多记一次"
}
```

★ **`message` 是给人看的，不要 parse 它。** 上面三段是**真实实现**的文案（下方"幂等"一节
描述的规则才是契约）；页面要分类就按 `created` / `channel_changed` / `status_changed` 判断，
`message` 原样显示即可。

**幂等 + 如实反馈**（规格里最容易做错的一条，也是 ISSUE #57 的原症状）：

- 同一条线索 + 同一个岗位点两次 → **只有一行** `applications`（`created=false`）；
- 第二次必须**如实说明是更新**什么（渠道 / 状态），什么都没变时也要说出来 ——
  三种情况的文案两两不同，页面据此显示不同的标签；
- **绝不静默改写** `channel`：`channel_before` / `channel_changed` 就是为了让页面说出来；
- ★ **新建不算"改写"**：`ensure_pending_application()` 会先给新行播一个默认渠道
  （`招聘网站`，那是给"从 BOSS 这类平台投的"准备的）。这个内部默认值**不会**被当成
  "改之前的渠道" —— 否则用户第一次标记就会看到「渠道 招聘网站 → 官网」，
  一次新建被描述成一次改写（前端实测发现过，已修）。所以新建时恒为
  `channel_before = null`、`channel_changed = false`。

错误（都带中文可操作提示，`{"detail": "..."}`）：

| 情况 | 码 |
|---|---|
| `lead_id` 不存在 | 404 |
| `job_id` 不存在 | 404 |
| `status` 不认识 | 400（提示可用取值） |
| 请求体字段类型不对 | 422（FastAPI 默认；页面照原样显示即可） |

---

## 4. `GET /api/health`

```json
{ "ok": true, "version": "0.1.0", "leads": 4, "jobs": 1, "frontend": true }
```

`frontend=false` 表示 `web/dist` 不存在（页面还没构建）。

---

## 5. 前端构建产物缺失时的行为

- `GET /` 时若 `web/dist/index.html` 不存在 → 返回 **503 + 一段可读的 HTML**（不是白屏、也不是裸 500）：
  「前端还没构建：先跑 `npm ci && npm run build`（在 `web/` 目录）」。
- `jh leads serve` 启动时也会在终端打同一句提示。
- **`web/dist/` 不进仓库**（`.gitignore`）；CI 里有 Node 步骤真的跑一遍构建。

---

## 6. 为什么不做的事（写下来免得被当成遗漏）

- ❌ 不做鉴权 / 用户系统（本地工具，只监听回环）。
- ❌ 不做增删改线索（线索的唯一来源是 `jh leads import`），页面只读 + 标记投递。
- ❌ 不做批量标记 / 自动投递（红线）。
- ❌ 不做 P8 的另外三个页面（今日清单 / 看板 / 面试问题 / 统计）。
- ❌ 不把采集端命令（`add` / `probe` / `capture` / `export`）搬回本仓库 —— 它们在独立部署
  `projects/leadhunter`（命令 `lh`）。
