# rpaloop · 政务材料录入 RPA 录制回放台

苏城企服（苏州政务外包公司，86 人）的政务材料录入 RPA 值守台：把办事员在政务网/税务/网银的
30-80 个字段录入动作录制为流程并回放，用**定位器稳健性评分**提前发现「页面改版后旧脚本悄悄填错位置」，
用**步骤 DAG 调度 + 断点续跑 + 异常接管**保证无人值守批次失败可被发现、可续跑、可导出审计报告。

## 真实场景

- **模拟客户**：苏城企服，86 名办事员，服务 200+ 苏州企业（年报、高企资质、稳岗补贴、工商变更）。
- **现有工作流**：办事员登录政务网逐字段手工录入，单次 25 分钟，完成后截图留证放共享盘。
- **3 个痛点**：① 12 类事项重复录入、人工易错；② 页面改版后旧脚本定位全挂却不报错（填错位置）；
  ③ 无人值守批次失败无人知晓，第 3 天才发现材料过期。
- **上下游**：上游=企业档案/材料清单（材料库 + GB 32100 信用代码校验）→ 本系统替代「流程录制、
  定位器评分、无人值守执行、异常接管」→ 下游=申报回执、审计留档（执行报告 CSV/Markdown 导出）。

## 角色与权限

| 账号 | 密码 | 角色 | 权限 | 典型动作 |
|---|---|---|---|---|
| `admin@rpaloop.io` | `Demo@2026` | 平台管理员 | 全部 | 发布流程新版本、归档旧版、材料库维护 |
| `engineer@rpaloop.io` | `Demo@2026` | 值守操作员 | 执行、干跑、接管处理；**不能发布流程版本** | 干跑、重试接管、取消 run |
| `viewer@rpaloop.io` | `Demo@2026` | 审计只读 | 只读 + 导出报告；**接管 403** | 导出执行报告 |

权限在后端 FastAPI 依赖（`require_roles`）强制校验，前端仅按角色隐藏按钮。

## 快速启动

```bash
cp .env.example .env          # 按需修改端口/密码
docker compose config --quiet # 校验 compose
docker compose up -d --build  # 首次构建约 2-4 分钟
docker compose ps             # 等 5/5 healthy
```

- 前端：<http://localhost:18237>
- 后端 API 文档：<http://localhost:19237/api/docs>
- 健康检查：`curl http://localhost:19237/api/health`

`SEED_DEMO=true` 时后端启动自动执行一次 seed（幂等），也可手动：

```bash
docker compose exec backend python -m scripts.seed_demo
# SEED_SUMMARY brand=rpaloop processes=4 steps=32 materials=6 runs=8 runsteps=45 interventions=3 users=3
```

## 核心功能

| 页面 | 路径 | 说明 |
|---|---|---|
| 登录 | `/login` | 三角色演示账号，一键填入 |
| 流程库 | `/processes` | 4 个流程（年报 9 步 / 资质 7 步 / 补贴 8 步 / 变更 8 步），待审候选/待发布版本角标 |
| 步骤编辑器 | `/processes/:id/edit` | 左侧步骤灯列 + 右侧定位器候选与稳健分、断言/重试配置、干跑；含「导入录制（候选审阅）」与版本切换 |
| 录制候选审阅 | `/processes/recordings/:id` | 按字段名称/动作/样例值列出新增、缺失、疑似错位；疑似错位逐条人工确认 |
| 值守看板 | `/board` | 队列、进行中 run 步骤灯、失败高亮，2s 轮询（步骤按运行绑定版本解析） |
| 运行详情 | `/runs/:id` | 步骤时间线、命中定位器、幂等键、接管记录、日志、报告导出 |
| 接管队列 | `/interventions` | 重试 / 改定位器 / 跳过 / 终止；viewer 403 |
| 材料库 | `/materials` | GB 32100 信用代码校验位真算，2 条非法标红且不能发起申报 |

## 核心算法

### 算法 A：定位器稳健性评分 `backend/src/services/locator_score.py`

- **输入**：步骤 `locator_candidates`（strategy + selector）+ 该步骤近 8 次 `RunStep` 命中记录
  （`locator_used` / `attempt` / `status`）。
