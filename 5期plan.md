# 5期 plan：Pixiv 审批页角色搜索与审核交互重整

## 状态

- 已完成 runtime 实现、文档更新、本地编译、热重载与 live DB 搜索验证。

## 范围

- 编辑目录：`data/plugins/astrbot_plugin_pjsk_pic` runtime 插件。
- 同步目录：`tmp_pjsk_pic_repo` 插件源码仓库。
- 本期只调整 Pixiv 审批页筛选和列表查询，不改变审核提交、拒绝来源和 tag 合并语义。

## MVP

1. Pixiv 审批页顶部提供角色 / alias / Pixiv tag 搜索框。
2. 搜索命中主 tag 后展开主 tag 名、alias、Pixiv 平台词。
3. 宽松纳入历史来源中出现过且能命中现有 tag 的同义角色候选。
4. 搜索结果显示命中主 tag 和展开词说明。
5. 批量审核区独立于筛选区，未勾选图片时保持收起。

## 技术决策

- `/api/pixiv-review-images` 新增 `keyword` 参数。
- 空 `keyword` 完全保留原待审队列逻辑。
- DB 层统一构建 `search_context`，前端只负责展示说明和传参。
- 搜索不自动创建 alias、不自动合并 tag；归并仍发生在审核确认或 tag 管理里。

## 验证方式

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- live DB 验证：
  - 空搜索仍返回默认待审队列。
  - `mzk` 展开到 `晓山瑞希`、`暁山瑞希`、`Akiyama Mizuki` 并返回相关待审图。
  - `Akiyama` 返回同一批瑞希相关待审图。
- Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated=true`

## 暂不做

- 不把搜索结果自动写入 alias / 平台词。
- 不做跨角色语义推理；只能利用已有 tag、alias、平台词和历史来源候选。
- 不重构 Pixiv 平台词管理页。
