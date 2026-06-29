# astrbot_plugin_pjsk_pic

PJSK 图片图库插件，支持本地图库发图、用户投稿、多平台采集、Pixiv 自动按 tag 抓图、审核与独立 WebUI 管理。

## 安装方式

- 插件仓库：`https://github.com/yuuiwa1551/astrbot_plugin_pjsk_pic`
- 作为独立插件仓使用时，**仓库根目录就是插件目录**
- 放入 AstrBot 运行仓后，通常路径为：`data/plugins/astrbot_plugin_pjsk_pic`
- 依赖声明位于：`requirements.txt`

## 1. 插件简介

这是 `astrbot_plugin_pjsk_pic` 的插件源码目录。

常见使用方式有两种：

- 直接作为独立插件仓安装
- 作为 AstrBot 运行仓中的一个插件目录使用

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
- Pixiv 采集过滤可识别主 tag、alias、平台词和内置常用查询词
- 主 tag 收口（仅保留 canonical tag）
- pHash 重复图识别
- 采集失败重试

### Pixiv 自动采集

- 按 tag 自动周期性搜索 Pixiv
- 支持 Pixiv 平台专用 query / match 词映射
- 支持仅订阅角色 tag
- 支持查询后缀（默认常见用法是给 tag 追加 `user`）
- 支持每轮任务上限、每 tag 结果上限、页数上限
- 当前自动采集是“最新增量采集”，不是一次性把某个 Pixiv tag 的历史结果全量扒完
- 支持手动 Pixiv 历史回填任务：指定 tag、页数、扫描上限、入队上限后逐页补旧图
- 自动采集只使用显式 Pixiv 平台词或内置官方 Pixiv 词映射，不再默认用普通 alias 搜图，避免短英文 alias 误命中其他作品

### 审核能力

- 自动审核接入
- 人工审核命令
- 审核列表 / 查看 / 通过 / 拒绝
- 按图片聚合的 Pixiv 人工审核
- Pixiv 图片批量人工审核
- 人工审核结果可沉淀 Pixiv tag 映射

### WebUI

- 独立 WebUI 服务
- 图片搜索与预览
- 来源信息查看
- tag 管理
- 审核任务查看与处理
- 采集任务查看、新建、重试
- Pixiv 审批页
- Pixiv 大图预览审核
- Pixiv 审批页支持按角色、alias、Pixiv tag 搜索相关待审图
- Pixiv 原始 tag / 翻译 tag 点选确认
- Pixiv 审批页可直接新增主 tag，并自动加入当前图片选择
- Pixiv 审批通过时只归入一个规范主 tag，已选同义词会沉淀为 alias / Pixiv 搜索词
- Pixiv 审批默认仅显示 `pending / uncertain` 待处理队列，可手动切到 `rejected`
- Pixiv 审批页支持拒绝整张图片，被拒绝 Pixiv 来源会在后续自动来源搜图中跳过
- Pixiv 平台词管理页
- Pixiv 历史建议词 / 未解决词查看与一键采纳
- Pixiv 批量审核预览 / 批量确认映射
- 历史 tag 归并助手

## 3. 命令 / 触发方式

### 自然语言触发

支持类似：

- `看看初音未来`
- `来张miku`
- `发一张宁宁`
- `来点瑞希`
- `看看id123`
- `看看 ID 123`

### 投稿与快捷命令

- `/投稿 <tag>` 或 `/tg <tag>`
  - 发送命令后附图，进入投稿流程
  - 可写成 `/投稿 <tag> 别名 <alias1,alias2>` 或 `/tg <tag> alias <alias1,alias2>`，投稿时顺手补充别名
  - 也可以先回复一条带图消息，再发送投稿命令
- `/alias <tag或alias> [alias1,alias2]`
- `/unalias <tag或alias> <alias1,alias2>`

### 图库管理命令

图库管理命令组同时支持：

- `/pjsk图库 ...`
- `/pp ...`

