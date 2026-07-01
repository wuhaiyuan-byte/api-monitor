# OSS 文件监控 — 接入与数据格式文档

> 给**另一个应用**直接消费用的完整参考。所有数据格式都来自线上跑的真实例子（2026-06-11 抓的）。

---

## 0. 服务信息

| 项 | 值 |
|---|---|
| 服务 | API Monitor (本仓库) |
| 部署 | Docker Compose，219 上 |
| API 基址 | `http://192.168.3.219:9876`（**注意：原 8000 被网络过滤了，改成 9876**） |
| API 根 | `http://192.168.3.219:9876/api` |
| WebSocket | `ws://192.168.3.219:9876/ws/status` |
| 后端容器内端口 | 8000（host→9876→container→8000） |
| 数据库 | PostgreSQL 15（容器内 `db:5432`，外部不暴露） |

---

## 1. 监控项模型 (OssMonitor)

### 1.1 字段完整定义（创建/更新请求体）

```json
{
  "name": "每日人群检查",                    // 必填，监控项名称
  "provider": "aliyun",                       // 必填，"aliyun" 或 "s3"（S3 兼容）
  "endpoint": "https://oss-cn-shanghai.aliyuncs.com",  // 必填，OSS endpoint
  "bucket": "eln-wecom-prod",                  // 必填，bucket 名
  "region": "cn-shanghai",                     // 可空，仅用于前端展示
  "prefix": "CDP/",                             // 必填且非空：你有权限访问的具体路径
  "keyword": "cdp_tag",                         // 必填，要匹配的关键字
  "match_mode": "contains",                     // "contains" | "regex"，默认 contains
  "expected_present": true,                     // true=应存在 | false=不应存在
  "recursive": false,                           // true=递归子目录 | false=仅当前目录
  "max_age_hours": 25,                          // 可空(不检查时效) | 1-8760
  "failure_threshold": 2,                       // 连续失败几次才真正告警
  "interval_seconds": 3600,                     // 检查间隔（秒），最小 10
  "is_active": true,                            // 是否启用
  "access_key_id": "LTAI5t7h9rZz4LFkk6zocKkk",// 必填
  "access_key_secret": "sk-..."                 // 必填（仅创建时；更新可不填表示不改）
}
```

### 1.2 字段含义详解

| 字段 | 说明 |
|---|---|
| `prefix` | OSS 前缀路径。**没有 bucket 全权限时必须填具体路径**（你拥有 List 权限的子目录）。会自动 normalize（去头尾 `/`，保留尾 `/`） |
| `keyword` | 文件 key（完整路径）中要包含的子串 |
| `match_mode=contains` | `keyword in key` 子串匹配 |
| `match_mode=regex` | `re.search(keyword, key)` 正则匹配（用 Python re 模块） |
| `expected_present=true` | 应该匹配到文件 → 没匹配上触发 `oss_missing` 告警 |
| `expected_present=false` | 不应该有文件 → 匹配上了触发 `oss_unexpected` 告警 |
| `recursive=true` | 递归扫描 prefix 下所有子目录 |
| `recursive=false` | 只扫 prefix 直接子文件（用 OSS `delimiter='/'`） |
| `max_age_hours` | 新鲜度窗口：匹配项的 `last_modified` 必须 ≥ `now - max_age_hours` 小时。NULL = 不检查时效。**陈旧匹配按"不存在"处理** |
| `failure_threshold` | 连续失败多少次才触发邮件/飞书告警（避免抖动误报）。默认 2 |
| `access_key_secret` | **只在创建和需要更换时传入**。后端用 Fernet 对称加密存到 DB（key 在 `/app/data/oss_fernet.key`，从 `OSS_ENC_KEY` env 派生） |

### 1.3 响应 (OssMonitorResponse)

响应比请求多这些字段：

```json
{
  "id": 13,
  "...": "上面所有字段",
  "access_key_secret_masked": "***li3J",  // 永不返回明文，尾 4 位
  "last_status": "matched",                  // matched | not_matched | error
  "last_checked_at": "2026-06-10T07:33:19.527497",  // UTC ISO，前端 +8 显示
  "last_matched_key": "CDP/cdp_tag_20260607_xxx.csv",  // 最新一次匹配的文件
  "last_matched_size": 12345,                 // 字节
  "last_matched_modified": "2026-06-07T07:12:43",  // 文件 OSS 上的 last_modified (UTC)
  "last_error": null,                          // 见下
  "consecutive_failures": 0,                   // 连续失败次数（达到 threshold 才告警）
  "created_at": "2026-06-06T15:52:34.431375"   // 创建时间 (UTC)
}
```