- **指标**：`hit_rate`=命中次数/历史样本；`avg_attempt_score`=1/avg_attempts；
  `specificity`=策略基准分(0.85 id / 0.80 text / 0.75 role / 0.55 css / 0.35 xpath)
  − 层级深度惩罚 − 选择器长度惩罚；`dynamic_risk`=正则检出 `\d{4,}`、uuid、时间戳、hash → 1.0。
- **公式**：`score = 100·(0.45·hit_rate + 0.20·avg_attempt_score + 0.20·(1−dynamic_risk) + 0.15·specificity)`。
- **输出**：每候选分、推荐主/备定位器、步骤稳健分与风险等级（high/medium/low）+ 原因，全部落库
  `steps.locator_scores / robustness_score / risk_level / risk_reason`。
- **失败态**：无历史 → `score=50`, `reason=no_history`（seed 中变更流程即为此态）。
- **可复现**：`POST /api/processes/{id}/rescore` 按当前历史确定重算。

### 算法 C：录制候选化审阅 + 版本快照切换

针对「页面改版后『法人姓名』被录到相邻『经办人姓名』，稳健分仍认可、发布后材料悄悄填错」的风险：

- **导入即候选，不动正式流程**：`POST /api/processes/{id}/import` 不再直接写步骤，
  而是生成 `recording_imports` 批次（`recording_service.py`），与当前**已发布版本快照**做差异分析
  （`recording_diff.py`），按**字段名称、动作、样例值**输出五类差异：
  `added`（新增）/ `missing`（缺失）/ `suspected_misalign`（疑似错位）/ `updated`（更新）/ `unchanged`。
- **疑似错位判定**：同序号录入类字段名称对不上且无同名承接；定位器语义命中其他字段
  （如 `field-agent` 映射「经办人」，含中英关键词映射表）；依赖 `nth-child`/兄弟轴等相邻结构。
  旧字段若从录制消失同时计入 `missing`。
- **人工确认门禁**：`suspected_misalign` 必须逐条勾选确认，否则 `confirm` 返回 422
  `MISALIGN_NOT_CONFIRMED`；确认前正式运行始终引用原 published 快照。
- **不可变版本 + 原子切换**：确认后生成 `process_versions` 的 `pending_publish` 快照；
  发布时候选转 `published`、旧版本转 `superseded`、工作区步骤同步替换，整条流程一次切换。
- **运行绑定版本**：`runs.process_version` 在创建时固化，执行器（`executor.load_run_steps`）
  始终按该版本的 `steps_snapshot` 回放；后续发布不影响已启动/历史运行，看板/接管/报告同样按绑定版本解析。
- **幂等导入**：录制内容（含 replace 标志）做 sha256 指纹，同一份录制重复导入复用原批次、不新增版本；
  已驳回/撤回的批次不参与去重，允许修正后重新导入。
- **冻结期保护**：存在待发布版本时工作区步骤冻结（编辑/评分/再导入均 409），需先发布或驳回。

### 算法 B：步骤 DAG 调度 + 断点续跑 / 失败接管 `backend/src/services/executor.py`

- **DAG**：步骤 `depends_on`（空=链式依赖 seq-1）→ Kahn 拓扑排序，执行序列落库 `runs.execution_sequence`。
- **幂等键**：`sha256(run_id + step_id + material.credit_code)`；续跑时已成功且幂等键一致的步骤跳过，
  跳过集合落库 `runs.skipped_steps`。
- **失败分类**：transient(timeout/network) → 指数退避 1/2/4s 至 `max_retries`；
  `assert_failed`/`locator_drift` → 生成 `Intervention` 并把 run 置为 `takeover` 暂停。
- **并发**：同一 Material 同时仅允许 1 个进行中的 run —— `runs.active_lock` 唯一约束 + 材料行锁，
  冲突返回 409 `E_CONCURRENCY_CONFLICT`。
- **可复现**：`dry_run` 不真实等待；assert 失败由 `Material.missing_flags` 固定触发。
- **执行器**：Celery worker（Redis broker）消费 `rpaloop.execute_run`，逐步骤记录
  `run_steps`（attempt/耗时/命中定位器/错误码/幂等键/output_digest）。

### GB 32100 统一社会信用代码校验 `backend/src/utils/credit_code.py`

