# PJSK 图片库 - 插件市场提交草稿

## 提交入口

- 插件市场：<https://plugins.astrbot.app>
- GitHub Issue 模板：<https://github.com/AstrBotDevs/AstrBot/issues/new?template=PLUGIN_PUBLISH.yml>

## 建议提交信息

```json
{
  "name": "astrbot_plugin_pjsk_pic",
  "display_name": "PJSK 图片库",
  "desc": "支持本地图库发图、多平台采集、Pixiv 自动按 tag 抓图、自动审核与 WebUI 管理的 PJSK 图片库插件",
  "author": "yuuiwa1551",
  "repo": "https://github.com/yuuiwa1551/astrbot_plugin_pjsk_pic",
  "tags": ["pjsk", "图库", "pixiv", "图片管理"],
  "social_link": "https://github.com/yuuiwa1551"
}
```

## 提交前自检

- 仓库根目录就是插件根目录
- `metadata.yaml` 中 `name / display_name / version / author / repo` 已更新
- 已补 `requirements.txt`
- 已补 `logo.png`
- `README.md` 已改为独立插件仓可直接阅读的说明