**`last_status` 含义**：
- `matched` — 找到了匹配文件且 freshness 通过
- `not_matched` — 没匹配到 OR 所有匹配都陈旧 OR `expected_present=false` 但匹配到了
- `error` — OSS API 调用失败（认证/网络/权限）

---

## 2. API 完整清单

### 2.1 列出所有 OSS 监控项

```http
GET /api/oss-monitors
```

**响应**：`OssMonitorResponse[]`

```bash
curl -s http://192.168.3.219:9876/api/oss-monitors | python3 -m json.tool
```

### 2.2 创建监控项

```http
POST /api/oss-monitors
Content-Type: application/json
Body: OssMonitorCreate (见 1.1)
```

```bash
curl -s -X POST http://192.168.3.219:9876/api/oss-monitors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "每日人群检查",
    "endpoint": "https://oss-cn-shanghai.aliyuncs.com",
    "region": "cn-shanghai",
    "bucket": "eln-wecom-prod",
    "prefix": "CDP/",
    "keyword": "cdp_tag",
    "match_mode": "contains",
    "expected_present": true,
    "recursive": false,
    "max_age_hours": 25,
    "interval_seconds": 3600,
    "is_active": true,
    "access_key_id": "LTAI5t...",
    "access_key_secret": "sk-..."
  }'
```

### 2.3 获取单个监控项

```http
GET /api/oss-monitors/{id}
```

### 2.4 更新监控项

```http
PUT /api/oss-monitors/{id}
Content-Type: application/json
Body: OssMonitorUpdate (所有字段可选，不传 = 不变)
```

