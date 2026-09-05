# 小红书采集提供者契约

## v0.21.0 分页入口

三期增加 `xiaohongshu_cli` 类型的独立 REST provider，使用 `xiaohongshu-cli 0.6.4`。显式设置 provider 类型、地址和 token 后，增量与回填使用同一个实例。构建与部署见 [sidecar 说明](../provider/xhs_cli_sidecar/README.md)。下面 v2.5.0 部署记录属于旧 MCP provider，保留供回滚使用。

分页搜索在原请求上增加 `page`、`page_size`，返回 `data.page`、`data.pageSize` 和 `data.hasMore`。新 provider 传递 `publish_time`，增量为一周内，回填不限。`tagList` 与正文话题合并后用于匹配。旧 MCP 不支持第二页，插件会明确报告。

本期只增加分页与回填业务，不扩展自动重试或 provider 自动切换。回填请求遵守现有暂停状态；失败可用 QQ 命令从保存的页内位置继续。迁移新增 `xhs_backfill_tasks`、`xhs_backfill_items` 和查询词饱和字段。

## 选型与边界

- 活动提供者固定为 `xpzouying/xiaohongshu-mcp:v2.5.0`，许可证为 Apache-2.0。
- 线上通过本地 REST 调用，不把 MCP 对话或 LLM 放进采集链路。
- 提供者独立持有浏览器登录态；插件不读取、复制或输出小红书 Cookie。
- 二期只做“最新 + 图文 + 一周内”的首批增量采集，不翻页、不做历史回填。
- 不绕过验证码、风控或平台限制，不执行点赞、评论、收藏、发布等互动操作。
- 官方开放平台当前不能提供公开笔记搜索/详情，因此本接入属于受限的自托管浏览器提供者，应固定版本并低频使用。

## 已验证版本

- Docker tag：`xpzouying/xiaohongshu-mcp:v2.5.0`
- 镜像摘要：`sha256:88e2603f324f567e0a254ed7a1e24d632a16eccc30e84ef3fb887e34a03d0fe3`
- 验证日期：2026-08-30

升级提供者前必须重新执行本文的隔离 smoke，不能直接跟随 `latest`。

## REST 契约

### 健康检查

```http
GET /health
```

要求 `success=true`，且 `data.status=healthy`。

### 登录状态

```http
GET /api/v1/login/status
Authorization: Bearer <token>
```

要求 `success=true`，且 `data.is_logged_in` 为布尔值。未登录时自动采集必须暂停，不得把它当成空结果。

### 搜索

```http
POST /api/v1/feeds/search
Authorization: Bearer <token>
Content-Type: application/json

{
  "keyword": "初音未来",
  "filters": {
    "sort_by": "最新",
    "note_type": "图文",
    "publish_time": "一周内"
  }
}
```

实测不能额外显式发送 `search_scope=不限` 或 `location=不限`；v2.5.0 会尝试点击本可省略的默认项，并可能返回 `SEARCH_FEEDS_FAILED`。

插件只接纳：

- `modelType=note`
- `noteCard.type=normal`
- 同时具有 `id` 和 `xsecToken`

搜索响应会被规范为 `XhsSearchHit`，插件其他模块不依赖提供者原始字段。

### 详情

```http
POST /api/v1/feeds/detail
Authorization: Bearer <token>
Content-Type: application/json

{
  "feed_id": "<note_id>",
  "xsec_token": "<search context>",
  "load_all_comments": false
}
```

要求 `data.data.note` 存在、`noteId` 与请求一致、类型为 `normal`，并且 `imageList` 是数组。插件读取标题、正文、作者、发布时间、话题和完整图片列表；评论不参与采集。

`xsecToken` 只保存在采集发现/任务的 `source_context_json`，规范来源 URL 不携带它，插件命令和插件自身日志也不显示它。注意第三方提供者 v2.5.0 的容器日志会打印正在打开的详情 URL，其中可能包含临时 `xsec_token`；因此提供者日志也要按敏感运行数据管理，不能粘贴到群聊、Issue 或公开日志平台。

## 图片安全规则

- 只允许已知小红书 CDN：`*.xhscdn.com` 和 `ci.xiaohongshu.com`。
- 提供者返回的已知 CDN `http` URL 会升级为 `https`，其他 scheme 或域名直接失败并暂停检查。
- 下载必须带笔记 `Referer`。
- 小红书详情不受通用 `crawler_max_candidates=6` 限制，必须保留全部图片及顺序。
- 单篇默认安全上限为 60 张；超过上限必须整项失败并暂停检查，不能静默截断。
- 单张远程图片默认最多下载 25 MiB；同时检查 `Content-Length` 和实际读取长度，不能靠缺失或伪造长度绕过。
- 图片响应必须是 `image/*` MIME，且可由 Pillow 完整解码后才能写入图库。

## tag 准入规则