常用命令包括：

- `/pp 帮助 [投稿|tag|审核|采集]`
- `/pp 重扫`
- `/pp 统计`
- `/pp 查看 <tag>`
- `/pp 看图 <image_id>`
- `/pp 别名添加 <tag> <alias>`
- `/pp 别名删除 <tag> <alias>`
- `/pp 别名查看 <tag>`
- `/pp tag列表`
- `/pp tag合并 <目标tag> <来源tag1,来源tag2>`
- `/pp 主tag切换 <old_tag> <new_tag>`
- `/pp 角色标记 <tag> <true|false>`
- `/pp tag清理预览`
- `/pp tag清理执行`
- `/pp 采集添加 <platform> <url> [tags_csv]`
- `/pp 采集列表`
- `/pp 采集诊断`
- `/pp 失败列表 [platform]`
- `/pp 失败重试 <job_id|全部>`
- `/pp 采集重试 <job_id>`
- `/pp 自动采集状态`
- `/pp 自动采集列表`
- `/pp 自动采集执行`
- `/pp 历史回填添加 <tag> [页数上限] [扫描上限] [入队上限]`
- `/pp 历史回填列表`
- `/pp 审核列表 [status]`
- `/pp 审核查看 <review_id>`
- `/pp 审核通过 <review_id>`
- `/pp 审核拒绝 <review_id>`
- `/pp 投稿审核状态`
- `/pp 投稿审核开启`
- `/pp 投稿审核关闭`
- `/pp 删图 <image_id>`
- `/pp 恢复图 <image_id>`
- `/pp 重复忽略 <id1> <id2> [原因]`
- `/pp 重复恢复 <id1> <id2>`
- `/pp 重复忽略列表 [image_id]`
- `/pp 面板地址`

数据库图片 ID 可使用 `/pp 看图 <image_id>`，也可自然语言发送 `看看id<image_id>` 查看。纯数字写法如 `看看1` 不再代表最近展示列表序号。

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
- `image_id_lookup_enabled`
  - 是否启用 `看看id<image_id>` 图片 ID 查看入口
- `image_id_lookup_admin_only`
  - `看看id<image_id>` 是否仅管理员可用，默认开启
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
- `/pp 采集诊断`
  - 查看采集 worker、Pixiv 自动采集、refresh token 配置、任务状态计数、最近失败原因和历史回填队列状态
- `/pp 失败列表 [platform]`
  - 查看最近失败采集任务，可按平台筛选
- `/pp 失败重试 <job_id|全部>`
  - 重新入队指定失败任务，或批量重试最近失败任务
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
  - 每个 tag 单轮最多拉取多少搜索结果；这是增量采集上限，不是全量回填
- `pixiv_auto_crawl_max_pages_per_tag`
  - 每个 tag 单轮最多拉取多少页
- `pixiv_auto_crawl_max_new_jobs_per_cycle`
  - 每轮全局最多新增多少自动采集任务
- `pixiv_backfill_default_pages`
  - Pixiv 历史回填任务默认页数上限
- `pixiv_backfill_default_max_results`
  - Pixiv 历史回填任务默认扫描结果上限
- `pixiv_backfill_default_max_new_jobs`
  - Pixiv 历史回填任务默认新增采集任务上限

自动采集按周期从 Pixiv 最新搜索结果里增量补任务，会记录上次看到的作品并在后续轮次遇到它时停止继续向旧结果翻页。已入库、已拒绝、未命中目标 tag / match 词的作品也会被跳过。需要把某个 tag 的全部历史作品补齐时，应单独做“历史回填”任务，而不是调大日常自动采集配置。

### Pixiv 历史回填

