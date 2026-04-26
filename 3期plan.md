# 3期 plan：拒绝图片交互简化

## 状态

- 已完成 runtime 实现、文档更新、本地编译、热重载与静态验证。

## 范围

- 编辑目录：`data/plugins/astrbot_plugin_pjsk_pic` runtime 插件。
- 同步目录：`tmp_pjsk_pic_repo` 插件源码仓库。
- 本期只调整 Pixiv 审批页单张“拒绝图片”的前端确认逻辑。

## MVP

1. 单张“拒绝图片”点击后直接执行拒绝请求。
2. 保持拒绝成功后从当前审批队列移除图片。
3. 保持拒绝来源写入与后续爬虫跳过逻辑不变。

## 技术决策

- 删除 `rejectPixivReviewImage` 中的浏览器 `confirm()`。
- 不改动后端拒绝接口。
- 不改动批量审核、平台词删除、tag 归并等其他确认框。

## 验证方式

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated=true`
- 静态验证：
  - `rejectPixivReviewImage` 中不再包含 `confirm(`。
  - WebUI HTML 中该函数不再包含确认逻辑。

## 暂不做

- 不在真实审批数据上点击“拒绝图片”做破坏性验证。
- 不改批量审核确认框。
