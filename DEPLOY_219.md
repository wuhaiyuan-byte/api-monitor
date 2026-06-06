# 部署到 192.168.3.219 操作手册

本文档记录把本地 `apimonitor` 项目部署到生产服务器 `192.168.3.219` 的完整流程和常用命令。

---

## 1. 初次部署

### 1.1 服务器信息
- **IP**: `192.168.3.219`
- **用户**: `blueleaf`
- **部署路径**: `~/apimonitor`
- **架构**: Docker Compose（backend + PostgreSQL）

### 1.2 SSH 连接（密钥认证）

```bash
# 本地已经有 SSH 公钥已配置到服务器，可直接无密码登录
ssh blueleaf@192.168.3.219
```

> **说明**：服务器未安装 docker.io，所以 docker 命令在 192.168.3.220 那台 opencode 客户端机器上找不到。但 219 上有 docker 并使用密钥登录。

### 1.3 一键部署脚本

```bash
# 1. 同步代码（rsync，自动删除目标多余文件）
rsync -av --delete \
  /home/opencode/opencode/apimonitor/backend/ \
  blueleaf@192.168.3.219:~/apimonitor/backend/ \
  --exclude=__pycache__

rsync -av /home/opencode/opencode/apimonitor/frontend/index.html \
  blueleaf@192.168.3.219:~/apimonitor/frontend/

# 2. 重新构建并启动
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose down && docker compose build --no-cache && docker compose up -d"

# 3. 验证服务
ssh blueleaf@192.168.3.219 "curl -s http://localhost:8000/api/monitors"
```

### 1.4 访问地址

- 前端 + API: `http://192.168.3.219:8000`
- API 根: `http://192.168.3.219:8000/api/`
- WebSocket: `ws://192.168.3.219:8000/ws/status`

---

## 2. 日常运维

### 2.1 查看容器状态

```bash
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose ps"
```

### 2.2 查看后端日志

```bash
# 最近 100 行
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs backend --tail=100"

# 实时跟踪
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs -f backend"

# 只看邮件相关（排查邮件发送问题）
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs backend 2>&1 | grep -iE 'email|cooldown|skipped|sent for|Failed'"
```

### 2.3 重启服务

```bash
# 只重启 backend
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose restart backend"

# 完整重启
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose restart"
```

### 2.4 快速热更新（不重建镜像）

```bash
# 适用场景：只改了 Python 文件，pip 依赖没变
# backend 通过 volumes 挂载了 ./backend:/app/backend，文件改动会即时生效
# 但 Python 进程需要重启加载新代码
rsync -av /home/opencode/opencode/apimonitor/backend/ \
  blueleaf@192.168.3.219:~/apimonitor/backend/ --exclude=__pycache__
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose restart backend"
```

### 2.5 完整重建（改依赖或 Dockerfile 时用）

```bash
# 同步全部
rsync -av --delete /home/opencode/opencode/apimonitor/ \
  blueleaf@192.168.3.219:~/apimonitor/ --exclude=__pycache__ --exclude=.git

# 重建
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose down && docker compose build --no-cache && docker compose up -d"
```

---

## 3. 数据库操作

### 3.1 进入 PostgreSQL 容器

```bash
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose exec db psql -U apimon -d apimon"
```

常用 SQL：

```sql
-- 查看所有监控项
SELECT id, name, url, is_active, failure_threshold_status, failure_threshold_latency FROM monitors;

-- 查看最近告警
SELECT id, monitor_id, alert_type, is_fired, consecutive_failures, threshold, is_resolved, created_at
FROM alerts ORDER BY created_at DESC LIMIT 20;

-- 查看邮件设置
SELECT key, value FROM settings WHERE key LIKE 'email%' OR key LIKE 'smtp%';

-- 手动标记告警为已解决
UPDATE alerts SET is_resolved = true WHERE id = <id>;

-- 删除测试监控
DELETE FROM monitors WHERE id = <id>;
```

### 3.2 重置数据库（危险操作）

```bash
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose down -v && docker compose up -d"
# -v 会删除 volumes，数据库会清空
```

---

## 4. 故障排查

### 4.1 前端空白 / 无法访问