- 自动搜索只使用显式 `platform_tag_terms(platform=xiaohongshu)`。
- `query` / `both` 用于搜索；`match` / `both` 用于详情标题、正文和话题复核。
- 只有 query 与 match 两侧都已配置的主 tag 才会启用订阅。
- 普通 alias 和主 tag 名本身不会被隐式拿去搜；如果希望使用同名搜索词，也必须显式保存小红书平台词。
- 小红书原始话题只作为来源元数据和已存在 tag 的匹配证据，不自动创建 canonical tag。

## 错误分类

| 分类 | 是否重试 | 是否暂停 |
| --- | --- | --- |
| `authentication` | 否 | 是 |
| `verification` | 否 | 是 |
| `risk_control`（含 `300012`） | 否 | 是 |
| `rate_limit` | 否 | 是 |
| `contract` / `upstream_contract` | 否 | 是 |
| `unsafe_image_url` / `response_too_large` | 否 | 是 |
| `timeout` / `transport` / 可重试 5xx | 由任务层有限重试 | 否 |
| `unsupported_note` / `empty_note` | 否 | 否，跳过单笔记 |

暂停状态保存在数据库中，插件重载后不会自动绕过。管理员确认问题已处理后，使用 `.pp 小红书采集恢复`。

`/health` 只能证明 HTTP 服务存活，不能证明浏览器页面自动化没有卡住。实测出现过健康检查继续成功、单次搜索长时间不返回的情况。插件会在请求超时后停止本轮剩余订阅、记录提供者错误，避免同一轮继续堆积搜索；管理员仍应结合一次受限搜索 smoke 和容器日志判断是否需要重启提供者。

## 2026-08-30 隔离 POC 结果

- 提供者仅绑定本机回环地址，健康检查和登录状态正常。
- 同一搜索低频执行两次，每次返回 20 条图文笔记，前 5 条 ID 顺序一致。
- 抽查 3 篇详情，图片数分别为 1、14、1；14 图笔记完整返回，未受 6 图上限影响。
- 从 3 篇中抽取 10 张，经 HTTPS 下载均为 HTTP 200、`image/webp`，Pillow 解码 10/10 成功。
- 主动停止提供者后，健康检查明确连接失败；重启后恢复健康且登录态仍有效。
- 另一次搜索曾在健康接口正常时卡住；重启隔离容器后登录态保留，搜索恢复。该结果已纳入单轮熔断和运维检查规则。
- 低频测试未触发验证码、登录互踢或 `300012`。
- 使用真实提供者向临时数据库执行了完整链路：详情全图下载、SHA-256/pHash、来源、待审任务均成功；重复提交未创建第二个任务，原始话题未新建主 tag。

POC 没有写入正式图库数据库，也没有启动自动定时任务。

源码内保留了默认跳过的真实集成测试。仅在隔离提供者已登录、且确认不会写正式图库时执行：

```powershell
$env:PJSK_XHS_INTEGRATION = '1'
$env:PJSK_XHS_PROVIDER_URL = 'http://127.0.0.1:18060'
python -m unittest tests.test_xhs_integration -v
```

测试只使用系统临时目录，并在结束时删除临时数据库和图片；鉴权提供者可通过临时环境变量 `PJSK_XHS_PROVIDER_TOKEN` 传入 token。

## 推荐部署

提供者应与 AstrBot 处于同一 Docker 网络，并固定服务名 `pjsk-xhs-provider`。宿主机端口若保留，只绑定 `127.0.0.1` 供扫码和诊断使用。REST 业务端点应设置 `AUTH_TOKEN`，插件配置同一个 `xhs_provider_access_token`；健康检查可匿名访问，但搜索、详情和登录状态必须鉴权。

部分版本的浏览器登录态会绑定原容器内的浏览器身份：即使复用同一个 `/app/data`，重建容器仍可能变成未登录。遇到这种情况不能为了补鉴权直接替换已验证的登录容器。允许使用以下过渡部署，但必须同时满足全部条件：

- 保留原登录容器并设置 `restart=unless-stopped`，不要复制 Cookie 或浏览器资料。
- 建立只供 AstrBot 与提供者通信的专用 Docker 网络，并给提供者设置 `pjsk-xhs-provider` 网络别名。
- 宿主机诊断端口只能绑定 `127.0.0.1`；不得绑定局域网或公网地址。
- 只有在专用网络和回环绑定都已核实时，才可暂时把 `xhs_provider_access_token` 留空。
- 后续准备好重新扫码时，应新建带 `AUTH_TOKEN` 的容器、完成登录与搜索 smoke，再切换 AstrBot；不要把未鉴权过渡形态当作最终方案。
- 新建容器时为 Docker 日志配置容量与文件数上限，并优先采用已修复详情 URL 脱敏的提供者版本；当前 v2.5.0 的日志访问权限必须限制在运维主机。

上线就绪检查必须同时通过 `/health`、登录状态和一次低频受限搜索。若健康与登录正常、但搜索连续超时，说明浏览器页面可能已经卡死；重启原登录容器后，再次确认登录态与搜索成功，才能恢复自动调度。

Cookie、token、提供者数据目录、下载图片、数据库与日志均不得提交到源码仓库。