历史回填是手动启动的独立任务，用来补某个 tag 的旧 Pixiv 搜索结果。它会先把输入词解析到主 tag，再优先使用该 tag 的 Pixiv `query` / `both` 平台词或内置官方 Pixiv 词映射逐页搜索；命中后创建普通 Pixiv 采集任务，下载、入库和后续审批仍由现有采集队列处理。采集队列会按候选图逐张导入并写入审核任务，WebUI 的采集页会低频自动刷新，Pixiv 审批页保留手动刷新以避免大量待审图时反复重查。

每个历史回填任务都有明确边界：

- `页数上限`：最多翻多少页 Pixiv 搜索结果
- `扫描上限`：最多检查多少个搜索结果
- `入队上限`：最多新增多少个采集任务

任务会跳过已入库、已拒绝、已存在采集任务、未命中目标 tag / match 词和重复搜索结果。WebUI 的“采集任务”页可查看回填任务进度，也可对失败任务重新入队。

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

从 `v0.14.0` 起，WebUI 前端使用 `Vue 3 + Vite + TypeScript` 维护；插件仓库已包含构建产物，正常安装与运行不需要 Node/npm。

默认配置：

- 监听地址：`0.0.0.0`
- 监听端口：`9099`

若仅希望本机访问，可设置：

- `webui_host=127.0.0.1`

若配置了 `webui_access_token`，可通过以下任一方式访问：

- 浏览器访问根页面后，在登录页输入访问令牌建立站点会话（HttpOnly Cookie）
- 请求头：`X-PJSK-Token: 你的令牌`
- 请求头：`Authorization: Bearer 你的令牌`

> 从 `v0.12.2` 起，不再支持通过 `?token=` URL 查询参数传递 WebUI 访问令牌，以避免令牌进入浏览器历史、日志或 Referrer。

管理员可通过命令查看当前访问地址：

- `/pp 面板地址`

当前 WebUI 已支持：

- 按图片聚合查看 Pixiv 待审图
- 在 Pixiv 审批页先按角色 / alias / Pixiv tag 筛出相关待审图，再进行单张或批量审核
- 搜索命中主 tag 后会展开该 tag 的 alias、Pixiv 平台词和历史来源中的同义角色候选
- 在卡片页直接勾选主 tag 与 Pixiv 来源词后提交人工审核
- 点击图片进入大图预览，并在预览页继续完成同一套审核操作
- 在 Pixiv 审批页直接新增主 tag，新增后会自动加入当前图片选择
- 审核通过时只把图片归入一个规范主 tag，并把已选 Pixiv 来源 tag / 同义候选词沉淀为 alias / Pixiv 搜索词
- 如果同义候选词已经是独立 tag，会归并到规范主 tag，避免同一角色多 tag 分裂
- 后续自动来源搜图会使用规范主 tag 下的 Pixiv 平台词或内置官方 Pixiv 词映射进行搜索和匹配；普通 alias 仍可用于聊天触发和人工搜索，但不再默认参与自动 Pixiv 搜图
- 直接拒绝整张 Pixiv 图片，并记录该 Pixiv 来源为后续自动搜图跳过项
- 勾选多张 Pixiv 图片后批量预览 / 批量确认人工审核
- 单张或批量审核成功后，图片会立即从当前审批队列移除，并以非阻塞提示反馈结果
- 查看候选主 tag 的 alias、已有 Pixiv 平台词与历史建议词
- 按主 tag / Pixiv 词查看已有平台映射
- 手动新增、删除、修改 `query / match / both` 平台词
- 查看历史建议词、未解决词，并直接采纳为 Pixiv 平台词
- 批量确认多个 Pixiv 来源词映射到指定主 tag
- 查看历史 tag 归并候选、影响预览，并在 WebUI 内直接执行归并

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
- 人工确认过的非重复图片对

## 7. TODO / 后续计划

后续优先事项：

1. 把 Pixiv 平台词治理能力继续推广到 X / 小红书等平台
2. 为历史 tag 归并补更多候选来源与更强的冲突检查
3. 继续清理历史遗留乱码文案，统一插件内中文文案质量
4. 为采集适配器补更多测试样本与失败场景回归
5. 持续优化自动采集与审核堆积时的治理策略
6. 补充更完整的使用说明与升级迁移说明

