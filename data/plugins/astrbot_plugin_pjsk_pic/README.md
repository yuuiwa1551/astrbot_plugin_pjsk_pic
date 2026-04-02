# astrbot_plugin_pjsk_pic

PJSK 图片图库插件，支持本地图库发图、用户投稿、多平台采集、Pixiv 自动按 tag 抓图、审核与独立 WebUI 管理。

## 1. 插件简介

当前目录是 **AstrBot 运行仓库**，插件本体位于：

- `data/plugins/astrbot_plugin_pjsk_pic`

插件运行数据默认位于：

- `data/plugin_data/astrbot_plugin_pjsk_pic/`

这个插件主要解决以下几类需求：

- 从本地图库按 tag / alias 随机发图
- 通过自然语言触发或 LLM Tool 触发发图
- 接收用户投稿，并接入通知 / 审核流程
- 从 Pixiv、X / Twitter、小红书、lofter / generic 页面采集图片
- 对 tag 做别名、主 tag、角色标记、清理与审核治理
- 通过独立 WebUI 查看图片、来源、审核任务与采集任务

当前重点支持的平台 / 来源：

- 本地图库
- Pixiv
- X / Twitter
- 小红书
- lofter / generic 兼容采集

## 2. 当前功能

### 发图能力

- 自然语言规则触发
- LLM Tool 发图
- tag / alias 匹配
- 模糊匹配可配置
- 会话级简单去重
- 同 sha256 多路径图片自动回退到可用文件

### 投稿能力

- 用户命令投稿
- 支持管理员通知
- 支持开启 / 关闭投稿审核
- 投稿时可自动补 tag / alias

### 图库管理 / tag 治理

- SQLite 索引
- 本地图库扫描
- tag / alias 管理
- tag 合并
- 主 tag 切换（canonical tag）
- 角色 tag 标记
- tag 清理预览 / 执行
- 图片删除 / 恢复

### 采集能力

- 采集任务队列
- 图片下载入库
- Pixiv / X / 小红书 / lofter / generic 适配
- include / exclude tag 过滤
- 主 tag 收口（仅保留 canonical tag）
- pHash 重复图识别
- 采集失败重试

### Pixiv 自动采集

- 按 tag 自动周期性搜索 Pixiv
- 支持仅订阅角色 tag
- 支持查询后缀（默认常见用法是给 tag 追加 `user`）
- 支持每轮任务上限、每 tag 结果上限、页数上限

### 审核能力

- 自动审核接入
- 人工审核命令
- 审核列表 / 查看 / 通过 / 拒绝

### WebUI

- 独立 WebUI 服务
- 图片搜索与预览
- 来源信息查看
- tag 管理
- 审核任务查看与处理
- 采集任务查看、新建、重试

## 3. 命令 / 触发方式

### 自然语言触发

支持类似：

- `看看初音未来`
- `来张miku`
- `发一张宁宁`
- `来点瑞希`

### 投稿与快捷命令

- `/投稿 <tag>` 或 `/tg <tag>`
  - 发送命令后附图，进入投稿流程
- `/alias <tag或alias> [alias1,alias2]`
- `/unalias <tag或alias> <alias1,alias2>`

### 图库管理命令

图库管理命令组同时支持：

- `/pjsk图库 ...`
- `/pp ...`

常用命令包括：

- `/pp 重扫`
- `/pp 统计`
- `/pp 查看 <tag>`
- `/pp 看图 <image_id>`
- `/pp 别名添加 <tag> <alias>`
- `/pp 别名删除 <tag> <alias>`
- `/pp 别名查看 <tag>`
- `/pp tag列表`
- `/pp tag合并 <from_tag> <to_tag>`
- `/pp 主tag切换 <old_tag> <new_tag>`
- `/pp 角色标记 <tag> <true|false>`
- `/pp tag清理预览`
- `/pp tag清理执行`
- `/pp 采集添加 <platform> <url> [tags_csv]`
- `/pp 采集列表`
- `/pp 采集重试 <job_id>`
- `/pp 自动采集状态`
- `/pp 自动采集列表`
- `/pp 自动采集执行`
- `/pp 审核列表 [status]`
- `/pp 审核查看 <review_id>`
- `/pp 审核通过 <review_id>`
- `/pp 审核拒绝 <review_id>`
- `/pp 投稿审核状态`
- `/pp 投稿审核开启`
- `/pp 投稿审核关闭`
- `/pp 删图 <image_id>`
- `/pp 恢复图 <image_id>`
- `/pp 面板地址`

## 4. 配置说明

主要配置项如下：

### 基础图库

- `library_root`
  - 本地图库根目录；留空时默认使用插件数据目录下的 `library/`
- `scan_on_startup`
  - AstrBot 启动后是否自动扫描本地图库
- `allow_fuzzy_match`
  - 是否允许 tag / alias 模糊匹配
- `recent_dedupe_count`
  - 每个会话最近去重的图片数量
- `enable_llm_tool`
  - 是否开启 LLM 发图工具

### WebUI

- `webui_enabled`
  - 是否启用独立 WebUI
- `webui_host`
  - WebUI 监听地址，默认 `0.0.0.0`
- `webui_port`
  - WebUI 监听端口，默认 `9099`
- `webui_access_token`
  - 可选访问令牌

### 投稿 / 审核

