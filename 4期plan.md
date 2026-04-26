# 4期 plan：Pixiv 审批规范主 tag 与 alias 闭环

## 状态

- 已完成 runtime 实现、文档更新、本地编译、热重载与临时 DB 验证。

## 范围

- 编辑目录：`data/plugins/astrbot_plugin_pjsk_pic` runtime 插件。
- 同步目录：`tmp_pjsk_pic_repo` 插件源码仓库。
- 本期只调整 Pixiv 审批通过路径的业务语义，不改变整图拒绝和拒绝来源跳过逻辑。

## MVP

1. Pixiv 审批页候选 tag 改为“归入主 tag”单选。
2. Pixiv 来源 tag 改为 alias / Pixiv 搜索词多选。
3. 审核通过时只给图片写入一个规范主 tag。
4. 选中的同义词写入 `tag_aliases`，并尽量写入 `platform_tag_terms`。
5. 如果同义词已经是独立 tag，则归并到规范主 tag。
6. 自动来源搜图继续使用规范主 tag 的 alias / Pixiv 搜索词搜索和匹配。

## 技术决策

- `selected_tag_names[0]` 是规范主 tag。
- `selected_tag_names[1:] + source_terms` 作为同义词集合处理。
- 独立 tag 同义词走现有 `merge_tags` 底层迁移逻辑。
- alias / 平台词冲突时不覆盖，返回跳过原因。
- 批量审核继续兼容多主 tag 输入，但语义变为第一个是主 tag，其余是同义词。

## 验证方式

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated=true`
- 临时 DB 验证：
  - `晓山瑞希` 为规范主 tag。
  - `暁山瑞希`、`Akiyama Mizuki` 作为同义词提交。
  - 图片最终只保留 `晓山瑞希` 的人工通过 tag。
  - `暁山瑞希`、`Akiyama Mizuki` 可被规范主 tag 的 alias / 搜索词解析。

## 暂不做

- 不自动沉淀用户没有选中的泛用 Pixiv tag。
- 不自动覆盖已归属其他角色的 alias / Pixiv 平台词。
- 不重构 Pixiv 平台词管理页。
