# 7期计划：WebUI 图片流分页与审核交互整理

## 范围

- 图片检索和 Pixiv 审批补充分页、跳页和每页数量切换。
- 前端状态枚举改为中文展示，图片检索默认查看已通过图片。
- 移除重复的“审核任务”前端页面，保留后端 API 与数据库结构。
- 稳定 Pixiv 审批卡片和预览弹窗布局，图片完整显示，tag 多时局部滚动。

## 交付

- `/api/images` 和 `/api/pixiv-review-images` 返回 `total`、`limit`、`offset`、`page`、`page_count`。
- `/api/images` 支持逗号分隔的多状态筛选。
- Vue WebUI 提供每页 15 / 30 / 60 / 100 的选择，默认 30。
- Pixiv 审批处理完当前页后自动刷新，必要时跳到仍有数据的页。
- README、`metadata.yaml`、前端 package 版本更新到 `0.14.2`。

## 验证

- `npm run build`
- `python -m compileall core main.py`
- runtime `python -m compileall data\plugins\astrbot_plugin_pjsk_pic`
- Dashboard 热重载并确认 `activated=true`
- 浏览器检查图片检索、Pixiv 审批分页、旧 `#/reviews` 跳转和控制台错误

## 延后

- 不删除后端 `/api/reviews` 和 `review_tasks` 表。
- 不给 tag 管理、平台词、采集任务增加图片分页。