```bash
# 改 max_age_hours 为 48（适合每日文件）
curl -s -X PUT http://192.168.3.219:9876/api/oss-monitors/13 \
  -H "Content-Type: application/json" \
  -d '{"max_age_hours": 48}'

# 暂停
curl -s -X PUT http://192.168.3.219:9876/api/oss-monitors/13 \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

### 2.5 删除监控项

```http
DELETE /api/oss-monitors/{id}
```

级联删除该监控项的所有 `oss_check_results`。

### 2.6 获取检查历史

```http
GET /api/oss-monitors/{id}/checks?minutes=60
```

**参数**：`minutes` 默认 60，最大 43200（30 天）。

**响应**：`OssCheckResultResponse[]`（按 `checked_at` 倒序）

### 2.7 手动触发一次检查

```http
POST /api/oss-monitors/{id}/check-now
```

立即调用 `check_oss_monitor()`。返回 `{"message": "check triggered"}`。

### 2.8 测试 OSS 连接（不入库）

```http
POST /api/oss-monitors/test-connection
Content-Type: application/json
Body:
{
  "provider": "aliyun",
  "endpoint": "https://oss-cn-shanghai.aliyuncs.com",
  "bucket": "test-bucket",
  "region": "cn-shanghai",         // 可选
  "access_key_id": "LTAI...",
  "access_key_secret": "sk-...",
  "prefix": "test/"                  // 可选，默认 ""
}
```

**成功**：`200 {"ok": true, "message": "...", "sample_key": "test/file1.csv", "sample_size": 123}`
**失败**：`400 {"detail": "OSS API error: NoSuchBucket ..."}`

---

## 3. 检查结果模型 (OssCheckResult)

### 3.1 字段定义

```json
{
  "id": 280,
  "oss_monitor_id": 13,
  "status": "matched",                  // matched | not_matched | error
  "matched_key": "CDP/cdp_tag_20260607_1001028_870.csv",  // 命中文件完整 key（陈旧时为 null）
  "file_size": 12345,                  // 命中文件字节数
  "file_last_modified": "2026-06-06T23:12:43",  // 命中文件 OSS 上的 last_modified (UTC)
  "scanned_count": 39,                  // 本次扫描总文件数
  "scan_truncated": false,             // 是否超过 200 条 SCAN_LIMIT
  "error_message": null,                // OSS API 错误信息（status=error 时有内容）
  "debug_info": "{...}",                // 见 3.2，JSON 字符串
  "checked_at": "2026-06-11T08:02:37.509377"  // 检查时间 (UTC)
}
```

### 3.2 debug_info 结构（关键！包含每个扫描文件的详情）

`debug_info` 是 JSON **字符串**字段，要 `JSON.parse()` 一下。完整结构：

```json
{
  "now_utc": "2026-06-11T08:02:37.509105",      // 本次 check 开始时间（UTC）
  "cutoff_utc": "2026-06-10T07:02:37.508774",  // 新鲜度截止时间（UTC，now - max_age_hours）
  "recursive": false,                          // 扫描模式
  "scanned": 39,                               // 本次扫描的总文件数
  "truncated": false,                          // 是否超过 200 cap
  "max_age_hours": 25,                         // 新鲜度窗口
  "keyword": "cdp_tag",                        // 关键字
  "match_mode": "contains",                    // 匹配模式
  "files": [                                   // 每个扫描到的文件
    {
      "key": "CDP/",                            // 文件完整 key
      "fm": "2025-12-19T02:48:50",              // 文件 last_modified (UTC)
      "matched": false,                         // 是否匹配 keyword
      "age_h": 4181.23,                         // 距今小时数（仅当 fm 不为空时）
      "cutoff": "2026-06-10T07:02:37.508774",   // 截止时间（冗余，方便前端直接显示）
      "decision": "stale"                       // stale | fresh | "n/a"（未匹配无 fm）
    },
    {
      "key": "CDP/cdp_tag_20260606_1001028_866.csv",
      "fm": "2026-06-05T16:39:27",
      "matched": true,
      "age_h": 36.85,
      "cutoff": "2026-06-10T07:02:37.508774",
      "decision": "stale"                        // 匹配上但超过 25h 窗口
    },
    {
      "key": "CDP/cdp_tag_20260607_1001028_870.csv",
      "fm": "2026-06-06T23:12:43",
      "matched": true,
      "age_h": 6.3,
      "cutoff": "2026-06-10T07:02:37.508774",
      "decision": "fresh"                        // 匹配上且新鲜 → status=matched
    }
  ]
}
```

**`decision` 规则**：
- `fresh` — `matched=true` 且 `last_modified >= cutoff`
- `stale` — `matched=true` 且 `last_modified < cutoff`（陈旧按不存在处理）
- `stale(unparseable)` — `matched=true` 但 `last_modified` 解析失败（视为陈旧）
- `n/a` — `matched=false`（没匹配上，无须判断新鲜度）
- `fresh`（无 max_age_hours 时）— 匹配上但没配新鲜度窗口

**`status` 怎么由 `files` 推导**：
| `expected_present` | 任意 file `decision` | 最终 status |
|---|---|---|
| true | 至少一个 `fresh` | `matched` |
| true | 只有 `stale` 或 `n/a` | `not_matched` |
| false | 至少一个 `fresh` | `not_matched`（异常出现） |
| false | 只有 `stale` 或 `n/a` | `matched`（干净） |
| — | OSS API 出错 | `error` |

---

## 4. 完整使用示例

### 4.1 用 Python 列出所有 OSS 监控项和最新状态

```python
import requests

BASE = "http://192.168.3.219:9876/api"

# 列出所有监控项
monitors = requests.get(f"{BASE}/oss-monitors").json()
for m in monitors:
    print(f"[{m['id']}] {m['name']}: last_status={m['last_status']} "
          f"consecutive_failures={m['consecutive_failures']} "
          f"last_error={m['last_error'][:50] if m['last_error'] else 'none'}...")

# 取单个监控项最近 24h 检查历史
checks = requests.get(f"{BASE}/oss-monitors/13/checks", params={"minutes": 1440}).json()
for c in checks[:5]:
    print(f"  [{c['id']}] {c['checked_at']} status={c['status']} "
          f"matched={c['matched_key']} scanned={c['scanned_count']}")
```

### 4.2 解析 debug_info 找到今天的 cdp_tag 文件

```python
import requests, json
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))

checks = requests.get(f"{BASE}/oss-monitors/13/checks", params={"minutes": 1440}).json()