## 8. 开源协议

本项目采用 `AGPL-3.0-or-later` 许可。

许可仅覆盖本插件的源代码与文档，不授予 Project Sekai 相关素材、Pixiv 作品、用户投稿图片或其他第三方内容的使用权。

详见仓库根目录的 `LICENSE` 文件。

## 9. 当前版本

- 当前插件版本：`0.14.13`

## 10. 更新记录

### v0.14.13

- 新增 `/pp 帮助`，支持投稿、tag、审核、采集等分组帮助，便于 QQ 内快速查看常用命令
- README 修正 `/pp tag合并` 参数方向，统一为目标 tag 在前、来源 tag 在后
- README 补充投稿时顺手添加 alias 的写法

### v0.14.12

- Pixiv 审核页补充查询索引，并将列表改为轻量加载、预览再加载来源词，降低大量待审图片下的加载压力
- Pixiv 自动采集不再默认使用普通 alias 搜索，改为显式 Pixiv 平台词和内置官方 Pixiv 词映射
- 自动采集与历史回填取消 substring 归属判断，避免 `rin` 命中 `Durin` 等误抓
- 新增 `/pp 重复忽略 <id1> <id2> [原因]`，人工确认两张图不是重复后可过滤后续疑似重复提示
- 新增 `/pp 重复恢复` 与 `/pp 重复忽略列表` 便于恢复和查看重复忽略记录

### v0.14.11

- 移除 `看看<序号>` / `/pp 看看 <序号>` 最近展示列表序号自查功能
- 发图结果和审核列表不再写入或提示 `自查：看看N`
- `看看id<image_id>` 图片 ID 查看入口改用独立配置项 `image_id_lookup_enabled` / `image_id_lookup_admin_only`

### v0.14.10

- 新增 `看看id<image_id>` / `看看 ID <image_id>` 自然语言图片 ID 查看入口
- `看看id123` 会优先按数据库图片 ID 查图，不再被当成 `id123` tag 静默吞掉

### v0.14.9

- 补充 `AGPL-3.0-or-later` 开源许可声明与 `LICENSE` 文件
- README 明确许可仅覆盖插件代码与文档，不覆盖第三方素材、Pixiv 作品或用户投稿图片

### v0.14.8

- 修复自然语言发图如 `看看初音未来` 可能先被群聊增强插件触发 LLM 回复、再由图库发图的问题
- 普通发图结果不再无条件展示 `自查：看看1`，仅在当前发送者有自查权限时提示

### v0.14.7

- 新增 `/pp 采集诊断`，集中查看采集 worker、Pixiv 自动采集、任务状态、最近失败和历史回填队列
- 新增 `/pp 失败列表 [platform]` 与 `/pp 失败重试 <job_id|全部>`，便于直接处理失败采集任务
- 新增 `看看<序号>` / `/pp 看看 <序号>` 管理员自查入口，可按当前会话最近展示图片序号查看图片详情

### v0.14.6

- Pixiv 采集 include / exclude 过滤支持展开主 tag、alias、平台词和内置常用 Pixiv 查询词
- Pixiv 历史回填创建采集任务时会把实际 Pixiv 查询词纳入 include 规则，减少中文主 tag 与 Pixiv 日文 tag 不一致导致的误跳过
- 采集任务页和 Pixiv 审批页新增静默自动刷新，新入审图片可更快进入审批列表

### v0.14.5

- 新增 Pixiv 历史回填任务，支持按 tag 逐页扫描旧搜索结果并创建普通采集任务
- 采集页新增历史回填面板，可设置页数、扫描结果和新增任务上限，并查看回填任务统计
- 新增 `/pp 历史回填添加` 和 `/pp 历史回填列表` 管理命令
- 后端新增历史回填任务表与后台 worker，避免污染日常自动采集的 `last_seen` 增量状态

