# 6期 plan：Vue 3 WebUI 框架化重构

## 目标

- 将独立 WebUI 从 `core/webui.py` 内嵌 HTML/CSS/vanilla JS，迁移为 Vue 3 + Vite + TypeScript 单页应用。
- 保留现有 aiohttp `/api/*` 协议，避免前端重构同时改变业务后端。
- 构建产物随插件发布，普通 AstrBot 用户安装插件后不需要 Node/npm。

## 范围

- 新增 `webui/` 前端源码工程。
- 新增 `core/webui_static/` 构建产物目录。
- 调整 `core/webui.py`：
  - `/` 优先返回 Vue `index.html`
  - `/assets/*` 返回 Vite 静态资源
  - 保留旧 HTML 作为构建产物缺失时的 fallback
- 迁移 8 个页面：
  - 概览
  - 图片检索
  - 审核任务
  - 采集任务
  - tag 管理
  - Pixiv 审批
  - Pixiv 平台词
  - tag 归并

## 交互要求

- 使用工作台式布局：侧边导航、顶部操作、筛选区、结果区、详情/批量操作区。
- 统一 toast、loading、弹窗、状态 badge 和列表卡片。
- Pixiv 审批页保留：
  - `mzk` / `Akiyama` 角色搜索与命中解释
  - 点击图片预览
  - 主 tag 单选
  - alias / Pixiv 来源词多选
  - 单张确认 / 拒绝后立即移出当前列表
  - 单张拒绝不弹确认框

## 验证

- `npm install`
- `npm run build`
- `python -m compileall data\plugins\astrbot_plugin_pjsk_pic`
- 热重载插件，确认 `activated=true`
- 浏览器打开 `http://127.0.0.1:9099/?v=0.14.0#overview`
- 检查 8 个页面可访问，Pixiv 审批页可搜索 `mzk` / `Akiyama`

## 延后

- 不在本期重构数据库结构。
- 不在本期改变 `/api/*` 响应协议。
- 不在本期引入新的 Python 运行依赖。
