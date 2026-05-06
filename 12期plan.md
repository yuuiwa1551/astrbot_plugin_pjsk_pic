# 12期计划：P0 采集诊断与序号自查

## 范围

- 面向管理员补齐采集故障的轻量诊断入口。
- 补充失败采集任务查看与重试命令。
- 新增 `看看<序号>` / `/pp 看看 <序号>`，用于按当前会话最近展示图片序号自查图片详情。

## 交付内容

- `/pp 采集诊断`
  - 显示采集 worker、Pixiv 自动采集、refresh token 配置、订阅数量、任务状态计数、最近失败任务与最近 Pixiv 自动采集错误。
- `/pp 失败列表 [platform]`
  - 展示最近失败任务、URL、错误摘要和更新时间。
- `/pp 失败重试 <job_id|全部>`
  - 支持指定任务重试，也支持批量重试最近失败任务。
- `看看<序号>` / `/pp 看看 <序号>`
  - 序号来源于当前会话最近一次展示的图片列表。
  - 默认仅管理员可用。
  - 查看时发送原图和完整图片详情。
- 配置项
  - `numeric_inspect_enabled`
  - `numeric_inspect_admin_only`
  - `numeric_inspect_ttl_minutes`
  - `numeric_inspect_max_items`

## 验证方法

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- AstrBot Dashboard 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated = true`
- 命令行为检查：
  - `/pp 采集诊断`
  - `/pp 失败列表`
  - `/pp 失败重试 <job_id>`
  - `/pp 审核列表` 后发送 `看看1`
  - 原自然语言发图 `看看<tag>` 不被 `看看<序号>` 破坏

## 暂缓事项

- 不做实时 Pixiv 网络连通性请求，避免诊断命令因网络阻塞。
- 不做 WebUI 失败队列重构，本期只补聊天侧管理入口。
- 不做长期持久化序号缓存，缓存仅在插件运行期按会话保存。
