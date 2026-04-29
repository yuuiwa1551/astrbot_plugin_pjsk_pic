# 11期计划：采集入审实时刷新与 Pixiv include 过滤修正

## Scope

- 修正 Pixiv 采集 include / exclude 过滤：规则词同时展开主 tag、alias、Pixiv 平台词和内置常用 Pixiv 查询词。
- Pixiv 历史回填创建普通采集任务时，把实际查询词纳入 include 规则。
- WebUI 对采集任务页和 Pixiv 审批页做静默自动刷新，让新入审图片更快进入可见列表。

## Deliverables

- `core/pixiv_tag_terms.py`：沉淀内置 Pixiv 常用查询词 helper。
- `core/crawl_service.py`：过滤与归属判断支持 Pixiv 平台词 / alias 扩展。
- `core/pixiv_backfill_service.py`：回填入队的采集任务携带 query terms。
- `webui/src/App.vue`：任务页与 Pixiv 审批页低频静默刷新。
- 版本更新到 `0.14.6`。

## Validation

- `npm run build`
- `python -m compileall core main.py`
- runtime `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- AstrBot Dashboard 热重载并确认 `activated=true`
- API smoke：Pixiv 审批接口可访问，采集任务接口可访问

## Deferred

- WebSocket / SSE 实时推送。
- 对已经失败的历史采集任务批量自动重试。
- 历史回填任务的无上限继续扫描语义。
