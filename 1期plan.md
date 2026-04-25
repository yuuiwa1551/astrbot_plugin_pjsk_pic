# 1期 plan：Pixiv 审核页通过/拒绝闭环

## 状态

- 已完成 runtime 实现、文档更新、本地编译、热重载与 WebUI 验证。

## 范围

- 编辑目录：`data/plugins/astrbot_plugin_pjsk_pic` runtime 插件。
- 同步目录：`tmp_pjsk_pic_repo` 插件源码仓库。
- 本期只处理 Pixiv 审核页，不重构其他 WebUI 页面。

## MVP

1. 候选主 tag 区增加加号入口。
2. 支持在审核页创建或复用主 tag，并自动加入当前图片选择。
3. 通过审核时，如果同时选择主 tag 和 Pixiv 来源 tag，则写入平台词映射。
4. 审核页新增“拒绝图片”按钮。
5. 拒绝图片时记录 Pixiv 来源为已拒绝，并把该图待审核项标记为人工拒绝。
6. 自动 Pixiv 来源搜图跳过已拒绝来源。

## 技术决策

- 使用现有独立 WebUI 服务和 SQLite 数据库。
- 主 tag 仍使用 `tags` 表。
- 来源 tag 映射仍使用 `platform_tag_terms` 表。
- 图片级拒绝新增独立的来源拒绝记录，避免把 tag 级 `manual_rejected` 误当成整图拒绝。
- 自动爬虫在入队前同时检查已入库来源和已拒绝来源。

## 验证方式

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated=true`
- WebUI/API 验证：
  - 新增主 tag 后自动选中。
  - 通过审核后可看到 Pixiv 来源词映射。
  - 拒绝图片后当前卡片消失。
  - 已拒绝 Pixiv 来源会被自动搜图跳过。

## 暂不做

- 不删除本地图片文件。
- 不做复杂的拒绝恢复 UI。
- 不改变非 Pixiv 平台的审核流程。
