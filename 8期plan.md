# 8期计划：tag 归并候选与 Pixiv 空 URL 搜图

## 目标

- 把“疑似同一角色 tag”做成待手动确认的候选流，不自动归并。
- Pixiv 采集页 URL 为空时按输入 tag 搜图预览，勾选作品后再创建真实采集任务。
- 明确 Pixiv 平台词类型：`query` 用于搜图，`match` 用于归属判断，`both` 两者都参与。

## 实现范围

- 新增 `tag_merge_identity_candidates` 表，保存来源 tag、目标 tag、状态、置信分、生成原因、Pixiv 证据和 LLM 复核结果。
- 新增 `TagIdentityService`，基于 CJK 重合字、alias、平台词和历史来源词生成候选；LLM 不可用时仍保留候选并标记待人工判断。
- WebUI tag 归并页新增“待手动确认”分区，支持扫描、使用并预览、忽略候选。
- `/api/jobs/pixiv-search-preview` 支持空 URL Pixiv 搜图预览，优先使用主 tag 的 query/both 平台词。
- 采集页勾选预览作品后复用 `/api/jobs` 创建真实 Pixiv 采集任务。

## 验证

- `python -m compileall core main.py`
- `npm run build`
- 同步到 runtime 后运行 `python -m compileall data\plugins\astrbot_plugin_pjsk_pic`
- 热重载插件并确认 `activated=true`、版本为 `0.14.3`
