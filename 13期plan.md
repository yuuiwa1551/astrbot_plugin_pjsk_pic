# 13期：自然语言图片 ID 查看

## 范围

- 支持管理员自然语言发送 `看看id<image_id>`、`看看 ID <image_id>` 查看数据库图片 ID 对应的图片详情。
- 保留现有 `看看<序号>` 语义，不把 `看看1` 改成数据库图片 ID 查询。
- 避免 `看看id123` 继续落入自然语言 tag 查询并静默无响应。

## 交付内容

- 在自然语言发图入口前置识别图片 ID 查询表达。
- 复用现有 `_send_image_detail_by_id` 详情发送逻辑，继续展示图片、来源、tag 与管理提示。
- 图片 ID 查看入口复用序号自查开关与管理员权限配置。
- 更新 README、`plan.md` 与 `metadata.yaml` 版本号。

## 验证方法

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- AstrBot Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认插件仍为 activated

## 延后事项

- 暂不新增单独的图片 ID 查看开关；如后续需要开放给普通用户，再拆出独立配置项。
