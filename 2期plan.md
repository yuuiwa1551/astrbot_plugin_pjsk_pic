# 2期 plan：Pixiv 预览新增主 tag 热修

## 状态

- 已完成 runtime 实现、文档更新、本地编译、热重载与 WebUI 验证。

## 范围

- 编辑目录：`data/plugins/astrbot_plugin_pjsk_pic` runtime 插件。
- 同步目录：`tmp_pjsk_pic_repo` 插件源码仓库。
- 本期只修复 Pixiv 审核页新增主 tag 表单，不改变审核通过/拒绝的数据语义。

## MVP

1. 让卡片区和大图预览区的新增主 tag 表单拥有独立定位。
2. 点击预览区“添加”时读取预览区当前输入框。
3. 保持新增成功后自动选中主 tag 的行为不变。

## 技术决策

- 表单增加按显示区域生成的 scope。
- 提交时用 `data-pixiv-manual-tag-form` 先定位当前表单，再读取内部 input/checkbox。
- 避免全局 `document.getElementById` 在重复渲染场景下读到另一区域的空输入。

## 验证方式

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard API 热重载 `astrbot_plugin_pjsk_pic`
- `GET /api/plugin/get?name=astrbot_plugin_pjsk_pic` 确认 `activated=true`
- WebUI 验证：
  - 大图预览中输入新主 tag 后点击添加，不再提示“请先填写主 tag”。
  - 新主 tag 出现在候选主 tag 中，并进入已选主 tag。

## 暂不做

- 不重构整页状态管理。
- 不改变 Pixiv 审核通过/拒绝接口。
