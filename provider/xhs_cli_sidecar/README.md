# PJSK 小红书分页 sidecar

该 sidecar 固定安装 `xiaohongshu-cli 0.6.4`，将真实 page 搜索和详情结果转换为图库插件现有的 REST 契约。

- Cookie 文件只读挂载到 `/provider-data/cookies.json`，不复制到镜像或源码。
- 搜索 session 按发布时间过滤分开写入 `/app/state/search-*.json`；图库的页码/页内 checkpoint 保存在插件 SQLite。
- `/health` 可匿名访问；其他接口使用 `AUTH_TOKEN` Bearer 鉴权。
- 请求在进程内串行，`XhsClient` 关闭内部重复重试（`max_retries=1`）。
- 插件通过 `xhs_provider_kind=xiaohongshu_cli` 显式选择；不会自动切换到旧 provider。

构建：

```powershell
docker build -t pjsk-xhs-cli-sidecar:0.1.0 .
```

源码来源：[jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli)，固定 0.6.4，审计提交 `4d63f3c0c85ccd9054fa8e96d7f761aaf2507449`，上游声明 Apache-2.0。完整 Linux 依赖由该提交的 uv.lock 导出并固定，基础镜像也固定摘要。

运行时设置 `AUTH_TOKEN`，将 Cookie 文件只读挂载到 `/provider-data/cookies.json`，为 `/app/state` 使用 Docker volume，并加入 AstrBot 的私有网络。宿主端口仅绑定回环；插件配置 `xhs_provider_kind=xiaohongshu_cli`、该容器 REST 地址和相同访问令牌。旧 MCP 容器停止后保留，用于人工恢复。

无需浏览器，也不自动重新登录；Cookie 失效时通过原提供者重新登录，完成后重启 sidecar 读取新的 Cookie 文件。不要把 Cookie 或访问令牌加入镜像与源码。
