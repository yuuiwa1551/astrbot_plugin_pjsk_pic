# 采集管线优化说明

## 目标

`v0.20.0` 优化搜索、发现、任务分发、详情解析、下载、去重和入库主路径。实现重点是删除重复工作、提前过滤、减少 SQLite 事务以及解除历史回填对最新任务的排队阻塞；没有增加新的重试、熔断或备用 provider。

## 优化前 live 基线

- Pixiv：2303 完成、11 失败；小红书：9 完成、0 失败。
- 最近同批 14 个 Pixiv 任务的最后一项延迟 45 秒。
- 历史回填所在窗口任务完成延迟 P50 约 5583 秒、P95 约 24779 秒。
- 小红书 discovery 到 crawl job 延迟 P50 约 10002 秒，最大约 31687 秒。
- Pixiv 历史完成任务中 1149/2303 导入 0 张；最近仍有 2/20 因 `R-18 / AI` 在详情阶段才排除。
- 容器绑定盘上，1000 次查询每次新建 SQLite 连接约 8697 ms，复用一个连接约 1030 ms。
- 新 URL 的 5000 次来源查询在旧索引副本约 6417 ms，迁移复合索引后的副本约 232 ms。

## 发现、过滤与水位

- Pixiv 自动采集和历史回填复用 CrawlService 的过滤集合，在搜索结果已有 tag 时直接排除。
- 自动 discovery 写入 `filters_applied=true`，任务阶段不再对同一自动结果重复执行默认过滤；手工 URL 任务仍在详情解析后应用过滤。
- Pixiv 查询词保存 `scan_offset`、`scan_high_watermark`、`scan_target_source_uid`。旧水位未出现时保留原水位并保存下一页 offset；后续快速周期继续，结束后一次性提交高水位。
- Pixiv / 小红书每轮先 drain pending discovery。剩余任务预算为 0 时不会继续搜索扩大积压。
- 有 discovery 或 Pixiv checkpoint 时使用 30 秒快速周期；正常搜索周期保持原配置。

## 详情与作品处理

- 小红书匹配时取得的 `XhsNoteDetail` 序列化为任务快照，包含标题、正文、话题、作者和完整图片列表；XiaohongshuAdapter 优先直接读取。
- Pixiv 搜索命中保存精简详情快照；快照覆盖声明的全部页且每页都有 original URL 时 PixivAdapter 才直接使用，否则调用详情 API 补全。
- discovery 提交后释放自身快照，job 完成后再释放任务快照；失败任务保留现场供排查，避免成功历史记录持续占用数据库空间。
- 管理员显式重试失败任务时移除旧详情快照，只保留作品标识和访问上下文，由适配器重新取得最新图片 URL。
- 同一个 job 将各页公共 raw/translated tags 合并后只规范化一次，并预先解析 tag ID/角色类型。
- 图片可以并发下载；审核决策完成后，source、图片 tag 和 review task 在一个事务内提交。

## 队列

- `crawl_jobs.origin`：`manual`、`auto_incremental`、`backfill`。
- 默认优先级：手工 `0`、最新增量 `20`、历史回填 `50`。
- 已排队任务收到更高优先级来源时可提升，PriorityQueue 使用惰性跳过旧条目，任务只处理一次。
- 队列分别记录 queued 与 running 身份；任务出队到状态落库之间发生优先级提升，也不会被第二个 worker 重复处理。
- 默认两个 worker。Pixiv / CDN 工作可以并行；小红书 provider 自身继续按客户端串行。
- 历史回填仅在队列低于 `crawl_backfill_queue_high_watermark` 时继续创建任务。

## 下载与去重

- ImportedImageService 使用共享 requests Session 和连接池，不再为每张图片重新建立 HTTP 客户端。
- 同作品使用受限并发下载，结果按原候选顺序提交。
- 下载内容先计算 SHA-256 并查询现有活动图片；完全相同内容直接复用，不再解码或计算 pHash。
- 未命中 SHA 时才解码并计算 pHash；相似图查询覆盖全部活动 pHash，不再只看最近 500 张。

## 数据迁移

新增内容：

- `crawl_subscription_terms.scan_offset`
- `crawl_subscription_terms.scan_high_watermark`
- `crawl_subscription_terms.scan_target_source_uid`
- `crawl_jobs.origin`
- `crawl_jobs.priority`
- `sources(platform, post_url)` 复合索引
- `crawl_jobs(platform, source_url)` 复合索引
- `crawl_jobs(status, priority, id)` 队列索引

迁移只新增字段和索引。使用正式数据库副本验证，images、sources、crawl_jobs、subscriptions、subscription_terms、review_tasks 数量迁移前后完全一致，`PRAGMA integrity_check=ok`。

## 非本期范围

- 小红书历史分页。
- 增加 provider fallback、额外重试或熔断层。
- 改变 LLM 图片审核的 shadow 状态。
- 为未实际使用的 X / lofter / generic 适配器扩展自动发现。
