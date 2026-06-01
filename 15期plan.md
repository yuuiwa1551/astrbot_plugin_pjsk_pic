# 15期计划：审核页性能、Pixiv 收敛与重复忽略

## 背景

当前 Pixiv 审核队列堆积到千级图片后，WebUI 审批页首次加载会明显变慢；自动 Pixiv 采集会把普通 alias 作为搜索词，短英文 alias（如 `rin`）容易命中其他作品 tag；投稿时 pHash 疑似重复提示缺少单对人工豁免能力。

## 范围

### 1. WebUI 审核页性能

- 为 Pixiv 审核查询补充组合索引，降低待审图片 count/list 的扫描成本。
- Pixiv 审核页不再参与 6 秒静默刷新，避免队列大时反复触发重查询。
- 列表接口仅返回轻量摘要，点击预览时再加载来源词解析和候选 tag 详情。
- 保留手动刷新按钮和分页能力。

### 2. Pixiv 自动采集收紧

- 自动采集不再默认使用普通 alias 作为 Pixiv 查询词。
- 查询词优先使用显式 Pixiv 平台词和内置官方 Pixiv 词映射。
- Pixiv 结果归属判断取消 substring 命中，避免 `rin` 命中 `Durin` 等误判。
- 自动采集和历史回填复用同一套严格匹配口径。

### 3. 疑似重复人工忽略

- 新增图片相似关系忽略表。
- 新增 `/pp 重复忽略 <id1> <id2> [原因]`。
- 新增 `/pp 重复恢复 <id1> <id2>` 和 `/pp 重复忽略列表 [image_id]` 作为辅助管理入口。
- 投稿 / 采集返回疑似重复 ID 前过滤已忽略的图片对。

## 验证方法

- `python -m compileall data/plugins/astrbot_plugin_pjsk_pic`
- WebUI 前端构建：`npm run build`
- 使用 live DB 对比 Pixiv 审核页 count/list 查询耗时。
- 用脚本验证 `rin` 不命中 `Durin`，`鏡音リン` 能命中 `镜音铃`。
- Dashboard 热重载并确认 `astrbot_plugin_pjsk_pic` 保持 activated。

## 暂缓项

- 图片缩略图缓存接口暂缓；若 SQL 与刷新策略优化后仍慢，再单独做。
- 旧误抓图片不自动清理，只通过严格规则阻止后续继续误入队。
