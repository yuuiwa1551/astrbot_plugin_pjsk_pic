# 14期：移除最近展示序号自查

## 范围

- 移除 `看看<序号>` / `/pp 看看 <序号>` 最近展示列表序号自查功能。
- 保留 `/pp 看图 <image_id>` 与 `看看id<image_id>` 数据库图片 ID 查看能力。
- 避免发图结果和审核列表继续提示 `自查：看看N`，减少序号与图片 ID 混淆。

## 交付内容

- 删除会话级自查序号缓存与相关解析、命令、提示。
- 自然语言发图入口不再拦截纯数字 `看看1` 这类消息。
- `看看id<image_id>` 改用独立配置项 `image_id_lookup_enabled` / `image_id_lookup_admin_only`。
- 更新 README、`plan.md`、`_conf_schema.json` 与 `metadata.yaml` 版本号。

## 验证方法

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- AstrBot Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认插件仍为 activated

## 延后事项

- 暂不新增 `看看123` 作为图片 ID 简写，避免再次和自然语言或数字 tag 语义混淆。
