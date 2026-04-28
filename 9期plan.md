# 9期计划：tag 归并候选阈值与自动采集边界说明

## 背景

`8期` 已经把疑似同一角色 tag 做成“待手动确认”候选池，但角色名重合规则仍偏保守：需要至少共享 2 个 CJK 字符才会明显加分。对于较短角色名、简繁/中日混排、同一角色不同写法，这会漏掉一部分需要人工确认的候选。

同时，Pixiv 自动采集当前是日常增量任务：按周期搜索最新结果，并受每 tag 结果数、每 tag 页数、全局新增任务数限制。它不是一次性把某个 tag 的全部历史结果扒完的历史回填工具，需要在配置和 README 中明确这个边界。

## Scope

- 放宽 tag 身份候选规则：角色名 CJK 字符约 40% 重合即可进入待确认候选。
- 在候选证据中记录并展示重合比例，帮助人工判断为什么它会出现。
- tag 归并页历史候选默认收起，提供“显示历史候选 / 取消显示”切换，减少日常归并页噪音。
- 更新配置说明和 README，明确 Pixiv 自动采集是最新增量采集，不是全量历史回填。
- 版本升级到 `0.14.4`。

## Deliverables

- `core/tag_identity_service.py`
  - CJK 重合规则从固定 2 字门槛调整为 40% 比例门槛。
  - `evidence.shared_ratio` 持久化到待确认候选证据。
- `webui/src/App.vue`
  - 待确认候选显示“重合字 + 约百分比”。
  - 历史候选默认隐藏，用户可手动显示，也可取消显示。
- `webui/src/styles.css`
  - 增加历史候选头部操作区样式。
- `_conf_schema.json`
  - 自动采集上限描述改为“单轮增量上限”。
- `README.md` / `plan.md` / `metadata.yaml`
  - 更新版本、说明与阶段记录。

## Validation

- 前端构建：`npm run build`
- 源码语法检查：`python -m compileall core main.py`
- runtime 语法检查：`python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- Dashboard 热重载插件，并确认 `astrbot_plugin_pjsk_pic` 仍为 `activated=true`
- API smoke：
  - `POST /api/tag-merge/identity-scan`
  - `GET /api/tag-merge/pending-candidates`
- WebUI smoke：
  - 打开 `#/tag-merge`
  - 确认默认不显示历史候选
  - 点击“显示历史候选”后出现历史候选与“取消显示”
  - 待确认候选展示重合比例

## Deferred

- Pixiv 历史全量回填任务暂不在本期实现。后续应单独做为“手动启动、有页码/游标、有明确上限和可暂停恢复”的任务，而不是混入日常自动采集。