```bash
# 检查容器是否运行
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose ps"

# 检查端口监听
ssh blueleaf@192.168.3.219 "netstat -tlnp 2>/dev/null | grep 8000 || ss -tlnp | grep 8000"

# 测试 API
ssh blueleaf@192.168.3.219 "curl -v http://localhost:8000/api/monitors"

# 看后端是否正常启动
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs backend | head -30"
```

### 4.2 邮件发送失败

```bash
# 看后端邮件相关日志
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs backend 2>&1 | grep -iE 'email|cooldown|skipped|sent for|Failed' | tail -30"

# 看当前邮件设置（注意 use_tls 和 smtp_port 匹配）
ssh blueleaf@192.168.3.219 "curl -s http://localhost:8000/api/settings | python3 -m json.tool"
```

**常见错误**：
- `Connection unexpectedly closed: timed out` → 端口 465 用了普通 SMTP（应该是 SMTP_SSL）
- `Alert email skipped: ... in cooldown` → 15 分钟内（默认）已经发过同类型告警

### 4.3 数据库迁移失败

```bash
# 看启动日志
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs backend 2>&1 | grep -iE 'migrat|error|column' | head -20"

# 查看列是否存在
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose exec db psql -U apimon -d apimon -c \"SELECT column_name FROM information_schema.columns WHERE table_name = 'monitors';\""
```

### 4.4 Python 依赖安装慢 / 失败

Dockerfile 已配置阿里云 pip 镜像（`mirrors.aliyun.com`）。如果还是超时：

```bash
# 临时改用清华源
ssh blueleaf@192.168.3.219 "cd ~/apimonitor/backend && sed -i 's|mirrors.aliyun.com|pypi.tuna.tsinghua.edu.cn|g' Dockerfile"

# 重建
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose build --no-cache"
```

---

## 5. 关键文件位置

```
/home/opencode/opencode/apimonitor/    # 本地源代码
├── backend/
│   ├── main.py          # FastAPI 入口
│   ├── models.py        # SQLAlchemy 数据模型
│   ├── tasks.py         # 调度任务 + 异常检测 + 邮件发送
│   ├── database.py      # DB 连接 + 自动迁移
│   ├── api/
│   │   ├── monitors.py  # 监控项 CRUD
│   │   ├── alerts.py    # 告警查询
│   │   └── settings.py  # 邮件设置 + 测试发送
│   ├── tests/           # pytest 测试
│   ├── requirements.txt
│   └── Dockerfile       # 含阿里云 pip 镜像
├── frontend/
│   └── index.html       # 单文件 Vue 3 应用
├── docker-compose.yml
└── .env.example
```

---

## 6. 常用调试命令速查

```bash
# 查看内存中的 cooldown 缓存（重启清空）
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose exec -T -w /app/backend backend python3 -c 'import tasks; print(tasks._last_alert_sent)'"

# 在容器内手动跑 pytest
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose exec -T -w /app/backend backend python3 -m pytest tests/ -v"

# 拉取最新日志（最近 1 小时）
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose logs --since=1h backend"

# 清理所有告警
ssh blueleaf@192.168.3.219 "cd ~/apimonitor && docker compose exec db psql -U apimon -d apimon -c 'TRUNCATE alerts;'"
```

---

## 7. Git 提交流程

```bash
# 本地提交
cd /home/opencode/opencode/apimonitor
git add -A
git commit -m "feat: xxx"
git push origin main

# 部署到 219（按上面 2.4 或 2.5）
```

---

## 8. 已知 Pitfall

- **端口 465 必须用 SMTP_SSL**：`use_tls=false` 但 `smtp_port=465` 时，代码会自动用 `SMTP_SSL`（已修复 `tasks.py` 和 `api/settings.py`）
- **数据库迁移是自动的**：启动时检查 `information_schema.columns`，缺什么加什么（已有数据不丢失）
- **cooldown 是内存变量**：重启容器会清空，会重新发邮件（`tasks.py:_last_alert_sent`）
- **PostgreSQL ENUM 类型比较问题**：`anomaly_type` 列在模型里用 `String(32)` 而非 `SQLEnum(AlertType)`，避免 `operator does not exist` 错误
- **Dockerfile 用阿里云 pip 源**：因为 pypi.org 在 219 上访问会超时
- **volumes 挂载**：`./backend:/app/backend` 覆盖镜像中的 backend，但 frontend 不会（如果改了 frontend 必须重建镜像）