# 取最新一次 check 的 debug_info
debug = json.loads(checks[0]["debug_info"])
print(f"扫描了 {debug['scanned']} 个对象")
print(f"扫描时间: {datetime.fromisoformat(debug['now_utc']).astimezone(BJ)}")

# 找今天（北京时间）上传的 cdp_tag 文件
today_bj = datetime.now(BJ).date()
for f in debug["files"]:
    if not f["matched"] or not f.get("fm"):
        continue
    fm_bj = datetime.fromisoformat(f["fm"]).astimezone(BJ)
    if fm_bj.date() == today_bj:
        print(f"  ✓ 今天上传: {f['key']} ({fm_bj}, age={f['age_h']:.1f}h, {f['decision']})")
```

### 4.3 用 curl + jq 找今天没匹配上的监控项

```bash
# 找当前状态是 not_matched 且有陈旧匹配的监控项
curl -s http://192.168.3.219:9876/api/oss-monitors | \
  jq '.[] | select(.last_status == "not_matched") | {id, name, last_error, consecutive_failures}'
```

### 4.4 触发一次手动 check 然后轮询

```bash
# 触发
curl -s -X POST http://192.168.3.219:9876/api/oss-monitors/13/check-now

# 等 5 秒，查最新结果
sleep 5
curl -s "http://192.168.3.219:9876/api/oss-monitors/13/checks?minutes=5" | \
  jq '.[] | {status, matched_key, error_message}'
```

---

## 5. 状态机与告警触发逻辑

```
每次 check_oss_monitor(id) 执行：

  ┌─ scan(prefix, keyword) ─┐
  │  oss2.Bucket.list_objects  │ → max 200 个对象 (SCAN_LIMIT)
  │  按 keyword in key 过滤    │
  └──────────────┬─────────────┘
                 ↓ all_scanned[(key, fm, matched), ...]
                 ↓
  ┌─ 对每个匹配 file 计算 freshness ─┐
  │  fm >= cutoff (now - max_age_hours) → fresh  │
  │  fm <  cutoff                       → stale  │
  │  fm 不可解析                        → stale  │
  └──────────────┬─────────────────────────────┘
                 ↓
  ┌─ 推导 status ─┐
  │ expected_present=true:             │
  │   有 fresh  → status=matched      │
  │   无 fresh  → status=not_matched   │
  │ expected_present=false:            │
  │   有 fresh  → status=not_matched   │
  │   无 fresh  → status=matched       │
  └──────────────┬─────────────────────┘
                 ↓
  ┌─ 累计 + 告警 ─┐
  │ consecutive_failures += 1 (非 good) │
  │ consecutive_failures = 0 (good)   │
  │ 当 = failure_threshold (默认 2)    │
  │   → 写 Alert 记录                │
  │   → 邮件 + 飞书并行发           │
  │   → WebSocket broadcast           │
  │ 上次 bad 本次 good → 恢复通知      │
  └──────────────────────────────────┘
```

---

## 6. 时间字段约定

| 字段 | 时区 | 来源 |
|---|---|---|
| `created_at`, `updated_at`, `checked_at`, `last_modified` 等 DB 时间戳 | **naive UTC**（不带 tzinfo，**实际是 UTC**） | DB 里就是 UTC |
| ISO 字符串（如 `"2026-06-10T07:33:19.527497"`） | UTC | API 返回 |
| 前端显示 | **Asia/Shanghai (UTC+8)** | `formatDate()` 自动 +8 |
| 邮件/飞书告警文案里的"时间" | **Asia/Shanghai** + `(北京时间)` 后缀 | 后端 `now_beijing_str()` 生成 |
| `[OSS-DEBUG]` 日志 | 同时打印 `now_utc=...ISO` 和 `now_bj=...human` | 后端 |

**给另一个应用消费建议**：
- 拿到的所有 ISO 时间都是 UTC，自己 +8 转北京时
- 或者用 JavaScript `new Date(iso).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})`

---

## 7. 已知约束与边界

| 项 | 值 | 影响 |
|---|---|---|
| `SCAN_LIMIT` | 200 | 单次 check 最多扫 200 个对象，超出会设 `truncated=true` 并停止。**实际场景 OSS prefix 下不要超过 200 个 cdp_tag 文件** |
| `all_matches` 错误信息上限 | 20 | `last_error` / 卡片最多列 20 个匹配项（debug_info 不限） |
| `debug_info` files 上限 | 200 | 每个 check 的 debug_info 包含所有扫描文件，最多 200 |
| `max_age_hours` 范围 | 1-8760 | 1 小时 ≤ 1 年 |
| `interval_seconds` 最小 | 10 | 太小会刷 OSS API |
| `failure_threshold` 默认 | 2 | 连续 2 次失败才触发告警 |
| Cooldown（重复抑制） | 默认 10 分钟（可改） | 同一 monitor+同一 alert_type 在窗口内只发 1 次 |
| Access Key Secret 存储 | Fernet 加密（`/app/data/oss_fernet.key`，从 env `OSS_ENC_KEY` 派生） | DB 泄露也不暴露明文 |
| 前端轮询 | WebSocket `/ws/status` 收 `oss_status_update` / `oss_new_alert` 事件 | 实时更新 dashboard |

---

## 8. 完整 cURL 速查

```bash
# 基础
BASE=http://192.168.3.219:9876