### v0.14.4

- tag 身份候选的角色名重合规则改为约 40% CJK 字符重合即可进入待确认候选
- tag 归并页的历史候选默认收起，可按需显示并可一键取消显示
- 配置与 README 明确 Pixiv 自动采集是最新增量采集，不是全量历史回填

### v0.14.3

- tag 归并页新增“待手动确认”候选池，可扫描疑似同一角色 tag 并保留 Pixiv 证据和 LLM 复核结论
- 新增 tag 身份候选接口，候选只用于人工确认，不会自动执行归并
- 采集页 Pixiv 空 URL 改为按输入 tag 搜索预览，勾选作品后再批量创建真实采集任务
- Pixiv 平台词类型统一为 `query` / `match` / `both`：`query` 用于搜图，`match` 用于归属判断

### v0.14.2

- WebUI 状态枚举改为中文展示，图片检索默认显示已通过图片
- 图片检索和 Pixiv 审批增加页码、跳页和每页数量切换
- 移除重复的“审核任务”前端页面，Pixiv 图片审核统一在 Pixiv 审批页完成
- Pixiv 审批卡片和预览弹窗改为完整图显示，tag 多时在局部区域内滚动

### v0.14.1

- WebUI 访问入口改为稳定地址，不再需要 `?v=版本号` 查询参数
- 前端启动时会自动清理历史缓存参数 `?v=...`，旧链接仍可打开并跳转到稳定地址
- 侧边栏不再硬编码 WebUI 版本号，避免日常入口和版本发布节奏绑定

### v0.14.0

- 独立 WebUI 从 `core/webui.py` 内嵌 HTML/CSS/vanilla JS 重构为 Vue 3 + Vite + TypeScript 单页应用
- WebUI 采用工作台式布局，统一导航、筛选区、列表、预览弹窗、toast、loading 和状态反馈
- 保留现有 aiohttp `/api/*` 协议，插件普通使用者仍只需要 Python 运行依赖
- 新增 `webui/` 前端源码工程，构建产物随插件发布到 `core/webui_static/`

### v0.13.1

- Pixiv 审批页新增角色 / alias / Pixiv tag 搜索，输入 `mzk` 等 alias 可筛出对应角色相关待审图
- 搜索命中主 tag 后会展开 alias、Pixiv 平台词和历史来源中的同义角色候选，便于集中处理同一角色
- Pixiv 审批页交互改为先筛选、再选图、再批量审核，批量审核区未选图时默认收起
- `/api/pixiv-review-images` 新增 `keyword` 参数，并返回 `search_context` 解释命中来源

### v0.13.0

- Pixiv 审批页候选 tag 改为“归入主 tag”单选，避免同一图片被打上多个同义主 tag
- Pixiv 来源 tag 改为 alias / Pixiv 搜索词多选，审核通过时沉淀到规范主 tag
- 审核提交兼容历史多主 tag 输入，第一个作为规范主 tag，其余作为同义词归并或沉淀
- 同义词已经是独立 tag 时会归并到规范主 tag，并迁移图片关系、审核任务、alias、平台词和自动搜图订阅

### v0.12.9

- Pixiv 审批页单张“拒绝图片”改为直接执行，不再弹出浏览器确认框

### v0.12.8

- 修复 Pixiv 大图预览里新增主 tag 时读到背后卡片空输入框的问题
- 新增主 tag 表单现在按卡片区和预览区分别定位，避免同图多处渲染时 id 冲突

### v0.12.7

- Pixiv 审批页候选主 tag 区新增手动添加入口，新增或复用主 tag 后会自动选中
- Pixiv 审批通过时继续沿用已选来源 tag 沉淀平台词映射，便于后续来源搜图使用 Pixiv 原始词
- Pixiv 审批页新增“拒绝图片”动作，可将整张 Pixiv 图标记为不收并立即移出当前队列
- 新增 Pixiv 来源拒绝记录，自动来源搜图和手动采集会跳过已拒绝来源