字符集 31 位（0-9 + A-Z 去掉 I/O/S/V/Z）；前 17 位按权重 `[1,3,9,27,19,26,16,17,20,29,25,13,8,24,10,30,28]`
加权求和取模 31，`check = 31 − (sum mod 31)`（31→0），与第 18 位比对。seed 中 2 家企业（华泰针织、信德模塑）校验位不匹配。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + TypeScript + Vite + Zustand + React Router 6，Nginx 托管 dist |
| 后端 | Python 3.11 + FastAPI 0.115 + SQLAlchemy 2.0（同步）+ Pydantic v2 + PyJWT + Passlib |
| 异步 | Celery 5.4 + Redis 7（broker/backend），worker 执行 run |
| 数据库 | MySQL 8.0（utf8mb4），Alembic 迁移 |
| 部署 | Docker Compose，5 服务（frontend/backend/worker/db/redis），全部 healthcheck |

## 目录结构

```
gb-564/
├── docker-compose.yml          # 5 服务 + 命名卷 + 5 个 healthcheck
├── .env / .env.example
├── backend/
│   ├── Dockerfile / Dockerfile.worker / entrypoint.sh / requirements.txt
│   ├── alembic.ini / migrations/
│   ├── scripts/seed_demo.py
│   └── src/{config,constants,models,schemas,services,controllers,routes,middlewares,utils}
└── frontend/
    ├── Dockerfile / nginx.conf
    └── src/{api,stores,types,components/common,components/business,hooks,pages,router,utils,constants,styles}
```

## 枚举出现位置（前后端各一份）

| 枚举 | 后端 | 前端 |
|---|---|---|
| UserRole | `backend/src/constants/enums.py` `roles.py` | `frontend/src/constants/enums.ts` `types/index.ts` |
| MatterType / ProcessStatus | `backend/src/constants/enums.py` | `frontend/src/constants/enums.ts` |
| StepAction / StepStatus | `backend/src/constants/enums.py` | `frontend/src/constants/enums.ts` |
| RunStatus / RunTrigger / RunStepStatus | `backend/src/constants/enums.py` `models/run*.py` | `frontend/src/constants/enums.ts` `types/index.ts` |
| InterventionReason / InterventionStatus | `backend/src/constants/enums.py` `models/intervention.py` | `frontend/src/constants/enums.ts` |
| RiskLevel | `backend/src/constants/enums.py` `locator_score.py` | `frontend/src/constants/enums.ts` `utils/risk.ts` |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | rpaloop | 容器名前缀 |
| `FRONTEND_PORT` / `BACKEND_PORT` / `DB_PORT` | 18237 / 19237 / 17737 | 宿主端口 |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_ROOT_PASSWORD` | rpaloop / rpaloop / … | MySQL |
| `JWT_SECRET` / `JWT_EXPIRE_MINUTES` | demo / 720 | JWT 签名 |
| `REDIS_URL` | redis://redis:6379/0 | Celery broker |
| `SEED_DEMO` | true | 启动自动 seed |
| `RATE_LIMIT_PER_MINUTE` | 240 | 限流中间件阈值 |

## Docker 说明

- 5 服务全部带 healthcheck：db（mysqladmin ping）、redis（redis-cli ping）、backend（curl /api/health）、
  worker（celery inspect ping）、frontend（wget 首页）。
- 依赖顺序：db/redis healthy → backend healthy → worker/frontend。
- 命名卷：`db_data`（MySQL）、`redis_data`（Redis AOF）、`shared_artifacts`（backend/worker 共享产物目录）。
- 停止并清理：`docker compose down -v --remove-orphans`。

## 本地开发

```bash
# 后端（需本机 MySQL/Redis，或仅用容器 db/redis）
cd backend && pip install -r requirements.txt
DB_HOST=127.0.0.1 DB_PORT=17737 uvicorn src.main:app --reload --port 19237
# worker
celery -A src.config.celery_app worker -l info --concurrency=2
# 前端
cd frontend && npm install --registry=https://registry.npmjs.org && npm run dev
# 后端测试（SQLite 内存库，无需 MySQL/Redis）
cd backend && python -m unittest discover -s tests
```

## License

MIT（演示项目，数据均为虚构）。