# 列出
curl -s $BASE/api/oss-monitors | jq .

# 单个
curl -s $BASE/api/oss-monitors/13 | jq .

# 创建
curl -s -X POST $BASE/api/oss-monitors -H 'Content-Type: application/json' -d '{
  "name":"X", "endpoint":"https://oss-cn-shanghai.aliyuncs.com",
  "bucket":"b", "prefix":"p/", "keyword":"k",
  "access_key_id":"AK", "access_key_secret":"SK"
}'

# 更新（任意字段）
curl -s -X PUT $BASE/api/oss-monitors/13 -H 'Content-Type: application/json' \
  -d '{"max_age_hours":48,"is_active":false}'

# 删除
curl -s -X DELETE $BASE/api/oss-monitors/13

# 历史
curl -s "$BASE/api/oss-monitors/13/checks?minutes=1440" | jq '.[0]'

# 立即检查
curl -s -X POST $BASE/api/oss-monitors/13/check-now

# 测试连接
curl -s -X POST $BASE/api/oss-monitors/test-connection -H 'Content-Type: application/json' -d '{
  "endpoint":"https://oss-cn-shanghai.aliyuncs.com",
  "bucket":"b", "access_key_id":"AK", "access_key_secret":"SK"
}'
```

---

## 9. 常见使用场景示例

### 9.1 "今天有没有今天的文件"

```python
import requests, json
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
TODAY_BJ = datetime.now(BJ).date()

monitors = requests.get("http://192.168.3.219:9876/api/oss-monitors").json()
for m in monitors:
    if m["last_status"] != "matched":
        print(f"⚠️  {m['name']}: {m['last_status']} (连续失败 {m['consecutive_failures']})")
        continue
    # 取最新 check 的 debug_info
    checks = requests.get(f"http://192.168.3.219:9876/api/oss-monitors/{m['id']}/checks?minutes=10").json()
    if not checks:
        continue
    debug = json.loads(checks[0]["debug_info"])
    today_file = next(
        (f for f in debug["files"]
         if f.get("matched") and f.get("fm")
         and datetime.fromisoformat(f["fm"]).astimezone(BJ).date() == TODAY_BJ
         and f["decision"] == "fresh"),
        None
    )
    if today_file:
        print(f"✓ {m['name']}: 今天有文件 {today_file['key']} ({today_file['age_h']:.1f}h 前)")
    else:
        print(f"❌ {m['name']}: 今天还没文件！")
```

### 9.2 "凌晨 0 点到现在的告警数"

```python
import requests
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
today_0am_bj = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0)
today_0am_utc = today_0am_bj.astimezone(timezone.utc)

checks = requests.get(f"http://192.168.3.219:9876/api/oss-monitors/13/checks", params={"minutes": 1440}).json()
today_errors = [c for c in checks if datetime.fromisoformat(c["checked_at"] + "+00:00") >= today_0am_utc and c["status"] == "error"]
print(f"今天 OSS 监控项 13 错误次数: {len(today_errors)}")
```

### 9.3 "哪些文件比 cutoff 还旧"

```python
import requests, json
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
checks = requests.get("http://192.168.3.219:9876/api/oss-monitors/13/checks?minutes=10080").json()
debug = json.loads(checks[0]["debug_info"])