### v0.12.6

- Pixiv 审批页默认只显示 `pending / uncertain` 待处理队列，`rejected` 需要在筛选器中显式查看
- Pixiv 审批列表接口按当前筛选构造卡片内的“当前审核项”，避免历史 rejected 任务混入默认队列造成误判
- 单张 / 批量审核成功后，图片会立即从当前审批队列移除，并改用非阻塞提示展示结果
- 刷新审批列表时会清理已不存在图片的勾选状态，避免批量选择计数残留

### v0.12.5

- Pixiv 采集将 `x_restrict` / `illust_ai_type` 等限制级与 AI 元数据补成过滤 tag，避免 `R-18`、`AI生成` 未出现在普通 tag 列表时漏过
- 默认采集排除项扩展为 `R-18,R-18G,AI,AI生成,AI-generated`
- WebUI 改为功能导航页，初次打开只加载当前页数据，图库、审核、采集、tag、Pixiv 审批、平台词和 tag 归并分开查看

### v0.12.4

- 修复图片检索卡片未接入大图预览入口的问题，缩略图和“预览”按钮现在会打开通用大图预览
- 图片检索卡片与通用大图预览补齐待审批任务的“通过 / 拒绝”操作

### v0.12.3

- 修复 WebUI 批量输入解析正则在运行时被换行转义破坏，导致管理台脚本无法执行的问题

### v0.12.2

- 修复图片级人工审核在同一 `(image_id, tag_id)` 存在多来源记录时，`reject_unselected` 可能误伤非当前来源 tag 的问题
- 修复手动审核的事务边界，将任务读取、任务更新与图片 tag 状态更新收口到单次数据库事务中
- 调整独立 WebUI 访问控制：移除 `?token=` URL 访问方案，改为登录页 + HttpOnly Cookie 会话，同时保留 `X-PJSK-Token` / `Authorization: Bearer`
- 修复 `datetime.utcnow()` 兼容性告警，统一改为带时区 UTC 时间
- 修复插件配置页 `_conf_schema.json` 中文说明乱码
- 移除仓库内仅面向插件目录整理的说明，收口为通用仓库描述

### v0.12.1

- 修正 `metadata.yaml` 的作者与仓库信息
- 新增 `requirements.txt` 与 `logo.png`
- README 改为兼容独立插件仓 / AstrBot 运行仓两种阅读场景
- 插件仓将同步整理为“仓库根目录即插件根目录”的结构

### v0.12.0

- Pixiv 审批页新增图片多选、批量审核预览、批量确认审核能力
- Pixiv 平台词管理页新增批量确认映射预览 / 提交能力，可对未解决词批量挂到指定主 tag
- 新增历史 tag 归并助手，支持候选归并对、影响预览与直接执行归并
- tag 归并流程补齐 `image_tags / review_tasks / tag_aliases / platform_tag_terms / crawl_subscriptions` 同步迁移

### v0.11.1

- 新增 Pixiv 平台词独立管理页，支持查看 / 搜索 / 新增 / 删除 / 修改 Pixiv query / match / both 映射
- 支持按主 tag 查看历史建议词，并可从建议词直接采纳到当前主 tag
- 支持查看未解决的 Pixiv 来源词，并从候选主 tag 直接一键沉淀平台词
- WebUI 与平台词 API 已联动 Pixiv 审批页，人工审核后可即时刷新映射治理结果

### v0.11.0

- 新增 Pixiv Web 审批页，支持按图片聚合查看待审图
- 支持在卡片页 / 预览页直接点选主 tag 与 Pixiv 来源词后提交人工审核
- 新增 Pixiv 平台 tag 映射层，并允许从人工审核与历史 Pixiv tag 中沉淀 query / match 词
- Pixiv 自动采集改为优先使用平台映射词查询与匹配

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
