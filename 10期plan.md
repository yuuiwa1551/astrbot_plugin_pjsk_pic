# 10期计划：Pixiv 历史回填任务

## 背景

日常 Pixiv 自动采集是“最新增量”逻辑：按周期搜索最新结果，命中上次看到的作品后停止，不适合把某个 tag 的历史结果一次性补齐。用户需要一个独立的历史回填能力：明确指定 tag、页数和上限，然后逐页把旧 Pixiv 搜索结果转成普通采集任务。

## Scope

- 新增 Pixiv 历史回填任务，不复用自动采集订阅的 `last_seen_source_uid`。
- 任务由用户手动创建，必须有明确的页数、扫描结果数和新增采集任务上限。
- 回填任务只负责搜索和入队；下载、入库、审核仍由现有 `CrawlService` 和 Pixiv 审批页处理。
- WebUI 在“采集任务”页展示历史回填任务的创建入口、进度和统计。
- 命令侧提供基础创建和列表查看能力。

## Deliverables

- `core/pixiv_backfill_service.py`
  - 后台 worker。
  - tag 解析、Pixiv query 词选择、逐页搜索、过滤和普通采集任务入队。
- `core/pixiv_search_service.py`
  - 新增单页搜索 API，返回 `hits + next_offset`。
- `core/db.py`
  - 新增 `pixiv_backfill_tasks` 表。
  - 新增创建、更新、列表、重试与运行中任务恢复方法。
  - 新增按来源 URL 检查已有采集任务的方法，避免重复入队。
- `core/webui.py`
  - 新增 `/api/jobs/pixiv-backfill` 和 `/api/jobs/pixiv-backfill/retry`。
- `webui/src/App.vue`
  - 采集页新增 Pixiv 历史回填面板。
  - 显示任务当前搜索词、页码、扫描数、匹配数、入队数和跳过统计。
- `main.py`
  - 插件启动/停止时管理回填 worker。
  - 新增 `/pp 历史回填添加`、`/pp 历史回填列表`。

## Validation

- 前端构建：`npm run build`
- 源码语法检查：`python -m compileall core main.py`
- runtime 语法检查：`python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard 热重载并确认 `activated=true` 且版本为 `0.14.5`
- API smoke：
  - `GET /api/jobs/pixiv-backfill`
  - `POST /api/jobs/pixiv-backfill`
  - `GET /api/jobs`
- WebUI smoke：
  - 打开 `#/jobs`
  - 确认“Pixiv 历史回填”面板加载
  - 刷新任务列表无控制台 error

## Deferred

- 暂停、取消、继续从已保存 offset 恢复。
- 按任务查看每个 Pixiv 作品的详细跳过原因。
- 对历史回填增加单独的速率限制和时间窗口设置。