print(f"匹配但陈旧的文件（> cutoff {debug['cutoff_utc']}）:")
for f in debug["files"]:
    if f.get("decision") == "stale":
        fm_bj = datetime.fromisoformat(f["fm"]).astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {f['key']}  上传: {fm_bj}  age={f['age_h']:.1f}h")
```

---

## 10. 错误码速查

| HTTP | 含义 | 怎么处理 |
|---|---|---|
| 200 | 正常 | — |
| 400 | 请求体缺/字段错（如 prefix 空、access_key_secret 空） | 检查字段 |
| 404 | 监控项 ID 不存在 | 查 ID |
| 422 | Pydantic 校验失败（如 max_age_hours 超过 8760） | 看 `detail[].msg` |
| 500 | 后端异常 | 看 `docker compose logs backend` |

OSS API 自身的错误会出现在 `last_error` 字段（如 `OSS API error: NoSuchBucket`、`AccessDenied`、`timeout` 等）。

---

## 11. 故障排查清单

1. **`last_status=error`** → 看 `last_error`，常见原因：
   - `AccessDenied` → AK/SK 没该 prefix 的 ListObject 权限
   - `NoSuchBucket` → bucket 名错了
   - `timeout` → 网络问题
   - `InvalidAccessKeyId` → AK 失效
2. **`last_status=not_matched` 但文件确实在** → 看 debug_info：
   - 文件在 `files` 里且 `matched=true` → 看 `decision`：`stale` 是 max_age_hours 太紧，`n/a` 是没匹配上
   - 文件不在 `files` 里 → prefix 路径错了
3. **`consecutive_failures` 一直涨** → 真正的问题没修，每次 check 都失败
4. **没收到告警** → 看 Settings → 飞书/邮件开关 + `repeat_suppress_minutes` 冷却
5. **想立即触发** → `POST /api/oss-monitors/{id}/check-now`

---

## 12. 复制的字段速查（给后端 schema 同步用）

```typescript
// OSS Monitor
type OssMonitor = {
  id: number;
  name: string;
  provider: 'aliyun' | 's3';
  endpoint: string;
  bucket: string;
  region: string | null;
  prefix: string;                // 已 normalize：去头 /，保留尾 /
  keyword: string;
  match_mode: 'contains' | 'regex';
  expected_present: boolean;
  recursive: boolean;             // true=递归, false=仅当前目录
  max_age_hours: number | null;   // null=不检查时效
  failure_threshold: number;
  interval_seconds: number;
  is_active: boolean;
  access_key_id: string;
  access_key_secret_masked: string;  // "***last4"，从不返回明文
  last_status: 'matched' | 'not_matched' | 'error' | null;
  last_checked_at: string | null;     // UTC ISO
  last_matched_key: string | null;
  last_matched_size: number | null;
  last_matched_modified: string | null; // UTC ISO
  last_error: string | null;
  consecutive_failures: number;
  created_at: string;                  // UTC ISO
}

// OSS Check Result
type OssCheckResult = {
  id: number;
  oss_monitor_id: number;
  status: 'matched' | 'not_matched' | 'error';
  matched_key: string | null;
  file_size: number | null;
  file_last_modified: string | null;   // UTC ISO
  scanned_count: number;
  scan_truncated: boolean;
  error_message: string | null;
  debug_info: string | null;           // JSON 字符串，要 parse
  checked_at: string;                  // UTC ISO
}

type DebugInfo = {
  now_utc: string;
  cutoff_utc: string | null;
  recursive: boolean;
  scanned: number;
  truncated: boolean;
  max_age_hours: number | null;
  keyword: string;
  match_mode: 'contains' | 'regex';
  files: Array<{
    key: string;
    fm: string | null;
    matched: boolean;
    age_h?: number;
    cutoff?: string;
    decision: 'fresh' | 'stale' | 'n/a' | 'stale(unparseable)';
  }>;
}
```

---

**版本**：对应 API Monitor commit `0bb3a52` (或更新)，2026-06-11 北京时间。
**作者备注**：直接复制进你们项目 README / SDK docs 都行。如果字段变了告诉我，我重抓数据更新这份。