- `submission_notify_enabled`
  - 是否启用投稿通知
- `submission_notify_use_astr_admins`
  - 是否将 AstrBot 管理员作为默认通知目标
- `submission_notify_targets`
  - 额外投稿通知目标列表
- `submission_review_enabled`
  - 是否开启投稿审核
- `enable_auto_review`
  - 是否启用自动审核
- `review_provider_id`
  - 自动审核使用的 provider id
- `review_confidence_threshold`
  - 自动审核置信度阈值
- `approve_non_character_tags`
  - 非角色 tag 是否自动放行
- `guess_character_tags`
  - 是否自动猜测角色 tag

### 采集

- `crawler_max_candidates`
  - 每个采集任务最多处理的候选图片数
- `platform_request_timeout`
  - 平台请求超时秒数
- `platform_retry_times`
  - 平台采集失败后的自动重试次数
- `max_tags_per_image`
  - 每张图最多写入的 tag 数量
- `tag_blacklist`
  - 额外 tag 黑名单
- `crawl_include_tags`
  - 强制包含的 tag 列表
- `crawl_exclude_tags`
  - 强制排除的 tag 列表，默认常见会排除 `R-18,AI`
- `crawl_keep_primary_tags_only`
  - 是否将采集 tag 收口到主 tag / canonical tag
- `enable_phash_dedupe`
  - 是否启用 pHash 去重
- `phash_max_distance`
  - pHash 判重最大距离

### Pixiv 自动采集

- `pixiv_refresh_token`
  - Pixiv App API 所需 refresh token
- `pixiv_auto_crawl_enabled`
  - 是否开启 Pixiv 自动采集
- `pixiv_auto_crawl_character_only`
  - 是否仅订阅角色 tag
- `pixiv_auto_crawl_interval_minutes`
  - 自动采集周期（分钟）
- `pixiv_auto_crawl_query_suffix`
  - 搜索后缀，默认可用于追加 `user`
- `pixiv_auto_crawl_max_results_per_tag`
  - 每个 tag 最多拉取多少搜索结果
- `pixiv_auto_crawl_max_pages_per_tag`
  - 每个 tag 最多拉取多少页
- `pixiv_auto_crawl_max_new_jobs_per_cycle`
  - 每轮最多新增多少自动采集任务

### 平台接入

- `x_cookie_string`
  - X / Twitter 登录 Cookie
- `x_account_pool_enabled`
  - 是否启用 X 账号池模式
- `xiaohongshu_cookie_string`
  - 小红书登录 Cookie

完整配置见：

- `_conf_schema.json`

## 5. WebUI 说明

插件启动后会拉起 **独立 WebUI 服务**。

默认配置：

- 监听地址：`0.0.0.0`
- 监听端口：`9099`

若仅希望本机访问，可设置：

- `webui_host=127.0.0.1`

若配置了 `webui_access_token`，可通过以下任一方式访问：

- URL 查询参数：`?token=你的令牌`
- 请求头：`X-PJSK-Token: 你的令牌`
- 请求头：`Authorization: Bearer 你的令牌`

管理员可通过命令查看当前访问地址：

- `/pp 面板地址`

## 6. 数据位置

插件运行数据目录：

- `data/plugin_data/astrbot_plugin_pjsk_pic/`

常见内容包括：

- `image_index.db`
  - SQLite 图片索引数据库
- `library/`
  - 本地图库目录
- `images/`
  - 导入 / 采集图片目录
- `trash/`
  - 删除后暂存目录

数据库会同时维护：

- 逻辑图片记录
- 物理文件位置记录
- tag / alias / 主 tag 关系
- 审核任务
- 采集任务与自动采集订阅

## 7. TODO / 后续计划

后续优先事项：

1. 继续补齐 WebUI 上的管理入口，减少必须走命令行的操作
2. 继续清理历史遗留乱码文案，统一插件内中文文案质量
3. 为采集适配器补更多测试样本与失败场景回归
4. 优化自动采集与审核堆积时的治理策略
5. 补充更完整的发布说明与升级迁移说明

## 8. 当前版本

- 当前插件版本：`0.10.0`

## 9. 更新记录

### v0.10.0

- 主 tag / canonical tag 治理完善
- 新增 tag 合并、主 tag 切换、tag 清理相关能力
- 强化抓图后的 tag 收口与图库整理

### v0.9.8

- 优化去重逻辑
- 调整免 token 访问场景下的 WebUI 体验

### v0.9.7

- 开放 WebUI 使用
- 补充审核图片预览能力

### v0.9.5

- 修复 Docker 代理环境下 Pixiv 自动采集问题

### v0.9.0

- 新增 Pixiv 自动按 tag 采集
- 整理自动采集相关仓库结构

### v0.5.7

- 修复 pjsk WebUI 端口冲突问题

### v0.5.6

- 增强投稿审核命令

### v0.5.5

- 新增投稿管理员通知

### v0.5.4

- 修复 `/tg` 投稿触发

### v0.5.3

- 优化投稿自动建 tag 与别名

### v0.5.2

- 调整未命中时的静默行为
- 完成一轮回归收口

### v0.5.1

- 新增用户投稿能力

### v0.5.0

- 独立 WebUI 改造
- 新增面板地址命令
- 使 WebUI 与 AstrBot Dashboard 解耦
