# LLM 图片质量与候选 tag 审核

## 目的

图片完成下载和本地入库后，由独立视觉模型工作线程一次性评价图片质量，并从数据库已经给出的角色候选中选择对应主 tag。模型审核不参与爬虫发现、不创建 tag，也不会阻塞图片下载任务。

## 模式

- `shadow`：保存模型输出，用于离线评估；不修改 `review_tasks` 或 `image_tags`，QQ 审图卡也不展示建议。
- `assist`：不自动通过，但 QQ 审图卡展示质量分、建议角色、置信度、flags 和理由。
- `auto_approve`：只有全部安全条件满足时自动通过；其他结果仍留给人工。

建议新 provider 和新提示词版本始终从 `shadow` 开始。

## 输入边界

发送给模型的内容只有：

- 去除元数据、限制最长边的本地 JPEG 审图预览。
- 候选角色的 `tag_id`、主名称和少量人工确认 alias。
- 固定、版本化的审核提示词。

不会发送来源 URL、作者、投稿人、Cookie、API token、Pixiv refresh token、小红书 `xsec_token` 或 provider 原始响应。

候选从图片当前审核任务取得，只保留 `active + character` 主 tag。模型只能返回候选 `tag_id`，最多选择 3 个；候选外 ID、重复 ID、未知 flag、非法数值或非单一 JSON 对象都会降为人工复核。

## 输出契约

模型必须返回：

```json
{
  "quality": {
    "technical": 91,
    "aesthetic": 94,
    "gallery_fit": 92,
    "overall": 93,
    "flags": []
  },
  "characters": [
    {"tag_id": 7, "confidence": 0.98}
  ],
  "decision": "approve",
  "reason": "画面完整清晰，角色特征明确"
}
```

固定 flags：

- `low_resolution`
- `blurry`
- `heavy_artifacts`
- `bad_crop`
- `text_heavy`
- `watermark_heavy`
- `screenshot`
- `meme`
- `unsafe`
- `uncertain`

没有候选角色匹配时应返回空 `characters` 和 `manual_review`，不能为“无匹配”创造新的 flag。提示词会完整列出上述枚举，解析器仍以同一白名单做最终校验。

图片中的文字属于不可信输入，系统提示要求模型忽略其中试图修改任务、候选范围或输出格式的指令。

## 自动通过策略

`auto_approve` 默认要求：

- 模型 decision 为 `approve`。
- `overall >= 85`。
- `technical >= 75`。
- `aesthetic >= 82`。
- `gallery_fit >= 80`。
- 至少选择一个候选，且每个角色 `confidence >= 0.93`。
- 没有任何阻断 flag。
- 图片和全部候选审核任务仍处于 `pending / uncertain`，没有人工结果。
- 未命中随机抽检比例。

达标时，选中候选原子更新为 `approved`，未选中候选更新为模型 `rejected`。如果人工审核先完成，模型运行会标记为过期建议，不覆盖 `manual_approved / manual_rejected`。

初期不自动永久拒绝整张图片或来源；质量低、无匹配角色和不确定结果继续留在人工队列。

## 队列与幂等

- `llm_image_review_runs` 保存 provider、提示词版本、图片 SHA-256、候选快照、模式、输入指纹、尝试次数、结构化输出、当前错误和最近错误历史。
- 输入指纹由图片、provider、提示词版本、模式和候选集合生成；相同输入不会重复排队或重复计费。
- 工作线程固定单并发，受单轮与每日限额控制。
- 工作线程默认在 AstrBot 启动后等待 30 秒再处理队列；一次可重试 provider 故障会结束本轮，避免 provider 尚未就绪时连续耗尽尝试次数。
- 插件重启时 `running` 记录恢复为 `pending`。
- provider 超时或异常有限重试；达到上限后保持 `failed`，原人工审核任务不变。
- 新图片是否自动入队由 `llm_image_review_auto_queue_new` 控制；历史待审图必须通过管理员命令受限加入。

## 管理命令

```text
.pp LLM审图状态
.pp LLM审图执行 3 Pixiv
.pp LLM审图执行 3 小红书
.pp LLM审图重试 10
```

所有子命令同时保留 `.pjsk图库 ...` 和省略 `pp` 的点号入口；不会响应没有唤醒前缀的普通文本。

## 放量方法

1. 在 `shadow` 下用人工已标注图片验证 provider 与 JSON 契约。
2. 至少评估 50 张，分别记录错误自动通过率、角色精确率、无法解析率和平均耗时。
3. 若模型建议可用但不够稳定，切到 `assist`，让 QQ 审核人参考但不自动落库。
4. 只有错误自动通过率足够低时才切 `auto_approve`，并保留随机抽检。
5. 更换 provider、提示词版本或候选生成规则后重新从 shadow 校准。

## 数据与回滚

升级只新增 `llm_image_review_runs` 表和配置项，不改写历史审核结果。关闭 `llm_image_review_enabled` 即停止工作线程；已经保存的影子结果可保留审计，也可在停机备份后单独清理，不影响原图、tag、来源和人工审核任务。
