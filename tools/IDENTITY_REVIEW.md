# 角色识别离线核实与评估

`chat_image_audit_profile_mode` 默认 `off`，保持原来的群聊识图提示词。`text` 才会加入 26 人文字特征档案；本阶段不默认启用，也不增加模型调用次数。档案基于官方角色页立绘的助手视觉观察，来源见 `core/data/pjsk_identity_profiles.json`，不等同于人工标准答案。

## 生成样本包

在能读取图片文件路径的环境运行（Docker 数据库里的路径应在容器内解析）：

```sh
python tools/export_identity_review.py --db /AstrBot/data/plugin_data/astrbot_plugin_pjsk_pic/image_index.db --output /AstrBot/data/temp/identity-review-new --limit 200
```

输出目录必须为空。工具只读数据库，不改标签，不调用模型；导出本地 JPEG 预览、manifest.json 和离线 review.html。样本包包含私人图片，请保存在本地，勿提交到源码仓库。来源标签和群聊更正只作未核实建议；覆盖数字也只是建议标签覆盖，不能作为真实角色覆盖率。

## 人工核实

1. 在浏览器打开 `review.html`，保持旁边的 `images` 文件夹完整；ZIP 必须先全部解压。
2. 填写核实人，逐张看图并勾选所有明确出现的角色。采用建议不代表核实完成。
3. 确定角色选项及完整性后，点击“确认这张标注”；难以确认的图片标记待定。
4. 可以分批处理。离开前“导出标注 JSON”，下次通过“继续已有标注”导入；浏览器草稿不是可靠备份。
5. 评估前人工检查开发/验收集的同源与相似图分组。有跨集近重复时，在 JSON 修正 group_id/split 后导入，确认无已知泄漏再勾选分组复核。

只对人工核实的完整标签评分，未核实样本始终排除。不将模型建议或自动测试数据变成人工标签。验收集用于最终比较，不用它反复修改提示词；小规模结果必须同时报告样本量、角色缺口和处理错误。

## 离线评分

评分器不负责调用模型。基线和新提示词分别输出一个 JSON 数组，以角色档案的 key 为角色标识：

```json
[
  {"sample_id":"sample-0001","status":"ok","character_keys":["miku"]},
  {"sample_id":"sample-0002","status":"error","character_keys":[]}
]
```

```sh
python tools/evaluate_identity_results.py --manifest labels.json --predictions baseline.json profiles.json --split holdout
```

每份预测必须覆盖指定集合的所有已核实样本。评分报告角色 precision/recall、整图完全匹配率、非目标误报率、处理错误和逐角色缺口；空分母返回 null。未核实分组、没有人工标签、预测缺失或跨集合泄漏会拒绝评分。只有人工核实后的对照结果支持改善，才考虑开启 `text`；本阶段没有准确率提升结论。
