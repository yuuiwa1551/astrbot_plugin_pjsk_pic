from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from aiohttp import web
from astrbot.api import logger

from .crawl_tag_rules import parse_crawl_rule_text, parse_tag_csv
from .db import ImageIndexDB
from .matcher import normalize_tag_name
from .pixiv_backfill_service import PixivBackfillService
from .pixiv_search_service import PixivSearchService
from .pixiv_tag_terms import known_pixiv_query_terms as shared_known_pixiv_query_terms
from .tag_identity_service import TagIdentityService

WEBUI_STATIC_DIR = Path(__file__).resolve().parent / "webui_static"
IMAGE_FILE_CACHE_HEADERS = {"Cache-Control": "private, max-age=3600"}


def _bounded_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _pagination_from_query(args: Any, *, default_limit: int, max_limit: int) -> tuple[int, int]:
    limit = _bounded_int(args.get("limit", default_limit), default_limit, min_value=1, max_value=max_limit)
    raw_page = str(args.get("page", "") or "").strip()
    if raw_page:
        page = _bounded_int(raw_page, 1, min_value=1, max_value=1_000_000)
        return limit, (page - 1) * limit
    offset = _bounded_int(args.get("offset", 0), 0, min_value=0, max_value=1_000_000_000)
    return limit, offset


def _pagination_payload(total: int, limit: int, offset: int) -> dict[str, int]:
    page_count = max(1, (max(total, 0) + limit - 1) // limit)
    page = max(1, offset // limit + 1)
    return {
        "total": max(total, 0),
        "limit": limit,
        "offset": offset,
        "page": page,
        "page_count": page_count,
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>PJSK 图片库管理</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f5f6f8; color: #222; }
    header { padding: 16px 20px; background: #3c65f5; color: white; }
    main { padding: 16px; display: block; max-width: 1280px; margin: 0 auto; }
    main > div { display: contents; }
    section { background: white; border-radius: 10px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
    .page-section { display: none; }
    .page-section.active { display: block; }
    .page-nav { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
    .page-nav button { background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.28); }
    .page-nav button.active { background: white; color: #2f52d6; }
    h2, h3 { margin-top: 0; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; align-items: center; }
    input, select, button { padding: 8px; border: 1px solid #d5d8df; border-radius: 8px; }
    button { cursor: pointer; background: #3c65f5; color: white; border: none; }
    button.secondary { background: #8892a6; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(90px,1fr)); gap: 10px; }
    .stat { background: #eef2ff; border-radius: 8px; padding: 10px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }
    .card { border: 1px solid #eceef2; border-radius: 10px; overflow: hidden; background: #fff; }
    .card img { width: 100%; height: 180px; object-fit: cover; background: #ddd; }
    .card-thumb-wrap { position: relative; }
    .card-thumb-wrap img { display: block; cursor: zoom-in; }
    .card .body { padding: 10px; font-size: 13px; }
    .list { display: grid; gap: 10px; }
    .item { border: 1px solid #eceef2; border-radius: 8px; padding: 10px; }
    .review-item { display: grid; grid-template-columns: 92px 1fr; gap: 10px; align-items: start; }
    .review-item img { width: 92px; height: 92px; object-fit: cover; border-radius: 8px; background: #ddd; }
    .review-actions { display: grid; gap: 6px; margin-top: 8px; }
    .review-action { border-top: 1px dashed #e5e7eb; padding-top: 6px; }
    .review-action .row { margin: 6px 0 0; }
    .muted { color: #666; font-size: 12px; }
    .pill { display: inline-block; background: #eef2ff; color: #2f52d6; border-radius: 999px; padding: 2px 8px; margin: 2px 4px 2px 0; }
    .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
    .chip { display: inline-block; background: white; color: #2f52d6; border: 1px solid #cfd8ff; border-radius: 999px; padding: 6px 10px; margin: 2px 4px 2px 0; font-size: 12px; }
    .chip.selected { background: #3c65f5; color: white; border-color: #3c65f5; }
    .chip.resolved { border-color: #93a7ff; }
    .chip.unresolved { border-style: dashed; }
    .notice { margin-top: 8px; font-size: 12px; color: #dce4ff; }
    .subheading { font-size: 12px; color: #666; margin: 10px 0 6px; }
    .subheading-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 10px 0 6px; }
    .subheading-row .subheading { margin: 0; }
    .inline-form { border: 1px dashed #d8def5; border-radius: 10px; padding: 8px; background: #f8faff; }
    .tag-block { border: 1px dashed #e5e7eb; border-radius: 10px; padding: 10px; }
    .empty { padding: 20px 0; text-align: center; color: #666; }
    .pixiv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
    .pixiv-card { border: 1px solid #eceef2; border-radius: 12px; overflow: hidden; background: #fff; }
    .pixiv-card.selected { box-shadow: 0 0 0 2px #3c65f5 inset; }
    .pixiv-thumb-wrap { position: relative; }
    .pixiv-thumb-wrap img { width: 100%; height: 250px; object-fit: cover; background: #ddd; cursor: zoom-in; }
    .preview-btn { position: absolute; right: 10px; bottom: 10px; background: rgba(0,0,0,.65); }
    .pixiv-body { padding: 12px; display: grid; gap: 6px; }
    .pixiv-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .modal { position: fixed; inset: 0; background: rgba(15,23,42,.55); display: none; align-items: center; justify-content: center; padding: 20px; z-index: 999; }
    .modal.show { display: flex; }
    .modal-panel { width: min(1180px, 96vw); max-height: 92vh; overflow: auto; background: white; border-radius: 16px; box-shadow: 0 18px 50px rgba(15,23,42,.28); }
    .modal-header { padding: 14px 16px; border-bottom: 1px solid #e5e7eb; display: flex; justify-content: space-between; gap: 8px; position: sticky; top: 0; background: white; z-index: 2; }
    .modal-body { padding: 16px; display: grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 0.95fr); gap: 16px; }
    .modal-image img { width: 100%; max-height: 72vh; object-fit: contain; border-radius: 12px; background: #ddd; }
    .mapping-box { border: 1px solid #eceef2; border-radius: 10px; padding: 10px; margin-bottom: 8px; }
    .split-grid { display: grid; grid-template-columns: minmax(380px, 1.15fr) minmax(320px, 0.85fr); gap: 16px; }
    .stack { display: grid; gap: 16px; }
    .term-item { border: 1px solid #eceef2; border-radius: 10px; padding: 12px; display: grid; gap: 6px; }
    .term-item.selected { box-shadow: 0 0 0 2px #3c65f5 inset; }
    .term-head { display: flex; justify-content: space-between; gap: 10px; align-items: start; flex-wrap: wrap; }
    .term-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .mini-btn { padding: 6px 10px; font-size: 12px; }
    button.danger { background: #dc2626; }
    .sample-links { display: grid; gap: 4px; }
    .toolbar-box { border: 1px dashed #d8def5; border-radius: 12px; padding: 12px; margin-bottom: 14px; background: #f8faff; }
    .toolbar-title { font-weight: 700; min-width: 72px; }
    .filter-box { border-style: solid; background: #fff; }
    .filter-box input { min-width: min(420px, 100%); }
    .search-context { color: #475569; font-size: 13px; min-height: 18px; }
    .batch-box { transition: border-color .16s ease, background .16s ease; }
    .batch-box.is-active { border-color: #9fb2ff; background: #f8faff; }
    .batch-box.is-idle .batch-controls { display: none; }
    .batch-box.is-active .batch-idle-hint { display: none; }
    .preview-box { border: 1px solid #eceef2; border-radius: 10px; padding: 12px; background: #fbfcff; }
    .merge-arrow { font-weight: bold; color: #3c65f5; }
    .toast { position: fixed; right: 18px; bottom: 18px; z-index: 1200; max-width: min(420px, calc(100vw - 36px)); background: #15223b; color: white; border-radius: 10px; padding: 12px 14px; box-shadow: 0 12px 32px rgba(15,23,42,.26); opacity: 0; transform: translateY(8px); pointer-events: none; transition: opacity .16s ease, transform .16s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast .muted { color: #c9d5ef; }
    pre.json { background: #0f172a; color: #d7e2ff; padding: 10px; border-radius: 12px; overflow: auto; font-size: 12px; }
    @media (max-width: 1000px) {
      main { padding: 12px; }
      .modal-body { grid-template-columns: 1fr; }
      .pixiv-grid { grid-template-columns: 1fr; }
      .split-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<header>
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;">
    <h1 style="margin:0;">PJSK 图片库管理台</h1>
    <button class="secondary" onclick="logoutWebUi()">退出登录</button>
  </div>
  <div class="muted" style="color:#dce4ff;">独立 WebUI：支持图库检索、Pixiv 图片审批、Pixiv 平台词管理、tag/别名管理、审核任务、采集任务与平台来源信息查看</div>
  <div class="notice" id="notice"></div>
  <nav class="page-nav" aria-label="功能导航">
    <button class="active" data-page-button="overview" onclick="showPage('overview')">概览</button>
    <button data-page-button="gallery" onclick="showPage('gallery')">图片检索</button>
    <button data-page-button="reviews" onclick="showPage('reviews')">审核任务</button>
    <button data-page-button="jobs" onclick="showPage('jobs')">采集任务</button>
    <button data-page-button="tags" onclick="showPage('tags')">tag 管理</button>
    <button data-page-button="pixiv-review" onclick="showPage('pixiv-review')">Pixiv 审批</button>
    <button data-page-button="pixiv-platform" onclick="showPage('pixiv-platform')">Pixiv 平台词</button>
    <button data-page-button="tag-merge" onclick="showPage('tag-merge')">tag 归并</button>
  </nav>
</header>
<main>
  <div>
    <section class="page-section active" data-page="overview">
      <h2>概览</h2>
      <div class="stats" id="stats"></div>
    </section>
    <section class="page-section" data-page="gallery">
      <h2>图片检索</h2>
      <div class="row">
        <input id="keyword" placeholder="关键词 / tag / alias" style="flex:1;" />
        <input id="tag" placeholder="精确 tag" />
        <select id="status"><option value="">全部状态</option><option>approved</option><option>manual_approved</option><option>pending</option><option>uncertain</option><option>rejected</option><option>manual_rejected</option></select>
        <select id="platform"><option value="">全部平台</option><option>pixiv</option><option>x</option><option>xiaohongshu</option><option>generic</option><option>submission</option></select>
        <button onclick="loadImages()">搜索</button>
      </div>
      <div class="grid" id="images"></div>
    </section>
  </div>
  <div>
    <section class="page-section" data-page="jobs">
      <h2>采集任务</h2>
      <div class="row">
        <select id="jobPlatform"><option>pixiv</option><option>x</option><option>xiaohongshu</option><option>generic</option></select>
        <input id="jobUrl" placeholder="帖子链接或图片直链" style="flex:1;" />
      </div>
      <div class="row">
        <input id="jobTags" placeholder="可选 tags_csv，例如 初音未来,miku" style="flex:1;" />
        <button onclick="createJob()">新建任务</button>
      </div>
      <div class="row">
        <input id="jobIncludeTags" placeholder="可选 include tags，例如 初音未来,天马司" style="flex:1;" />
        <input id="jobExcludeTags" placeholder="可选 exclude tags，例如 R-18,梦向" style="flex:1;" />
      </div>
      <div class="list" id="jobs"></div>
    </section>
    <section class="page-section" data-page="reviews">
      <h2>审核任务</h2>
      <div class="row">
        <select id="reviewStatus"><option value="">全部</option><option>pending</option><option>uncertain</option><option>rejected</option><option>approved</option><option>manual_approved</option><option>manual_rejected</option></select>
        <button onclick="loadReviews()">刷新审核</button>
      </div>
      <div class="list" id="reviews"></div>
    </section>
    <section class="page-section" data-page="tags">
      <h2>tag 管理</h2>
      <div class="row">
        <input id="tagSearch" placeholder="搜索 tag" style="flex:1;" />
        <button onclick="loadTags()">搜索</button>
      </div>
      <div class="row">
        <input id="aliasTag" placeholder="tag" />
        <input id="aliasValue" placeholder="alias" />
        <button onclick="addAlias()">添加别名</button>
        <button class="secondary" onclick="removeAlias()">删除别名</button>
      </div>
      <div class="row">
        <input id="charTag" placeholder="tag" />
        <select id="charValue"><option value="true">设为角色</option><option value="false">设为普通</option></select>
        <button onclick="setCharacter()">提交</button>
      </div>
      <div class="list" id="tags"></div>
    </section>
  </div>
  <section class="page-section" data-page="pixiv-review">
    <h2>Pixiv 审批页</h2>
    <div class="toolbar-box filter-box">
      <div class="row">
        <span class="toolbar-title">筛选</span>
        <select id="pixivReviewStatus" onchange="loadPixivReviewImages()">
          <option value="pending,uncertain">待处理（pending / uncertain）</option>
          <option value="pending">仅 pending</option>
          <option value="uncertain">仅 uncertain</option>
          <option value="rejected">仅 rejected</option>
          <option value="pending,uncertain,rejected">待处理 + rejected</option>
        </select>
        <input id="pixivReviewKeyword" placeholder="搜索角色 / alias / Pixiv tag，例如 mzk、Akiyama Mizuki" style="flex:1;" oninput="schedulePixivReviewSearch()" onkeydown="handlePixivReviewKeywordKeydown(event)" />
        <button class="secondary" onclick="clearPixivReviewSearch()">清空搜索</button>
        <button onclick="loadPixivReviewImages()">刷新 Pixiv 审批</button>
      </div>
      <div class="search-context" id="pixivReviewSearchContext">先筛选角色相关待审图，再勾选图片做批量审核。</div>
    </div>
    <div class="toolbar-box batch-box is-idle" id="pixivBatchBox">
      <div class="row">
        <span class="toolbar-title">批量审核</span>
        <span class="muted" id="pixivBatchSelectionHint">当前已选 0 张图片。</span>
        <button class="secondary" onclick="selectAllPixivReviewImages()">全选当前页</button>
        <button class="secondary" onclick="clearSelectedPixivReviewImages()">清空已选</button>
      </div>
      <div class="muted batch-idle-hint">勾选图片后显示批量归入和 alias / 来源词操作。</div>
      <div class="batch-controls">
        <div class="row">
          <input id="pixivBatchTags" placeholder="批量归入主 tag；多个时第 1 个为主 tag，其余作为别名词" style="flex:1;" />
          <input id="pixivBatchSourceTerms" placeholder="批量 alias / Pixiv 来源词，逗号分隔；留空时使用每张图当前已选来源词" style="flex:1;" />
          <label class="muted"><input id="pixivBatchRejectUnselected" type="checkbox" checked /> 拒绝未选 tag</label>
        </div>
        <div class="row">
          <button class="secondary" onclick="applyBatchTagsToSelectedImages()">将上面输入应用到已选图片</button>
          <button onclick="previewPixivBatchReview()">批量预览审核</button>
          <button onclick="submitPixivBatchReview()">批量确认审核</button>
        </div>
        <div class="list" id="pixivBatchPreview"></div>
      </div>
    </div>
    <div class="pixiv-grid" id="pixivReviewImages"></div>
  </section>
  <section class="page-section" data-page="pixiv-platform">
    <h2>Pixiv 平台词管理</h2>
    <div class="row">
      <input id="pixivPlatformTagFilter" placeholder="按主 tag 筛选，例如 初音未来" />
      <input id="pixivPlatformKeyword" placeholder="按 Pixiv 词搜索 / 查看未解决词" style="flex:1;" />
      <select id="pixivPlatformTypeFilter">
        <option value="">全部类型</option>
        <option value="both">both</option>
        <option value="query">query</option>
        <option value="match">match</option>
      </select>
      <button onclick="loadPixivPlatformTerms()">查询平台词</button>
      <button class="secondary" onclick="loadPixivPlatformSuggestions()">查看建议词</button>
      <button class="secondary" onclick="loadPixivPlatformUnresolved()">刷新未解决词</button>
    </div>
    <div class="row">
      <input id="pixivPlatformTagInput" placeholder="主 tag" />
      <input id="pixivPlatformTermInput" placeholder="Pixiv 词，例如 初音ミク" style="flex:1;" />
      <select id="pixivPlatformTypeInput">
        <option value="both">both</option>
        <option value="query">query</option>
        <option value="match">match</option>
      </select>
      <input id="pixivPlatformSourceInput" placeholder="来源" value="manual_review" />
      <input id="pixivPlatformConfidenceInput" type="number" step="0.01" min="0" max="1" value="1" style="width:110px;" />
      <button id="pixivPlatformSaveButton" onclick="savePixivPlatformTerm()">新增平台词</button>
      <button class="secondary" onclick="resetPixivPlatformForm()">重置</button>
    </div>
    <div class="split-grid">
      <div>
        <div class="subheading">已配置平台词</div>
        <div class="list" id="pixivPlatformTerms"></div>
      </div>
      <div class="stack">
        <div>
          <div class="subheading">历史建议词</div>
          <div class="muted" id="pixivPlatformSuggestionsHint">输入主 tag 后点击“查看建议词”。</div>
          <div class="list" id="pixivPlatformSuggestions"></div>
        </div>
        <div>
          <div class="subheading">未解决词 / 待确认词</div>
          <div class="toolbar-box">
            <div class="row">
              <strong>批量确认 Pixiv 词映射</strong>
              <span class="muted" id="pixivBulkMapSelectionHint">当前已选 0 个词。</span>
              <button class="secondary" onclick="clearPixivBulkMapSelection()">清空已选</button>
            </div>
            <div class="row">
              <input id="pixivBulkMapTag" placeholder="目标主 tag" />
              <input id="pixivBulkMapTerms" placeholder="额外 Pixiv 词，逗号分隔；可与勾选未解决词一起提交" style="flex:1;" />
              <select id="pixivBulkMapType">
                <option value="both">both</option>
                <option value="query">query</option>
                <option value="match">match</option>
              </select>
              <input id="pixivBulkMapSource" placeholder="来源" value="pixiv_history" />
              <input id="pixivBulkMapConfidence" type="number" step="0.01" min="0" max="1" value="0.8" style="width:110px;" />
            </div>
            <div class="row">
              <button onclick="previewPixivBulkMap()">批量映射预览</button>
              <button onclick="submitPixivBulkMap()">批量确认映射</button>
            </div>
            <div class="list" id="pixivBulkMapPreview"></div>
          </div>
          <div class="list" id="pixivPlatformUnresolved"></div>
        </div>
      </div>
    </div>
  </section>
  <section class="page-section" data-page="tag-merge">
    <h2>历史 tag 归并助手</h2>
    <div class="row">
      <input id="tagMergeKeyword" placeholder="按 source / target / 词搜索候选" style="flex:1;" />
      <button onclick="loadTagMergeCandidates()">刷新候选</button>
    </div>
    <div class="row">
      <input id="tagMergeTarget" placeholder="目标主 tag" />
      <input id="tagMergeSources" placeholder="来源 tag，逗号分隔" style="flex:1;" />
      <button onclick="previewTagMerge()">预览归并</button>
      <button onclick="executeTagMerge()">执行归并</button>
    </div>
    <div class="split-grid">
      <div>
        <div class="subheading">候选归并对</div>
        <div class="list" id="tagMergeCandidates"></div>
      </div>
      <div>
        <div class="subheading">归并影响预览</div>
        <div class="list" id="tagMergePreview"></div>
      </div>
    </div>
  </section>
</main>
<div class="modal" id="pixivPreviewModal">
  <div class="modal-panel">
    <div class="modal-header">
      <div>
        <strong id="pixivPreviewTitle">Pixiv 预览</strong>
        <div class="muted" id="pixivPreviewSubtitle"></div>
      </div>
      <div class="row" style="margin:0;">
        <button class="secondary" onclick="closePixivPreview()">关闭</button>
      </div>
    </div>
    <div class="modal-body" id="pixivPreviewBody"></div>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script>
document.getElementById('notice').textContent = '当前使用站点会话访问；如需切换账户，可点击右上角退出登录。';

let toastTimer = null;
let pixivReviewSearchTimer = null;

function showToast(message, detail = '') {
  const toast = document.getElementById('toast');
  if (!toast) return;
  if (toastTimer) clearTimeout(toastTimer);
  toast.innerHTML = `<div>${escapeHtml(message || '')}</div>${detail ? `<div class="muted">${escapeHtml(detail)}</div>` : ''}`;
  toast.classList.add('show');
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3600);
}

function api(path) {
  return new URL(path, location.origin).toString();
}

async function fetchJson(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  const resp = await fetch(api(path), {...options, headers, credentials: 'same-origin'});
  if (!resp.ok) throw new Error(await resp.text());
  return await resp.json();
}

async function logoutWebUi() {
  try {
    await fetchJson('/api/auth/logout', {method: 'POST'});
  } catch (err) {
    console.warn(err);
  }
  location.reload();
}

const pixivReviewState = {
  items: {},
  selectedTagsByImage: {},
  selectedTermsByImage: {},
  selectedImages: {},
  previewImageId: null,
  batchPreview: [],
  manualTagFormImageId: null,
  searchKeyword: '',
  searchContext: null,
};

const imagePreviewState = {
  imageId: null,
  item: null,
};

const pixivPlatformState = {
  items: [],
  suggestionsTag: '',
  suggestions: [],
  unresolved: [],
  selectedBulkTerms: {},
  editingTermId: 0,
  bulkPreview: null,
};

const tagMergeState = {
  candidates: [],
  preview: null,
};

let currentPage = 'overview';
const loadedPages = {};

const pageLoaders = {
  overview: [loadSummary],
  gallery: [loadImages],
  reviews: [loadReviews],
  jobs: [loadJobs],
  tags: [loadTags],
  'pixiv-review': [loadPixivReviewImages],
  'pixiv-platform': [loadPixivPlatformTerms, loadPixivPlatformUnresolved],
  'tag-merge': [loadTagMergeCandidates],
};

function normalizePageName(page) {
  return Object.prototype.hasOwnProperty.call(pageLoaders, page) ? page : 'overview';
}

function markPagesDirty(pages) {
  for (const page of pages || []) delete loadedPages[page];
}

async function loadPageData(page, {force = false} = {}) {
  const normalized = normalizePageName(page);
  if (!force && loadedPages[normalized]) return;
  const loaders = pageLoaders[normalized] || [];
  await Promise.all(loaders.map(loader => loader()));
  loadedPages[normalized] = true;
}

async function showPage(page, {force = false, updateHash = true} = {}) {
  const normalized = normalizePageName(page);
  currentPage = normalized;
  document.querySelectorAll('.page-section').forEach(section => {
    section.classList.toggle('active', section.dataset.page === normalized);
  });
  document.querySelectorAll('[data-page-button]').forEach(button => {
    button.classList.toggle('active', button.dataset.pageButton === normalized);
  });
  if (updateHash && location.hash !== `#${normalized}`) {
    history.replaceState(null, '', `#${normalized}`);
  }
  await loadPageData(normalized, {force});
}

function removePixivReviewImageFromQueue(imageId) {
  const key = Number(imageId || 0);
  if (!key || !pixivReviewState.items[key]) return false;
  delete pixivReviewState.items[key];
  delete pixivReviewState.selectedTagsByImage[key];
  delete pixivReviewState.selectedTermsByImage[key];
  delete pixivReviewState.selectedImages[key];
  if (pixivReviewState.previewImageId === key) closePixivPreview();
  renderPixivReviewList();
  renderPixivBatchPreview();
  return true;
}

function prunePixivReviewSelections() {
  const currentIds = new Set(Object.keys(pixivReviewState.items || {}).map(Number));
  for (const key of Object.keys(pixivReviewState.selectedImages || {})) {
    if (!currentIds.has(Number(key))) delete pixivReviewState.selectedImages[key];
  }
  for (const key of Object.keys(pixivReviewState.selectedTagsByImage || {})) {
    if (!currentIds.has(Number(key))) delete pixivReviewState.selectedTagsByImage[key];
  }
  for (const key of Object.keys(pixivReviewState.selectedTermsByImage || {})) {
    if (!currentIds.has(Number(key))) delete pixivReviewState.selectedTermsByImage[key];
  }
}

async function refreshAfterReviewMutation({removePixivImageIds = [], toastMessage = '', toastDetail = ''} = {}) {
  const removed = [];
  for (const imageId of removePixivImageIds || []) {
    if (removePixivReviewImageFromQueue(imageId)) removed.push(Number(imageId));
  }
  if (toastMessage) showToast(toastMessage, toastDetail);
  markPagesDirty(['overview', 'gallery', 'reviews', 'pixiv-review', 'pixiv-platform', 'tag-merge']);
  if (currentPage === 'pixiv-review' && removed.length) {
    loadSummary().catch(err => console.error(err));
    return;
  }
  await loadPageData(currentPage, {force: true});
}

async function refreshAfterPlatformMutation(tagName = '') {
  markPagesDirty(['pixiv-platform', 'pixiv-review', 'tag-merge']);
  if (currentPage === 'pixiv-platform') {
    await Promise.all([loadPixivPlatformTerms(), loadPixivPlatformSuggestions(tagName), loadPixivPlatformUnresolved()]);
    loadedPages['pixiv-platform'] = true;
    return;
  }
  await loadPageData(currentPage, {force: true});
}

async function refreshAfterTagMergeMutation() {
  markPagesDirty(['overview', 'tags', 'pixiv-platform', 'pixiv-review', 'tag-merge']);
  await loadPageData(currentPage, {force: true});
}

function normalizeKey(value) {
  return String(value || '').normalize('NFKC').trim().toLowerCase().replace(/\\s+/g, '');
}

function uniqueTexts(values) {
  const seen = new Set();
  const result = [];
  for (const raw of values || []) {
    const text = String(raw || '').trim();
    const key = normalizeKey(text);
    if (!text || !key || seen.has(key)) continue;
    seen.add(key);
    result.push(text);
  }
  return result;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, '&#96;');
}

const ACTIONABLE_REVIEW_STATUSES = new Set(['pending', 'uncertain', 'rejected']);

function isActionableReviewTask(task) {
  return ACTIONABLE_REVIEW_STATUSES.has(String((task || {}).status || ''));
}

function renderImageTags(tags) {
  return (tags || []).map(tag => `<span class="pill">${escapeHtml(tag.name)}(${escapeHtml(tag.review_status)})</span>`).join('') || '<span class="muted">无</span>';
}

function renderReviewTaskActions(tasks) {
  const activeTasks = (tasks || []).filter(isActionableReviewTask);
  if (!activeTasks.length) return '<div class="muted">暂无待审批任务</div>';
  return `<div class="review-actions">${activeTasks.map(task => {
    const reviewId = Number(task.id || 0);
    return `
      <div class="review-action">
        <div><strong>${escapeHtml(task.tag_name || '-')}</strong> <span class="pill">${escapeHtml(task.status || '-')}</span></div>
        <div class="muted">${escapeHtml(task.reason || task.source_type || '')}</div>
        <div class="row">
          <button class="mini-btn" onclick="reviewDecision(${reviewId}, true)">通过</button>
          <button class="mini-btn danger" onclick="reviewDecision(${reviewId}, false)">拒绝</button>
        </div>
      </div>
    `;
  }).join('')}</div>`;
}

function renderImageSources(sources) {
  return (sources || []).map((source, index) => {
    const extra = source.extra || {};
    const postUrl = String(source.post_url || '');
    const title = String(extra.title || extra.illust_title || '');
    return `
      <div class="item">
        <div><strong>${escapeHtml(source.platform || `来源 ${index + 1}`)}</strong>${title ? ` · ${escapeHtml(title)}` : ''}</div>
        <div class="muted">${escapeHtml(source.author || '-')}</div>
        <div>${postUrl ? `<a href="${escapeAttr(postUrl)}" target="_blank" rel="noreferrer">${escapeHtml(postUrl)}</a>` : '-'}</div>
        <div class="muted">原始 tag：${(source.raw_tags || []).map(escapeHtml).join('、') || '无'}</div>
      </div>
    `;
  }).join('') || '<div class="muted">无来源</div>';
}

function renderStats(stats) {
  const items = [['图片', stats.images], ['tag', stats.tags], ['alias', stats.aliases], ['采集任务', stats.crawl_jobs], ['待审核', stats.pending_reviews]];
  document.getElementById('stats').innerHTML = items.map(([k,v]) => `<div class="stat"><div class="muted">${k}</div><div style="font-size:22px;font-weight:bold;">${v}</div></div>`).join('');
}

async function loadSummary() { renderStats(await fetchJson('/api/summary')); }

async function loadImages() {
  const q = new URLSearchParams({
    keyword: document.getElementById('keyword').value,
    tag: document.getElementById('tag').value,
    review_status: document.getElementById('status').value,
    platform: document.getElementById('platform').value,
    limit: '30'
  });
  const data = await fetchJson(`/api/images?${q.toString()}`);
  document.getElementById('images').innerHTML = (data.items || []).map(item => {
    const imageId = Number(item.id || 0);
    return `
      <div class="card">
        <div class="card-thumb-wrap">
          <img src="${api(`/api/image-file?image_id=${imageId}`)}" loading="lazy" onclick="openImagePreview(${imageId})" />
          <button class="preview-btn" onclick="openImagePreview(${imageId})">预览</button>
        </div>
        <div class="body">
          <div><strong>#${imageId}</strong> ${escapeHtml(item.file_name || '')}</div>
          <div class="muted">${escapeHtml(item.width || 0)}x${escapeHtml(item.height || 0)} · ${escapeHtml(item.format || '')} · ${escapeHtml(item.platform || 'local')}</div>
          <div class="muted">phash: ${escapeHtml(item.phash || '-')}</div>
          <div class="muted">来源: ${item.post_url ? `<a href="${escapeAttr(item.post_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.post_url)}</a>` : '-'}</div>
          <div class="muted">疑似重复: ${(item.similar_image_ids || []).map(escapeHtml).join(', ') || '无'}</div>
          <div>${renderImageTags(item.tags || [])}</div>
          <div class="subheading">待审批</div>
          ${renderReviewTaskActions(item.review_tasks || [])}
        </div>
      </div>
    `;
  }).join('') || '<div class="muted">暂无结果</div>';
}

async function loadJobs() {
  const data = await fetchJson('/api/jobs');
  document.getElementById('jobs').innerHTML = (data.items || []).map(item => `
    <div class="item">
      <div><strong>#${item.id}</strong> [${item.status}] ${item.platform} · 第${item.attempt_count || 0}次</div>
      <div class="muted">${item.source_url}</div>
      <div>标签：${item.tags_text || '自动提取'}</div>
      <div>包含采集：${item.include_tags_text || '-'}</div>
      <div>排除采集：${item.exclude_tags_text || '-'}</div>
      <div>结果：${item.result_summary || item.error_log || '-'}</div>
      <div class="row"><button onclick="retryJob(${item.id})">重试</button></div>
    </div>
  `).join('') || '<div class="muted">暂无任务</div>';
}

async function createJob() {
  await fetchJson('/api/jobs', {method: 'POST', body: JSON.stringify({
    platform: document.getElementById('jobPlatform').value,
    source_url: document.getElementById('jobUrl').value,
    tags: document.getElementById('jobTags').value,
    include_tags: document.getElementById('jobIncludeTags').value,
    exclude_tags: document.getElementById('jobExcludeTags').value,
  })});
  await loadJobs(); await loadSummary();
}

async function retryJob(jobId) {
  await fetchJson('/api/jobs/retry', {method: 'POST', body: JSON.stringify({job_id: jobId})});
  await loadJobs();
}

async function loadReviews() {
  const q = new URLSearchParams({status: document.getElementById('reviewStatus').value, limit: '20'});
  const data = await fetchJson(`/api/reviews?${q.toString()}`);
  document.getElementById('reviews').innerHTML = (data.items || []).map(item => `
    <div class="item review-item">
      <img src="${api(`/api/image-file?image_id=${item.image_id}`)}" loading="lazy" />
      <div>
        <div><strong>#${item.id}</strong> [${item.status}] ${item.tag_name}</div>
        <div class="muted">image=${item.image_id} · source=${item.source_type || '-'}</div>
        <div>${item.reason || '-'}</div>
        <div class="row">
          <button onclick="reviewDecision(${item.id}, true)">通过</button>
          <button class="secondary" onclick="reviewDecision(${item.id}, false)">拒绝</button>
        </div>
      </div>
    </div>
  `).join('') || '<div class="muted">暂无审核任务</div>';
}

async function reviewDecision(reviewId, approved) {
  const result = await fetchJson('/api/reviews/decision', {method: 'POST', body: JSON.stringify({review_id: reviewId, approved})});
  await refreshAfterReviewMutation({toastMessage: result.message || '审核任务已更新'});
  if (imagePreviewState.imageId) await refreshImagePreview();
}

async function loadTags() {
  const q = new URLSearchParams({keyword: document.getElementById('tagSearch').value, limit: '50'});
  const data = await fetchJson(`/api/tags?${q.toString()}`);
  document.getElementById('tags').innerHTML = (data.items || []).map(item => `
    <div class="item">
      <div><strong>${item.name}</strong> ${item.is_character ? '<span class="pill">角色</span>' : ''}</div>
      <div class="muted">图片数：${item.image_count}</div>
      <div>别名：${(item.aliases || []).join('、') || '无'}</div>
    </div>
  `).join('') || '<div class="muted">暂无 tag</div>';
}

async function addAlias() {
  await fetchJson('/api/tag/alias', {method: 'POST', body: JSON.stringify({tag_name: document.getElementById('aliasTag').value, alias: document.getElementById('aliasValue').value})});
  await loadTags();
}

async function removeAlias() {
  await fetchJson('/api/tag/alias', {method: 'DELETE', body: JSON.stringify({tag_name: document.getElementById('aliasTag').value, alias: document.getElementById('aliasValue').value})});
  await loadTags();
}

async function setCharacter() {
  await fetchJson('/api/tag/character', {method: 'POST', body: JSON.stringify({tag_name: document.getElementById('charTag').value, is_character: document.getElementById('charValue').value === 'true'})});
  await loadTags();
}

function ensurePixivReviewSelection(item) {
  if (!item) return;
  const imageId = item.image_id;
  if (!pixivReviewState.selectedTagsByImage[imageId]) {
    const defaults = uniqueTexts((item.review_tasks || []).filter(task => task.status !== 'manual_rejected').map(task => task.tag_name));
    const candidates = uniqueTexts((item.candidate_tags || []).slice(0, 1).map(tag => tag.name));
    const canonical = defaults.length ? defaults[0] : (candidates[0] || '');
    pixivReviewState.selectedTagsByImage[imageId] = canonical ? [canonical] : [];
  }
  if (!pixivReviewState.selectedTermsByImage[imageId]) {
    pixivReviewState.selectedTermsByImage[imageId] = [];
  }
}

function resetPixivReviewSelection(imageId) {
  const item = pixivReviewState.items[imageId];
  if (!item) return;
  delete pixivReviewState.selectedTagsByImage[imageId];
  delete pixivReviewState.selectedTermsByImage[imageId];
  ensurePixivReviewSelection(item);
  renderPixivReviewList();
  renderPixivPreview();
}

function togglePixivManualTagForm(imageId) {
  pixivReviewState.manualTagFormImageId = pixivReviewState.manualTagFormImageId === imageId ? null : imageId;
  renderPixivReviewList();
  renderPixivPreview();
}

function pixivManualTagScope(surface, imageId) {
  return `${String(surface || 'card').replace(/[^a-z0-9_-]/gi, '')}-${imageId}`;
}

function renderPixivManualTagForm(imageId, surface) {
  if (pixivReviewState.manualTagFormImageId !== imageId) return '';
  const scope = pixivManualTagScope(surface, imageId);
  return `
    <div class="row inline-form" data-pixiv-manual-tag-form="${escapeAttr(scope)}">
      <input id="pixivNewTagName-${escapeAttr(scope)}" data-pixiv-new-tag-name placeholder="新增主 tag" style="flex:1;" />
      <label class="muted"><input id="pixivNewTagCharacter-${escapeAttr(scope)}" data-pixiv-new-tag-character type="checkbox" checked /> 角色</label>
      <button class="mini-btn" onclick='createPixivReviewMainTag(${imageId}, ${JSON.stringify(surface || 'card')})'>添加</button>
      <button class="secondary mini-btn" onclick="togglePixivManualTagForm(${imageId})">取消</button>
    </div>
  `;
}

function renderCandidateTagSection(item, selectedTags, surface = 'card') {
  const imageId = item.image_id;
  const canonicalTag = uniqueTexts(selectedTags || [])[0] || '';
  const chips = (item.candidate_tags || [])
    .map(tag => renderCandidateTagChip(imageId, tag, normalizeKey(canonicalTag) === normalizeKey(tag.name)))
    .join('') || '<span class="muted">暂无可归入主 tag</span>';
  return `
    <div class="subheading-row">
      <div class="subheading">归入主 tag（单选）</div>
      <button class="secondary mini-btn" title="新增主 tag" onclick="togglePixivManualTagForm(${imageId})">+</button>
    </div>
    ${renderPixivManualTagForm(imageId, surface)}
    <div class="chip-row">${chips}</div>
  `;
}

async function createPixivReviewMainTag(imageId, surface = 'card') {
  const scope = pixivManualTagScope(surface, imageId);
  const form = document.querySelector(`[data-pixiv-manual-tag-form="${scope}"]`);
  const input = form ? form.querySelector('[data-pixiv-new-tag-name]') : null;
  const checkbox = form ? form.querySelector('[data-pixiv-new-tag-character]') : null;
  const tagName = input ? input.value.trim() : '';
  if (!tagName) {
    showToast('请先填写主 tag');
    return;
  }
  const result = await fetchJson('/api/tag/create', {
    method: 'POST',
    body: JSON.stringify({
      tag_name: tagName,
      is_character: checkbox ? checkbox.checked : true,
    }),
  });
  const tag = result.tag || {name: tagName, is_character: checkbox ? checkbox.checked : true};
  const item = pixivReviewState.items[imageId];
  if (item) {
    const currentCandidates = item.candidate_tags || [];
    if (!currentCandidates.some(candidate => normalizeKey(candidate.name) === normalizeKey(tag.name))) {
      item.candidate_tags = [...currentCandidates, tag];
    }
  }
  pixivReviewState.selectedTagsByImage[imageId] = [tag.name];
  pixivReviewState.manualTagFormImageId = null;
  markPagesDirty(['tags', 'pixiv-platform']);
  renderPixivReviewList();
  renderPixivPreview();
  showToast(result.message || '已添加主 tag', tag.name || tagName);
}

function toggleCandidateTag(imageId, tagName) {
  const current = uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []);
  const key = normalizeKey(tagName);
  const canonical = current[0] || '';
  const exists = normalizeKey(canonical) === key;
  pixivReviewState.selectedTagsByImage[imageId] = exists ? [] : [tagName];
  renderPixivReviewList();
  renderPixivPreview();
}

function toggleSourceTerm(imageId, term, resolvedTagName) {
  const current = uniqueTexts(pixivReviewState.selectedTermsByImage[imageId] || []);
  const key = normalizeKey(term);
  const exists = current.some(item => normalizeKey(item) === key);
  pixivReviewState.selectedTermsByImage[imageId] = exists ? current.filter(item => normalizeKey(item) !== key) : [...current, term];
  if (!exists && resolvedTagName) {
    const selectedTags = uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []);
    if (!selectedTags.length) {
      pixivReviewState.selectedTagsByImage[imageId] = [resolvedTagName];
    }
  }
  renderPixivReviewList();
  renderPixivPreview();
}

function renderCandidateTagChip(imageId, tag, selected) {
  return `<button class="chip ${selected ? 'selected' : ''}" onclick='toggleCandidateTag(${imageId}, ${JSON.stringify(tag.name)})'>${escapeHtml(tag.name)}${tag.is_character ? ' ·角色' : ''}</button>`;
}

function renderSourceTermChip(imageId, term, selected) {
  const prefix = term.origin === 'translated' ? '译' : '原';
  const label = `${prefix}·${term.term}${term.resolved_tag_name ? ` → ${term.resolved_tag_name}` : ''}`;
  const classes = ['chip', selected ? 'selected' : '', term.resolved_tag_name ? 'resolved' : 'unresolved'].filter(Boolean).join(' ');
  return `<button class="${classes}" title="${escapeAttr(term.resolution || '')}" onclick='toggleSourceTerm(${imageId}, ${JSON.stringify(term.term)}, ${JSON.stringify(term.resolved_tag_name || '')})'>${escapeHtml(label)}</button>`;
}

function renderPixivReviewCard(item) {
  ensurePixivReviewSelection(item);
  const imageId = item.image_id;
  const selectedTags = pixivReviewState.selectedTagsByImage[imageId] || [];
  const selectedTerms = pixivReviewState.selectedTermsByImage[imageId] || [];
  const selectedImage = !!pixivReviewState.selectedImages[imageId];
  return `
    <div class="pixiv-card ${selectedImage ? 'selected' : ''}">
      <div class="pixiv-thumb-wrap">
        <img src="${api(`/api/image-file?image_id=${imageId}`)}" loading="lazy" onclick="openPixivPreview(${imageId})" />
        <button class="preview-btn" onclick="openPixivPreview(${imageId})">预览</button>
      </div>
      <div class="pixiv-body">
        <div class="row" style="margin:0;">
          <label><input type="checkbox" ${selectedImage ? 'checked' : ''} onchange="togglePixivReviewImageSelection(${imageId})" /> 选中</label>
          <strong>#${imageId}</strong> ${escapeHtml(item.file_name || '')}
        </div>
        <div class="muted">${item.width}x${item.height} · ${escapeHtml(item.author || '-')}</div>
        <div class="muted">标题：${escapeHtml(item.title || '-')}</div>
        <div class="muted">来源：${item.post_url ? `<a href="${escapeAttr(item.post_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.post_url)}</a>` : '-'}</div>
        <div class="tag-block">
          <div class="subheading">当前审核项</div>
          <div>${(item.review_tasks || []).map(task => `<span class="pill">${escapeHtml(task.tag_name)}(${escapeHtml(task.status)})</span>`).join('') || '<span class="muted">无</span>'}</div>
          ${renderCandidateTagSection(item, selectedTags, 'card')}
          <div class="subheading">alias / Pixiv 来源词</div>
          <div class="chip-row">${(item.source_terms || []).map(term => renderSourceTermChip(imageId, term, selectedTerms.some(name => normalizeKey(name) === normalizeKey(term.term)))).join('') || '<span class="muted">暂无来源 tag</span>'}</div>
          <div class="muted">归入主 tag：${escapeHtml((uniqueTexts(selectedTags)[0] || '无'))}；alias / 搜索词：${selectedTerms.map(escapeHtml).join('、') || '无'}</div>
        </div>
        <div class="pixiv-actions">
          <button onclick="submitPixivReview(${imageId})">确认审核</button>
          <button class="danger" onclick="rejectPixivReviewImage(${imageId})">拒绝图片</button>
          <button class="secondary" onclick="resetPixivReviewSelection(${imageId})">重置</button>
        </div>
      </div>
    </div>
  `;
}

function renderPixivReviewList() {
  const items = Object.values(pixivReviewState.items).sort((a, b) => (b.image_id || 0) - (a.image_id || 0));
  const keyword = String(pixivReviewState.searchKeyword || '').trim();
  const emptyText = keyword
    ? '没有找到相关待审图；可以先补 alias / Pixiv 平台词，或清空搜索查看默认队列。'
    : '当前筛选没有 Pixiv 待审图片';
  document.getElementById('pixivReviewImages').innerHTML = items.length ? items.map(renderPixivReviewCard).join('') : `<div class="empty">${escapeHtml(emptyText)}</div>`;
  renderPixivBatchSelectionHint();
}

function renderPixivReviewSearchContext() {
  const box = document.getElementById('pixivReviewSearchContext');
  if (!box) return;
  const keyword = String(pixivReviewState.searchKeyword || '').trim();
  const context = pixivReviewState.searchContext || {};
  if (!keyword) {
    box.textContent = '先筛选角色相关待审图，再勾选图片做批量审核。';
    return;
  }
  const matchedTags = (context.matched_tags || []).map(item => item.name).filter(Boolean);
  const terms = uniqueTexts(context.expanded_terms || context.terms || []).slice(0, 12);
  const count = Object.keys(pixivReviewState.items || {}).length;
  if (matchedTags.length) {
    box.textContent = `${keyword} 命中：${matchedTags.join('、')}；展开词：${terms.join('、') || '无'}；当前 ${count} 张`;
  } else {
    box.textContent = `${keyword} 未命中主 tag；已按待审 tag / Pixiv 来源词直接模糊搜索；当前 ${count} 张`;
  }
}

function schedulePixivReviewSearch() {
  if (pixivReviewSearchTimer) clearTimeout(pixivReviewSearchTimer);
  pixivReviewSearchTimer = setTimeout(() => {
    loadPixivReviewImages().catch(err => {
      console.error(err);
      showToast(err.message || err);
    });
  }, 420);
}

function handlePixivReviewKeywordKeydown(event) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  if (pixivReviewSearchTimer) clearTimeout(pixivReviewSearchTimer);
  loadPixivReviewImages().catch(err => {
    console.error(err);
    showToast(err.message || err);
  });
}

function clearPixivReviewSearch() {
  const input = document.getElementById('pixivReviewKeyword');
  if (input) input.value = '';
  if (pixivReviewSearchTimer) clearTimeout(pixivReviewSearchTimer);
  loadPixivReviewImages().catch(err => {
    console.error(err);
    showToast(err.message || err);
  });
}

async function loadPixivReviewImages() {
  const keywordInput = document.getElementById('pixivReviewKeyword');
  const keyword = keywordInput ? String(keywordInput.value || '').trim() : '';
  pixivReviewState.searchKeyword = keyword;
  const q = new URLSearchParams({
    status: document.getElementById('pixivReviewStatus').value,
    keyword,
    limit: '24',
  });
  const data = await fetchJson(`/api/pixiv-review-images?${q.toString()}`);
  pixivReviewState.items = {};
  pixivReviewState.searchContext = data.search_context || null;
  pixivReviewState.batchPreview = [];
  for (const item of (data.items || [])) {
    pixivReviewState.items[item.image_id] = item;
    ensurePixivReviewSelection(item);
  }
  prunePixivReviewSelections();
  renderPixivReviewList();
  renderPixivReviewSearchContext();
  renderPixivBatchPreview();
  renderPixivPreview();
}

function parseCsvInput(value) {
  return uniqueTexts(String(value || '').split(/[,\\n，]/g));
}

function selectedPixivReviewImageIds() {
  return Object.keys(pixivReviewState.selectedImages || {}).filter(key => pixivReviewState.selectedImages[key]).map(key => Number(key));
}

function renderPixivBatchSelectionHint() {
  const count = selectedPixivReviewImageIds().length;
  document.getElementById('pixivBatchSelectionHint').textContent = `当前已选 ${count} 张图片。`;
  const box = document.getElementById('pixivBatchBox');
  if (box) {
    box.classList.toggle('is-idle', count === 0);
    box.classList.toggle('is-active', count > 0);
  }
}

function togglePixivReviewImageSelection(imageId) {
  if (pixivReviewState.selectedImages[imageId]) delete pixivReviewState.selectedImages[imageId];
  else pixivReviewState.selectedImages[imageId] = true;
  renderPixivBatchSelectionHint();
  renderPixivReviewList();
}

function selectAllPixivReviewImages() {
  for (const imageId of Object.keys(pixivReviewState.items || {})) {
    pixivReviewState.selectedImages[imageId] = true;
  }
  renderPixivBatchSelectionHint();
  renderPixivReviewList();
}

function clearSelectedPixivReviewImages() {
  pixivReviewState.selectedImages = {};
  pixivReviewState.batchPreview = [];
  renderPixivBatchSelectionHint();
  renderPixivReviewList();
  renderPixivBatchPreview();
}

function applyBatchTagsToSelectedImages() {
  const imageIds = selectedPixivReviewImageIds();
  if (!imageIds.length) {
    alert('请先勾选至少一张图片。');
    return;
  }
  const tags = parseCsvInput(document.getElementById('pixivBatchTags').value);
  const terms = parseCsvInput(document.getElementById('pixivBatchSourceTerms').value);
  if (!tags.length && !terms.length) {
    alert('请至少填写批量归入主 tag 或 alias / 来源词。');
    return;
  }
  for (const imageId of imageIds) {
    if (tags.length) pixivReviewState.selectedTagsByImage[imageId] = [...tags];
    if (terms.length) pixivReviewState.selectedTermsByImage[imageId] = [...terms];
  }
  renderPixivReviewList();
  renderPixivPreview();
}

function buildPixivBatchReviewItems() {
  const imageIds = selectedPixivReviewImageIds();
  const overrideTags = parseCsvInput(document.getElementById('pixivBatchTags').value);
  const overrideTerms = parseCsvInput(document.getElementById('pixivBatchSourceTerms').value);
  return imageIds.map(imageId => ({
    image_id: imageId,
    selected_tag_names: overrideTags.length ? overrideTags : uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []),
    source_terms: overrideTerms.length ? overrideTerms : uniqueTexts(pixivReviewState.selectedTermsByImage[imageId] || []),
  }));
}

function renderPixivBatchPreview() {
  const items = pixivReviewState.batchPreview || [];
  document.getElementById('pixivBatchPreview').innerHTML = items.length ? items.map(item => {
    if (item.status && item.status !== 'ok') {
      return `<div class="item"><strong>#${item.image_id}</strong><div class="muted">${escapeHtml(item.message || '预览失败')}</div></div>`;
    }
    return `
      <div class="item">
        <div><strong>#${item.image_id}</strong></div>
        <div class="muted">归入：${(item.approved_tags || []).map(escapeHtml).join('、') || '无'}；拒绝：${(item.rejected_tags || []).map(escapeHtml).join('、') || '无'}</div>
        <div class="muted">alias / 搜索词：${(item.mapped_terms || []).map(term => `${escapeHtml(term.term)}→${escapeHtml(term.tag_name)}${term.action === 'already' ? '(已存在)' : term.action === 'merge_tag' ? '(合并 tag)' : term.action === 'alias_added' ? '(alias)' : ''}`).join('、') || '无'}</div>
        <div class="muted">跳过：${(item.skipped_terms || []).map(escapeHtml).join('；') || '无'}</div>
      </div>
    `;
  }).join('') : '<div class="muted">暂无批量审核预览</div>';
}

async function previewPixivBatchReview() {
  const items = buildPixivBatchReviewItems();
  if (!items.length) {
    alert('请先勾选至少一张图片。');
    return;
  }
  const result = await fetchJson('/api/pixiv-review/batch-preview', {
    method: 'POST',
    body: JSON.stringify({
      items,
      reject_unselected: document.getElementById('pixivBatchRejectUnselected').checked,
    }),
  });
  pixivReviewState.batchPreview = result.items || [];
  renderPixivBatchPreview();
}

async function submitPixivBatchReview() {
  const items = buildPixivBatchReviewItems();
  if (!items.length) {
    alert('请先勾选至少一张图片。');
    return;
  }
  if (!confirm(`确认批量审核这 ${items.length} 张图片吗？`)) return;
  const result = await fetchJson('/api/pixiv-review/batch-submit', {
    method: 'POST',
    body: JSON.stringify({
      items,
      reject_unselected: document.getElementById('pixivBatchRejectUnselected').checked,
    }),
  });
  const reviewedImageIds = items.map(item => Number(item.image_id || 0)).filter(Boolean);
  pixivReviewState.batchPreview = result.items || [];
  renderPixivBatchPreview();
  await refreshAfterReviewMutation({
    removePixivImageIds: reviewedImageIds,
    toastMessage: result.message || '批量审核完成',
    toastDetail: '已从当前审批队列移除。',
  });
}

function buildCandidateMappingsHtml(item) {
  return (item.candidate_tags || []).map(tag => `
    <div class="mapping-box">
      <div><strong>${escapeHtml(tag.name)}</strong>${tag.is_character ? ' <span class="pill">角色</span>' : ''}</div>
      <div class="muted">alias：${(tag.aliases || []).map(escapeHtml).join('、') || '无'}</div>
      <div class="muted">已有 Pixiv 词：${(tag.platform_terms || []).map(escapeHtml).join('、') || '无'}</div>
      <div class="muted">历史建议：${(tag.suggested_terms || []).map(term => `${escapeHtml(term.term)}(${term.count})`).join('、') || '无'}</div>
    </div>
  `).join('') || '<div class="muted">暂无候选 tag 细节</div>';
}

function renderImagePreview() {
  const modal = document.getElementById('pixivPreviewModal');
  const body = document.getElementById('pixivPreviewBody');
  if (!modal.classList.contains('show') || !imagePreviewState.imageId) {
    body.innerHTML = '';
    return;
  }
  const detail = imagePreviewState.item;
  if (!detail) {
    body.innerHTML = '<div class="empty">正在加载图片详情...</div>';
    return;
  }
  const image = detail.image || {};
  const imageId = Number(image.id || imagePreviewState.imageId);
  const sources = detail.sources || [];
  const source0 = sources[0] || {};
  const extra = source0.extra || {};
  const title = String(extra.title || extra.illust_title || '');
  document.getElementById('pixivPreviewTitle').textContent = `#${imageId} ${image.file_name || ''}`;
  document.getElementById('pixivPreviewSubtitle').textContent = `${image.width || 0}x${image.height || 0} · ${source0.platform || 'local'} · ${source0.author || title || '-'}`;
  body.innerHTML = `
    <div class="modal-image">
      <img src="${api(`/api/image-file?image_id=${imageId}`)}" alt="preview" />
      <div class="subheading">文件</div>
      <div class="item">
        <div>${escapeHtml(image.file_name || '')}</div>
        <div class="muted">${escapeHtml(image.format || '')} · phash: ${escapeHtml(image.phash || '-')}</div>
        <div class="muted">疑似重复: ${(detail.similar_image_ids || []).map(escapeHtml).join(', ') || '无'}</div>
      </div>
    </div>
    <div>
      <div class="subheading">待审批</div>
      ${renderReviewTaskActions(detail.review_tasks || [])}
      <div class="subheading">当前 tag</div>
      <div class="item">${renderImageTags(detail.tags || [])}</div>
      <div class="subheading">来源</div>
      ${renderImageSources(sources)}
    </div>
  `;
}

async function refreshImagePreview() {
  const imageId = imagePreviewState.imageId;
  if (!imageId) return;
  imagePreviewState.item = await fetchJson(`/api/image?image_id=${imageId}`);
  renderImagePreview();
}

async function openImagePreview(imageId) {
  imagePreviewState.imageId = imageId;
  imagePreviewState.item = null;
  pixivReviewState.previewImageId = null;
  document.getElementById('pixivPreviewModal').classList.add('show');
  renderImagePreview();
  try {
    await refreshImagePreview();
  } catch (err) {
    closePixivPreview();
    alert(err.message || err);
  }
}

function renderPixivPreview() {
  if (imagePreviewState.imageId) {
    renderImagePreview();
    return;
  }
  const modal = document.getElementById('pixivPreviewModal');
  const body = document.getElementById('pixivPreviewBody');
  if (!modal.classList.contains('show') || !pixivReviewState.previewImageId) {
    body.innerHTML = '';
    return;
  }
  const item = pixivReviewState.items[pixivReviewState.previewImageId];
  if (!item) {
    body.innerHTML = '<div class="empty">图片不存在</div>';
    return;
  }
  ensurePixivReviewSelection(item);
  const imageId = item.image_id;
  const selectedTags = pixivReviewState.selectedTagsByImage[imageId] || [];
  const selectedTerms = pixivReviewState.selectedTermsByImage[imageId] || [];
  document.getElementById('pixivPreviewTitle').textContent = `#${imageId} ${item.file_name || ''}`;
  document.getElementById('pixivPreviewSubtitle').textContent = `${item.width}x${item.height} · ${item.author || '-'} · ${item.title || '-'}`;
  body.innerHTML = `
    <div class="modal-image">
      <img src="${api(`/api/image-file?image_id=${imageId}`)}" alt="preview" />
      <div class="subheading">来源</div>
      <div class="item">${item.post_url ? `<a href="${escapeAttr(item.post_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.post_url)}</a>` : '-'}</div>
      <div class="subheading">当前图片 tag</div>
      <div class="item">${(item.current_tags || []).map(tag => `<span class="pill">${escapeHtml(tag.name)}(${escapeHtml(tag.review_status)})</span>`).join('') || '<span class="muted">无</span>'}</div>
      <div class="subheading">原始数据</div>
      <pre class="json">${escapeHtml(JSON.stringify({raw_tags: item.raw_tags || [], translated_tags: item.translated_tags || []}, null, 2))}</pre>
    </div>
    <div>
      ${renderCandidateTagSection(item, selectedTags, 'preview')}
      <div class="subheading">alias / Pixiv 来源词</div>
      <div class="chip-row">${(item.source_terms || []).map(term => renderSourceTermChip(imageId, term, selectedTerms.some(name => normalizeKey(name) === normalizeKey(term.term)))).join('') || '<span class="muted">暂无来源 tag</span>'}</div>
      <div class="muted">归入主 tag：${escapeHtml((uniqueTexts(selectedTags)[0] || '无'))}；alias / 搜索词：${selectedTerms.map(escapeHtml).join('、') || '无'}</div>
      <div class="row">
        <button onclick="submitPixivReview(${imageId})">确认审核</button>
        <button class="danger" onclick="rejectPixivReviewImage(${imageId})">拒绝图片</button>
        <button class="secondary" onclick="resetPixivReviewSelection(${imageId})">重置</button>
      </div>
      <div class="subheading">Pixiv 映射信息</div>
      ${buildCandidateMappingsHtml(item)}
    </div>
  `;
}

async function openPixivPreview(imageId) {
  imagePreviewState.imageId = null;
  imagePreviewState.item = null;
  let item = pixivReviewState.items[imageId];
  const detail = await fetchJson(`/api/pixiv-review-image?image_id=${imageId}`);
  item = detail.item || item;
  if (!item) return;
  pixivReviewState.items[imageId] = item;
  ensurePixivReviewSelection(item);
  pixivReviewState.previewImageId = imageId;
  document.getElementById('pixivPreviewModal').classList.add('show');
  renderPixivPreview();
}

function closePixivPreview() {
  pixivReviewState.previewImageId = null;
  imagePreviewState.imageId = null;
  imagePreviewState.item = null;
  document.getElementById('pixivPreviewModal').classList.remove('show');
  renderPixivPreview();
}

async function submitPixivReview(imageId) {
  const selectedTags = uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []);
  if (!selectedTags.length) {
    alert('请至少选择一个归入主 tag。');
    return;
  }
  const sourceTerms = uniqueTexts(pixivReviewState.selectedTermsByImage[imageId] || []);
  const payload = {
    image_id: imageId,
    selected_tag_names: selectedTags,
    source_terms: sourceTerms,
    reject_unselected: true,
  };
  const result = await fetchJson('/api/pixiv-review/submit', {method: 'POST', body: JSON.stringify(payload)});
  const mappedText = (result.mapped_terms || []).map(item => {
    const suffix = item.action === 'merge_tag' ? '(合并 tag)' : item.action === 'alias_added' ? '(alias)' : item.action === 'already' ? '(已存在)' : '';
    return `${item.term}→${item.tag_name}${suffix}`;
  }).join('、');
  const skippedText = (result.skipped_terms || []).join('；');
  delete pixivReviewState.selectedTagsByImage[imageId];
  delete pixivReviewState.selectedTermsByImage[imageId];
  closePixivPreview();
  await refreshAfterReviewMutation({
    removePixivImageIds: [imageId],
    toastMessage: result.message || '已完成审核',
    toastDetail: [mappedText ? `已沉淀：${mappedText}` : '', skippedText ? `未沉淀：${skippedText}` : '已从当前审批队列移除。'].filter(Boolean).join('；'),
  });
}

async function rejectPixivReviewImage(imageId) {
  const result = await fetchJson('/api/pixiv-review/reject-image', {
    method: 'POST',
    body: JSON.stringify({image_id: imageId}),
  });
  delete pixivReviewState.selectedTagsByImage[imageId];
  delete pixivReviewState.selectedTermsByImage[imageId];
  delete pixivReviewState.selectedImages[imageId];
  closePixivPreview();
  await refreshAfterReviewMutation({
    removePixivImageIds: [imageId],
    toastMessage: result.message || '已拒绝图片',
    toastDetail: result.post_url ? `已记录跳过来源：${result.post_url}` : '已从当前审批队列移除。',
  });
}

document.getElementById('pixivPreviewModal').addEventListener('click', (event) => {
  if (event.target.id === 'pixivPreviewModal') closePixivPreview();
});

function syncPixivPlatformSaveButton() {
  const button = document.getElementById('pixivPlatformSaveButton');
  if (!button) return;
  button.textContent = pixivPlatformState.editingTermId ? '更新平台词' : '新增平台词';
}

function prefillPixivPlatformForm(tagName = '', term = '', termType = 'both', source = 'manual_review', confidence = '1', termId = 0) {
  document.getElementById('pixivPlatformTagInput').value = tagName || '';
  document.getElementById('pixivPlatformTermInput').value = term || '';
  document.getElementById('pixivPlatformTypeInput').value = termType || 'both';
  document.getElementById('pixivPlatformSourceInput').value = source || 'manual_review';
  document.getElementById('pixivPlatformConfidenceInput').value = confidence ?? '1';
  pixivPlatformState.editingTermId = Number(termId || 0);
  syncPixivPlatformSaveButton();
}

function resetPixivPlatformForm() {
  prefillPixivPlatformForm(document.getElementById('pixivPlatformTagFilter').value.trim(), '', 'both', 'manual_review', '1', 0);
}

function editPixivPlatformTerm(termId, tagName, term, termType, source, confidence) {
  prefillPixivPlatformForm(tagName, term, termType, source, String(confidence ?? '1'), termId);
}

function currentPixivPlatformSuggestionTag() {
  return document.getElementById('pixivPlatformTagFilter').value.trim() || document.getElementById('pixivPlatformTagInput').value.trim();
}

function selectPixivPlatformTag(tagName) {
  document.getElementById('pixivPlatformTagFilter').value = tagName || '';
  document.getElementById('pixivPlatformTagInput').value = tagName || '';
  document.getElementById('pixivBulkMapTag').value = tagName || '';
  loadPixivPlatformTerms();
  loadPixivPlatformSuggestions(tagName || '');
}

function usePixivTermInForm(term, defaultTag = '') {
  prefillPixivPlatformForm(defaultTag || currentPixivPlatformSuggestionTag(), term, 'both', 'pixiv_history', '0.8', 0);
}

function renderPixivPlatformTermItem(item) {
  return `
    <div class="term-item">
      <div class="term-head">
        <div>
          <div><strong>${escapeHtml(item.term)}</strong> <span class="pill">${escapeHtml(item.term_type)}</span>${item.is_character ? ' <span class="pill">角色</span>' : ''}</div>
          <div class="muted">主 tag：${escapeHtml(item.tag_name)} · 来源：${escapeHtml(item.source || '-')} · 置信度：${Number(item.confidence || 0).toFixed(2)}</div>
        </div>
        <div class="term-actions">
          <button class="secondary mini-btn" onclick='selectPixivPlatformTag(${JSON.stringify(item.tag_name)})'>查看建议</button>
          <button class="secondary mini-btn" onclick='editPixivPlatformTerm(${item.id}, ${JSON.stringify(item.tag_name)}, ${JSON.stringify(item.term)}, ${JSON.stringify(item.term_type)}, ${JSON.stringify(item.source || "manual_review")}, ${JSON.stringify(item.confidence ?? 1)})'>编辑</button>
          <button class="danger mini-btn" onclick="deletePixivPlatformTerm(${item.id})">删除</button>
        </div>
      </div>
      <div class="muted">alias：${(item.aliases || []).map(escapeHtml).join('、') || '无'}</div>
      <div class="muted">更新时间：${escapeHtml(item.updated_at || item.created_at || '-')}</div>
    </div>
  `;
}

function renderPixivPlatformTerms() {
  document.getElementById('pixivPlatformTerms').innerHTML = (pixivPlatformState.items || []).length
    ? pixivPlatformState.items.map(renderPixivPlatformTermItem).join('')
    : '<div class="muted">暂无 Pixiv 平台词</div>';
}

function renderPixivPlatformSuggestionItem(item) {
  const currentTag = pixivPlatformState.suggestionsTag || '';
  return `
    <div class="term-item">
      <div class="term-head">
        <div>
          <div><strong>${escapeHtml(item.term)}</strong> <span class="pill">建议</span></div>
          <div class="muted">历史命中 ${item.count || 0} 次</div>
        </div>
        <div class="term-actions">
          ${currentTag ? `<button class="mini-btn" onclick='quickSavePixivPlatformTerm(${JSON.stringify(currentTag)}, ${JSON.stringify(item.term)}, "both", "pixiv_history")'>采纳到当前 tag</button>` : ''}
          <button class="secondary mini-btn" onclick='usePixivTermInForm(${JSON.stringify(item.term)}, ${JSON.stringify(currentTag)})'>填入表单</button>
        </div>
      </div>
      <div class="muted">候选主 tag：${(item.candidate_tags || []).map(escapeHtml).join('、') || '无'}</div>
    </div>
  `;
}

function renderPixivPlatformSuggestions() {
  const hint = document.getElementById('pixivPlatformSuggestionsHint');
  hint.textContent = pixivPlatformState.suggestionsTag
    ? `当前主 tag：${pixivPlatformState.suggestionsTag}，下面是历史 Pixiv 数据里推荐沉淀的词。`
    : '输入主 tag 后点击“查看建议词”。';
  document.getElementById('pixivPlatformSuggestions').innerHTML = (pixivPlatformState.suggestions || []).length
    ? pixivPlatformState.suggestions.map(renderPixivPlatformSuggestionItem).join('')
    : '<div class="muted">暂无建议词</div>';
}

function selectedPixivBulkMapTerms() {
  return Object.keys(pixivPlatformState.selectedBulkTerms || {}).filter(key => pixivPlatformState.selectedBulkTerms[key]);
}

function renderPixivBulkMapSelectionHint() {
  document.getElementById('pixivBulkMapSelectionHint').textContent = `当前已选 ${selectedPixivBulkMapTerms().length} 个词。`;
}

function togglePixivBulkMapTermSelection(term) {
  const key = normalizeKey(term);
  if (pixivPlatformState.selectedBulkTerms[key]) delete pixivPlatformState.selectedBulkTerms[key];
  else pixivPlatformState.selectedBulkTerms[key] = term;
  renderPixivBulkMapSelectionHint();
  renderPixivPlatformUnresolved();
}

function clearPixivBulkMapSelection() {
  pixivPlatformState.selectedBulkTerms = {};
  pixivPlatformState.bulkPreview = null;
  renderPixivBulkMapSelectionHint();
  renderPixivPlatformUnresolved();
  renderPixivBulkMapPreview();
}

function currentPixivBulkMapTerms() {
  return uniqueTexts([
    ...selectedPixivBulkMapTerms().map(key => pixivPlatformState.selectedBulkTerms[key]),
    ...parseCsvInput(document.getElementById('pixivBulkMapTerms').value),
  ]);
}

function renderPixivPlatformUnresolvedItem(item) {
  const selected = !!pixivPlatformState.selectedBulkTerms[normalizeKey(item.term)];
  return `
    <div class="term-item ${selected ? 'selected' : ''}">
      <div class="term-head">
        <div>
          <div><label><input type="checkbox" ${selected ? 'checked' : ''} onchange='togglePixivBulkMapTermSelection(${JSON.stringify(item.term)})' /> <strong>${escapeHtml(item.term)}</strong></label> <span class="pill">待确认</span></div>
          <div class="muted">出现 ${item.count || 0} 次 · 候选主 tag：${(item.candidate_tags || []).map(escapeHtml).join('、') || '无'}</div>
        </div>
        <div class="term-actions">
          ${((item.candidate_tags || []).slice(0, 3)).map(tagName => `<button class="mini-btn" onclick='quickSavePixivPlatformTerm(${JSON.stringify(tagName)}, ${JSON.stringify(item.term)}, "both", "pixiv_history")'>映射到 ${escapeHtml(tagName)}</button>`).join('')}
          <button class="secondary mini-btn" onclick='usePixivTermInForm(${JSON.stringify(item.term)})'>填入表单</button>
        </div>
      </div>
      <div class="muted">作者：${(item.sample_authors || []).map(escapeHtml).join('、') || '无'}</div>
      <div class="sample-links">${(item.sample_post_urls || []).map(url => `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>`).join('') || '<span class="muted">暂无示例链接</span>'}</div>
    </div>
  `;
}

function renderPixivPlatformUnresolved() {
  document.getElementById('pixivPlatformUnresolved').innerHTML = (pixivPlatformState.unresolved || []).length
    ? pixivPlatformState.unresolved.map(renderPixivPlatformUnresolvedItem).join('')
    : '<div class="muted">暂无未解决词</div>';
  renderPixivBulkMapSelectionHint();
}

function renderPixivBulkMapPreview() {
  const payload = pixivPlatformState.bulkPreview;
  const box = document.getElementById('pixivBulkMapPreview');
  if (!payload || !(payload.items || []).length) {
    box.innerHTML = '<div class="muted">暂无批量映射预览</div>';
    return;
  }
  box.innerHTML = (payload.items || []).map(item => `
    <div class="item">
      <div><strong>${escapeHtml(item.term)}</strong> → ${escapeHtml(item.target_tag || payload.target_tag || '-')}</div>
      <div class="muted">状态：${escapeHtml(item.status || '-')} · 次数：${item.count || 0} · ${escapeHtml(item.message || '')}</div>
      <div class="muted">作者：${(item.sample_authors || []).map(escapeHtml).join('、') || '无'}</div>
    </div>
  `).join('');
}

async function loadPixivPlatformTerms() {
  const q = new URLSearchParams({
    tag_name: document.getElementById('pixivPlatformTagFilter').value,
    keyword: document.getElementById('pixivPlatformKeyword').value,
    term_type: document.getElementById('pixivPlatformTypeFilter').value,
    limit: '80',
  });
  const data = await fetchJson(`/api/pixiv-platform-terms?${q.toString()}`);
  pixivPlatformState.items = data.items || [];
  renderPixivPlatformTerms();
}

async function loadPixivPlatformSuggestions(tagName = '') {
  const target = String(tagName || currentPixivPlatformSuggestionTag()).trim();
  if (!target) {
    pixivPlatformState.suggestionsTag = '';
    pixivPlatformState.suggestions = [];
    renderPixivPlatformSuggestions();
    return;
  }
  const data = await fetchJson(`/api/pixiv-platform-suggestions?tag_name=${encodeURIComponent(target)}&limit=20`);
  const canonicalTag = (data.tag && data.tag.name) || target;
  pixivPlatformState.suggestionsTag = canonicalTag;
  pixivPlatformState.suggestions = data.items || [];
  document.getElementById('pixivPlatformTagFilter').value = canonicalTag;
  document.getElementById('pixivBulkMapTag').value = canonicalTag;
  if (!document.getElementById('pixivPlatformTagInput').value.trim()) {
    document.getElementById('pixivPlatformTagInput').value = canonicalTag;
  }
  const aliases = (data.tag && data.tag.aliases) ? data.tag.aliases.join('、') : '';
  document.getElementById('pixivPlatformSuggestionsHint').textContent = `当前主 tag：${canonicalTag}${data.tag && data.tag.is_character ? '（角色）' : ''}${aliases ? ` · alias：${aliases}` : ''}`;
  renderPixivPlatformSuggestions();
}

async function loadPixivPlatformUnresolved() {
  const q = new URLSearchParams({
    keyword: document.getElementById('pixivPlatformKeyword').value,
    limit: '30',
  });
  const data = await fetchJson(`/api/pixiv-platform-unresolved?${q.toString()}`);
  pixivPlatformState.unresolved = data.items || [];
  renderPixivPlatformUnresolved();
}

async function previewPixivBulkMap() {
  const tagName = document.getElementById('pixivBulkMapTag').value.trim();
  const terms = currentPixivBulkMapTerms();
  if (!tagName || !terms.length) {
    alert('请先填写目标主 tag，并勾选或输入至少一个 Pixiv 词。');
    return;
  }
  const result = await fetchJson('/api/pixiv-platform/batch-preview', {
    method: 'POST',
    body: JSON.stringify({
      tag_name: tagName,
      terms,
      term_type: document.getElementById('pixivBulkMapType').value,
    }),
  });
  pixivPlatformState.bulkPreview = result;
  renderPixivBulkMapPreview();
}

async function submitPixivBulkMap() {
  const tagName = document.getElementById('pixivBulkMapTag').value.trim();
  const terms = currentPixivBulkMapTerms();
  if (!tagName || !terms.length) {
    alert('请先填写目标主 tag，并勾选或输入至少一个 Pixiv 词。');
    return;
  }
  if (!confirm(`确认将 ${terms.length} 个 Pixiv 词批量映射到 ${tagName} 吗？`)) return;
  const confidence = Number(document.getElementById('pixivBulkMapConfidence').value || '0.8');
  const result = await fetchJson('/api/pixiv-platform/batch-submit', {
    method: 'POST',
    body: JSON.stringify({
      tag_name: tagName,
      terms,
      term_type: document.getElementById('pixivBulkMapType').value,
      source: document.getElementById('pixivBulkMapSource').value,
      confidence,
    }),
  });
  alert(result.message || '批量映射完成');
  clearPixivBulkMapSelection();
  document.getElementById('pixivBulkMapTag').value = tagName;
  await refreshAfterPlatformMutation(tagName);
}

async function savePixivPlatformTerm() {
  const tagName = document.getElementById('pixivPlatformTagInput').value.trim();
  const term = document.getElementById('pixivPlatformTermInput').value.trim();
  const confidenceText = document.getElementById('pixivPlatformConfidenceInput').value.trim();
  if (!tagName || !term) {
    alert('请先填写主 tag 和 Pixiv 词。');
    return;
  }
  const confidence = confidenceText === '' ? 1 : Number(confidenceText);
  if (Number.isNaN(confidence)) {
    alert('置信度格式不正确。');
    return;
  }
  const result = await fetchJson('/api/pixiv-platform-terms/save', {
    method: 'POST',
    body: JSON.stringify({
      term_id: pixivPlatformState.editingTermId || 0,
      tag_name: tagName,
      term,
      term_type: document.getElementById('pixivPlatformTypeInput').value,
      source: document.getElementById('pixivPlatformSourceInput').value,
      confidence,
    }),
  });
  alert(result.message || '已保存平台词');
  document.getElementById('pixivPlatformTagFilter').value = tagName;
  resetPixivPlatformForm();
  await refreshAfterPlatformMutation(tagName);
}

async function deletePixivPlatformTerm(termId) {
  if (!confirm('确认删除这条 Pixiv 平台词吗？')) return;
  const result = await fetchJson('/api/pixiv-platform-terms', {
    method: 'DELETE',
    body: JSON.stringify({term_id: termId}),
  });
  alert(result.message || '已删除平台词');
  if (pixivPlatformState.editingTermId === Number(termId || 0)) resetPixivPlatformForm();
  await refreshAfterPlatformMutation();
}

async function quickSavePixivPlatformTerm(tagName, term, termType = 'both', source = 'pixiv_history') {
  const resolvedTag = String(tagName || currentPixivPlatformSuggestionTag()).trim();
  if (!resolvedTag) {
    alert('请先指定主 tag。');
    usePixivTermInForm(term);
    return;
  }
  const result = await fetchJson('/api/pixiv-platform-terms/save', {
    method: 'POST',
    body: JSON.stringify({
      tag_name: resolvedTag,
      term,
      term_type: termType,
      source,
      confidence: 0.8,
    }),
  });
  alert(result.message || '已保存平台词');
  document.getElementById('pixivPlatformTagFilter').value = resolvedTag;
  await refreshAfterPlatformMutation(resolvedTag);
}

function currentTagMergeSources() {
  return parseCsvInput(document.getElementById('tagMergeSources').value);
}

function useTagMergeCandidate(sourceTag, targetTag) {
  document.getElementById('tagMergeTarget').value = targetTag || '';
  const current = currentTagMergeSources();
  if (!current.some(item => normalizeKey(item) === normalizeKey(sourceTag))) current.push(sourceTag);
  document.getElementById('tagMergeSources').value = current.join(', ');
  previewTagMerge();
}

function renderTagMergeCandidateItem(item) {
  return `
    <div class="term-item">
      <div class="term-head">
        <div>
          <div><strong>${escapeHtml(item.source_tag)}</strong> <span class="merge-arrow">→</span> <strong>${escapeHtml(item.target_tag)}</strong></div>
          <div class="muted">分数：${item.score || 0} · source图数：${item.source_image_count || 0} · target图数：${item.target_image_count || 0}</div>
        </div>
        <div class="term-actions">
          <button onclick='useTagMergeCandidate(${JSON.stringify(item.source_tag)}, ${JSON.stringify(item.target_tag)})'>使用并预览</button>
        </div>
      </div>
      <div class="muted">原因：${(item.reasons || []).map(escapeHtml).join('、') || '无'}；示例词：${(item.example_terms || []).map(escapeHtml).join('、') || '无'}</div>
    </div>
  `;
}

function renderTagMergeCandidates() {
  document.getElementById('tagMergeCandidates').innerHTML = (tagMergeState.candidates || []).length
    ? tagMergeState.candidates.map(renderTagMergeCandidateItem).join('')
    : '<div class="muted">暂无候选归并对</div>';
}

function renderTagMergePreview() {
  const payload = tagMergeState.preview;
  const box = document.getElementById('tagMergePreview');
  if (!payload || !(payload.items || []).length) {
    box.innerHTML = '<div class="muted">暂无归并预览</div>';
    return;
  }
  const totals = payload.totals || {};
  box.innerHTML = `
    <div class="preview-box">
      <div><strong>目标主 tag：</strong>${escapeHtml(payload.target_tag || '-')}</div>
      <div class="muted">target alias：${(payload.target_aliases || []).map(escapeHtml).join('、') || '无'}</div>
      <div class="muted">总计：图片关联 ${totals.image_links || 0}（冲突 ${totals.image_link_collisions || 0}）；
      审核任务 ${totals.review_tasks || 0}（冲突 ${totals.review_task_collisions || 0}）；
      alias ${totals.aliases || 0}；平台词 ${totals.platform_terms || 0}；自动订阅 ${totals.subscriptions || 0}</div>
    </div>
    ${(payload.items || []).map(item => `
      <div class="item">
        <div><strong>${escapeHtml(item.source_tag || '-')}</strong> <span class="merge-arrow">→</span> ${escapeHtml(item.target_tag || payload.target_tag || '-')}</div>
        <div class="muted">${escapeHtml(item.message || '-')}</div>
        <div class="muted">图片关联 ${item.image_links || 0}（冲突 ${item.image_link_collisions || 0}）；
        审核任务 ${item.review_tasks || 0}（冲突 ${item.review_task_collisions || 0}）；
        alias ${item.alias_count || 0}；平台词 ${item.platform_term_count || 0}；自动订阅 ${item.subscription_count || 0}</div>
        <div class="muted">alias：${(item.aliases || []).map(escapeHtml).join('、') || '无'}</div>
        <div class="muted">平台词：${(item.platform_terms || []).map(escapeHtml).join('、') || '无'}</div>
      </div>
    `).join('')}
  `;
}

async function loadTagMergeCandidates() {
  const q = new URLSearchParams({
    keyword: document.getElementById('tagMergeKeyword').value,
    limit: '40',
  });
  const data = await fetchJson(`/api/tag-merge/candidates?${q.toString()}`);
  tagMergeState.candidates = data.items || [];
  renderTagMergeCandidates();
}

async function previewTagMerge() {
  const targetTag = document.getElementById('tagMergeTarget').value.trim();
  const sourceTags = currentTagMergeSources();
  if (!targetTag || !sourceTags.length) {
    alert('请先填写目标主 tag 和来源 tag。');
    return;
  }
  const data = await fetchJson('/api/tag-merge/preview', {
    method: 'POST',
    body: JSON.stringify({
      target_tag: targetTag,
      source_tags: sourceTags,
    }),
  });
  tagMergeState.preview = data;
  renderTagMergePreview();
}

async function executeTagMerge() {
  const targetTag = document.getElementById('tagMergeTarget').value.trim();
  const sourceTags = currentTagMergeSources();
  if (!targetTag || !sourceTags.length) {
    alert('请先填写目标主 tag 和来源 tag。');
    return;
  }
  if (!confirm(`确认将 ${sourceTags.join('、')} 归并到 ${targetTag} 吗？`)) return;
  const data = await fetchJson('/api/tag-merge/execute', {
    method: 'POST',
    body: JSON.stringify({
      target_tag: targetTag,
      source_tags: sourceTags,
    }),
  });
  const lines = [data.message || '归并完成'];
  if ((data.merged_tags || []).length) lines.push(`已归并：${data.merged_tags.join('、')}`);
  if ((data.aliases_added || []).length) lines.push(`直接挂 alias：${data.aliases_added.join('、')}`);
  alert(lines.join('\\n'));
  tagMergeState.preview = null;
  renderTagMergePreview();
  await refreshAfterTagMergeMutation();
}

resetPixivPlatformForm();
clearPixivBulkMapSelection();
renderPixivReviewSearchContext();
renderPixivBatchSelectionHint();
renderPixivBatchPreview();
renderTagMergePreview();
renderPixivBulkMapSelectionHint();
renderPixivBulkMapPreview();
window.addEventListener('hashchange', () => {
  showPage(normalizePageName(location.hash.replace(/^#/, '') || 'overview'), {updateHash: false}).catch(err => {
    console.error(err);
    alert(err.message || err);
  });
});
showPage(normalizePageName(location.hash.replace(/^#/, '') || 'overview'), {updateHash: false}).catch(err => { console.error(err); alert(err.message || err); });
</script>
</body>
</html>
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>PJSK 图片库登录</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #eef2ff; font-family: Arial, sans-serif; color: #1f2937; }
    .panel { width: min(420px, calc(100vw - 32px)); background: white; border-radius: 16px; padding: 24px; box-shadow: 0 18px 48px rgba(37, 99, 235, .18); }
    h1 { margin: 0 0 10px; font-size: 24px; }
    p { margin: 0 0 18px; color: #4b5563; line-height: 1.6; }
    input, button { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 10px; border: 1px solid #cfd8ff; font-size: 14px; }
    input { margin-bottom: 12px; }
    button { border: none; background: #3c65f5; color: white; cursor: pointer; }
    .hint { margin-top: 12px; font-size: 12px; color: #6b7280; }
    .error { min-height: 20px; margin-top: 10px; color: #dc2626; font-size: 13px; }
  </style>
</head>
<body>
  <div class="panel">
    <h1>PJSK 图片库登录</h1>
    <p>当前 WebUI 已启用访问令牌保护。请输入 <code>webui_access_token</code> 完成登录，浏览器会建立站点会话，不再通过 URL 传递令牌。</p>
    <form id="loginForm">
      <input id="tokenInput" type="password" placeholder="输入访问令牌" autocomplete="current-password" />
      <button type="submit">登录</button>
    </form>
    <div class="error" id="errorText"></div>
    <div class="hint">仍可继续使用 <code>X-PJSK-Token</code> 或 <code>Authorization: Bearer</code> 直接调用 API。</div>
  </div>
  <script>
    const form = document.getElementById('loginForm');
    const input = document.getElementById('tokenInput');
    const errorText = document.getElementById('errorText');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      errorText.textContent = '';
      const token = input.value.trim();
      if (!token) {
        errorText.textContent = '请输入访问令牌。';
        return;
      }
      try {
        const resp = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          credentials: 'same-origin',
          body: JSON.stringify({token}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.ok === false) throw new Error(data.message || '登录失败');
        location.href = '/';
      } catch (err) {
        errorText.textContent = err.message || String(err);
      }
    });
    input.focus();
  </script>
</body>
</html>
"""


class GalleryWebUI:
    def __init__(
        self,
        db: ImageIndexDB,
        crawl_service,
        *,
        pixiv_backfill_service: PixivBackfillService | None = None,
        context=None,
        config=None,
    ) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.pixiv_backfill_service = pixiv_backfill_service or PixivBackfillService(
            db=db,
            crawl_service=crawl_service,
            config=config if config is not None else getattr(crawl_service, "config", {}),
        )
        self.context = context
        self.config = config if config is not None else getattr(crawl_service, "config", {})
        self.host = "0.0.0.0"
        self.port = 9099
        self.access_token = ""
        self._access_cookie_name = "pjsk_pic_webui_token"
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._actual_port: int | None = None

    @property
    def is_running(self) -> bool:
        return self._runner is not None

    async def start(self, *, host: str, port: int, access_token: str = "") -> None:
        self.host = str(host or "0.0.0.0").strip() or "0.0.0.0"
        self.port = max(1, int(port or 9099))
        self.access_token = str(access_token or "").strip()

        if self.is_running:
            return

        app = web.Application()
        routes = [
                web.get("/", self.ui_page),
                web.post("/api/auth/login", self.api_auth_login),
                web.post("/api/auth/logout", self.api_auth_logout),
                web.get("/api/summary", self.api_summary),
                web.get("/api/images", self.api_images),
                web.get("/api/image", self.api_image_detail),
                web.get("/api/image-file", self.api_image_file),
                web.get("/api/tags", self.api_tags),
                web.get("/api/jobs", self.api_jobs),
                web.post("/api/jobs", self.api_jobs),
                web.post("/api/jobs/pixiv-search-preview", self.api_jobs_pixiv_search_preview),
                web.get("/api/jobs/pixiv-backfill", self.api_jobs_pixiv_backfill),
                web.post("/api/jobs/pixiv-backfill", self.api_jobs_pixiv_backfill),
                web.post("/api/jobs/pixiv-backfill/retry", self.api_jobs_pixiv_backfill_retry),
                web.post("/api/jobs/retry", self.api_jobs_retry),
                web.get("/api/reviews", self.api_reviews),
                web.get("/api/pixiv-review-images", self.api_pixiv_review_images),
                web.get("/api/pixiv-review-image", self.api_pixiv_review_image),
                web.post("/api/pixiv-review/submit", self.api_pixiv_review_submit),
                web.post("/api/pixiv-review/reject-image", self.api_pixiv_review_reject_image),
                web.post("/api/pixiv-review/batch-preview", self.api_pixiv_review_batch_preview),
                web.post("/api/pixiv-review/batch-submit", self.api_pixiv_review_batch_submit),
                web.get("/api/pixiv-platform-terms", self.api_pixiv_platform_terms),
                web.post("/api/pixiv-platform-terms/save", self.api_pixiv_platform_terms_save),
                web.delete("/api/pixiv-platform-terms", self.api_pixiv_platform_terms_delete),
                web.get("/api/pixiv-platform-suggestions", self.api_pixiv_platform_suggestions),
                web.get("/api/pixiv-platform-unresolved", self.api_pixiv_platform_unresolved),
                web.post("/api/pixiv-platform/batch-preview", self.api_pixiv_platform_batch_preview),
                web.post("/api/pixiv-platform/batch-submit", self.api_pixiv_platform_batch_submit),
                web.get("/api/tag-merge/candidates", self.api_tag_merge_candidates),
                web.get("/api/tag-merge/pending-candidates", self.api_tag_merge_pending_candidates),
                web.post("/api/tag-merge/identity-scan", self.api_tag_merge_identity_scan),
                web.post("/api/tag-merge/candidate/ignore", self.api_tag_merge_candidate_ignore),
                web.post("/api/tag-merge/preview", self.api_tag_merge_preview),
                web.post("/api/tag-merge/execute", self.api_tag_merge_execute),
                web.post("/api/reviews/decision", self.api_review_decision),
                web.post("/api/tag/create", self.api_tag_create),
                web.post("/api/tag/alias", self.api_tag_alias),
                web.delete("/api/tag/alias", self.api_tag_alias),
                web.post("/api/tag/character", self.api_tag_character),
            ]
        assets_dir = WEBUI_STATIC_DIR / "assets"
        if assets_dir.exists():
            routes.append(web.static("/assets", assets_dir))
        app.add_routes(routes)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host=self.host, port=self.port)
        await site.start()

        self._runner = runner
        self._site = site
        sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
        if sockets:
            self._actual_port = int(sockets[0].getsockname()[1])
        else:
            self._actual_port = self.port

        for url in self.get_access_urls():
            logger.info(f"[PJSKPic] 独立 WebUI 已启动: {url}")
        if self.host in {"0.0.0.0", "::"} and not self.access_token:
            logger.warning("[PJSKPic] 独立 WebUI 当前对局域网开放且未配置 webui_access_token，请注意访问安全。")

    async def stop(self) -> None:
        if self._runner is None:
            return
        try:
            await self._runner.cleanup()
        finally:
            self._runner = None
            self._site = None
            self._actual_port = None

    def get_access_urls(self) -> list[str]:
        port = self._actual_port or self.port

        if self.host in {"0.0.0.0", "::"}:
            urls = [f"http://127.0.0.1:{port}/"]
            lan_ip = self._detect_lan_ip()
            if lan_ip:
                urls.insert(0, f"http://{lan_ip}:{port}/")
            return urls
        return [f"http://{self.host}:{port}/"]

    @staticmethod
    def _detect_lan_ip() -> str:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return ""
        finally:
            if sock:
                sock.close()

    def _request_token(self, request: web.Request) -> str:
        return (
            request.cookies.get(self._access_cookie_name, "")
            or request.headers.get("X-PJSK-Token", "")
            or self._bearer_token(request.headers.get("Authorization", ""))
        )

    def _is_authorized(self, request: web.Request) -> bool:
        if not self.access_token:
            return True
        return self._request_token(request) == self.access_token

    def _check_access(self, request: web.Request) -> web.Response | None:
        if self._is_authorized(request):
            return None
        return self._json_response({"ok": False, "message": "forbidden"}, status=403)

    @staticmethod
    def _bearer_token(value: str) -> str:
        text = str(value or "").strip()
        if text.lower().startswith("bearer "):
            return text[7:].strip()
        return ""

    def _json_response(self, payload: dict, *, status: int = 200) -> web.Response:
        return web.Response(
            text=json.dumps(payload, ensure_ascii=False),
            status=status,
            content_type="application/json",
            charset="utf-8",
        )

    def _apply_access_cookie(self, response: web.StreamResponse, *, token: str | None = None, clear: bool = False) -> None:
        if clear:
            response.del_cookie(self._access_cookie_name, path="/")
            return
        response.set_cookie(
            self._access_cookie_name,
            token or "",
            path="/",
            httponly=True,
            samesite="Lax",
            max_age=30 * 24 * 60 * 60,
        )

    async def _json_body(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _dedupe_texts(values: list[str] | None) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw in values or []:
            text = str(raw or "").strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(text)
        return result

    @staticmethod
    def _pick_source(detail: dict[str, Any], platform: str) -> dict[str, Any]:
        sources = detail.get("sources", []) if isinstance(detail, dict) else []
        platform_text = str(platform or "").strip().lower()
        for source in sources:
            if str(source.get("platform", "") or "").strip().lower() == platform_text:
                return source
        return sources[0] if sources else {}

    def _build_source_term_payload(
        self,
        term: str,
        origin: str,
        *,
        cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = str(term or "").strip()
        cache_key = (str(origin or "raw"), normalize_tag_name(text))
        if cache is not None and cache_key in cache:
            return dict(cache[cache_key])
        platform_match = self.db.resolve_platform_term("pixiv", text)
        resolved_tag_name = ""
        resolution = ""
        candidates: list[str] = []
        match_type = ""
        if platform_match.matched and platform_match.tag_name:
            resolved_tag_name = str(platform_match.tag_name)
            match_type = str(platform_match.match_type or "")
            resolution = f"已映射到主 tag：{resolved_tag_name}"
        else:
            direct_match = self.db.resolve_tag(text, allow_fuzzy=True, candidate_limit=5)
            if direct_match.matched and direct_match.tag_name:
                resolved_tag_name = str(direct_match.tag_name)
                match_type = str(direct_match.match_type or "")
                resolution = f"命中现有主 tag：{resolved_tag_name}"
            else:
                candidates = self._dedupe_texts(direct_match.candidates)
                resolution = "未命中现有主 tag"
                if candidates:
                    resolution += "；可参考：" + "、".join(candidates)
        payload = {
            "term": text,
            "origin": str(origin or "raw"),
            "resolved_tag_name": resolved_tag_name,
            "resolution": resolution,
            "match_type": match_type,
            "candidate_tags": candidates,
        }
        if cache is not None:
            cache[cache_key] = dict(payload)
        return payload

    def _build_candidate_tag_payload(
        self,
        tag_name: str,
        *,
        cache: dict[str, dict[str, Any] | None] | None = None,
        compact: bool = False,
    ) -> dict[str, Any] | None:
        normalized = normalize_tag_name(tag_name)
        if cache is not None and normalized in cache:
            cached = cache[normalized]
            return dict(cached) if cached else None
        row = self.db.get_tag_row(tag_name)
        if not row:
            if cache is not None:
                cache[normalized] = None
            return None
        canonical_name = str(row["name"])
        payload = {
            "id": int(row["id"]),
            "name": canonical_name,
            "is_character": bool(row["is_character"]),
        }
        if not compact:
            payload.update(
                {
                    "aliases": self.db.list_aliases(canonical_name),
                    "platform_terms": self.db.get_platform_terms_for_tag(
                        tag_name=canonical_name,
                        platform="pixiv",
                        purpose="match",
                        include_aliases=False,
                        include_primary=False,
                    ),
                    "suggested_terms": self.db.suggest_platform_terms_for_tag(
                        tag_name=canonical_name,
                        platform="pixiv",
                        limit=8,
                    ),
                }
            )
        if cache is not None:
            cache[normalized] = dict(payload)
        return payload

    def _candidate_tag_names_for_term(self, term: str) -> list[str]:
        match = self.db.resolve_tag(term, allow_fuzzy=True, candidate_limit=5)
        resolved: list[str] = []
        seen: set[str] = set()

        def push(name: str) -> None:
            text = str(name or "").strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen:
                return
            seen.add(normalized)
            resolved.append(text)

        if match.matched and match.tag_name:
            push(str(match.tag_name))
        for candidate in match.candidates:
            push(candidate)
        return resolved

    def _build_platform_term_item(self, row: Any) -> dict[str, Any]:
        tag_name = str(row["tag_name"])
        tag_row = self.db.get_tag_row(tag_name)
        return {
            "id": int(row["id"]),
            "tag_id": int(row["tag_id"]),
            "tag_name": tag_name,
            "platform": str(row["platform"] or ""),
            "term": str(row["term"] or ""),
            "normalized_term": str(row["normalized_term"] or ""),
            "term_type": str(row["term_type"] or "both"),
            "source": str(row["source"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "aliases": self.db.list_aliases(tag_name),
            "is_character": bool(tag_row["is_character"]) if tag_row else False,
        }

    def _build_platform_suggestion_item(self, item: dict[str, Any]) -> dict[str, Any]:
        term = str(item.get("term", "") or "").strip()
        return {
            "term": term,
            "normalized_term": str(item.get("normalized_term", "") or ""),
            "count": int(item.get("count", 0) or 0),
            "candidate_tags": self._candidate_tag_names_for_term(term),
        }

    def _build_unresolved_platform_term_item(self, item: dict[str, Any]) -> dict[str, Any]:
        term = str(item.get("term", "") or "").strip()
        return {
            "term": term,
            "normalized_term": str(item.get("normalized_term", "") or ""),
            "count": int(item.get("count", 0) or 0),
            "candidate_tags": self._candidate_tag_names_for_term(term),
            "sample_post_urls": self._dedupe_texts(list(item.get("sample_post_urls", []) or [])),
            "sample_authors": self._dedupe_texts(list(item.get("sample_authors", []) or [])),
        }

    def _build_review_task_payloads(
        self,
        image_id: int,
        *,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": int(row["id"]),
                "status": str(row["status"]),
                "reason": str(row["reason"] or ""),
                "manual_result": str(row["manual_result"] or ""),
                "model_result": str(row["model_result"] or ""),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "tag_id": int(row["tag_id"]),
                "tag_name": str(row["tag_name"]),
                "is_character": bool(row["is_character"]),
                "source_type": str(row["source_type"] or ""),
            }
            for row in self.db.get_review_tasks_for_image(image_id, statuses=statuses)
        ]

    def _build_pixiv_review_item(
        self,
        image_id: int,
        *,
        review_statuses: tuple[str, ...] | None = None,
        source_term_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
        candidate_tag_cache: dict[str, dict[str, Any] | None] | None = None,
        compact: bool = False,
    ) -> dict[str, Any] | None:
        detail = self.db.get_image_detail(image_id, sync_files=not compact)
        if not detail:
            return None
        image = detail.get("image", {})
        source = self._pick_source(detail, "pixiv")
        if not source or str(source.get("platform", "") or "").strip().lower() != "pixiv":
            return None
        extra = source.get("extra", {}) if isinstance(source.get("extra"), dict) else {}
        raw_tags = self._dedupe_texts(source.get("raw_tags", []))
        translated_tags = self._dedupe_texts(extra.get("translated_tags", []))

        source_terms: list[dict[str, Any]] = []
        if not compact:
            seen_terms: set[str] = set()
            for origin, terms in (("raw", raw_tags), ("translated", translated_tags)):
                for term in terms:
                    normalized = normalize_tag_name(term)
                    if not normalized or normalized in seen_terms:
                        continue
                    seen_terms.add(normalized)
                    source_terms.append(self._build_source_term_payload(term, origin, cache=source_term_cache))

        review_tasks = self._build_review_task_payloads(image_id, statuses=review_statuses)

        current_tags = [
            {
                "name": str(tag.get("name", "") or ""),
                "is_character": bool(tag.get("is_character", False)),
                "source_type": str(tag.get("source_type", "") or ""),
                "review_status": str(tag.get("review_status", "") or ""),
                "review_reason": str(tag.get("review_reason", "") or ""),
                "score": float(tag.get("score", 0.0) or 0.0),
            }
            for tag in detail.get("tags", [])
            if str(tag.get("name", "") or "").strip()
        ]

        candidate_names: list[str] = []
        seen_candidates: set[str] = set()

        def push_candidate(name: str) -> None:
            text = str(name or "").strip()
            normalized = normalize_tag_name(text)
            if not text or not normalized or normalized in seen_candidates:
                return
            seen_candidates.add(normalized)
            candidate_names.append(text)

        for task in review_tasks:
            if task["status"] != "manual_rejected":
                push_candidate(task["tag_name"])
        for tag in current_tags:
            if tag["review_status"] != "manual_rejected":
                push_candidate(tag["name"])
        for term in source_terms:
            if term["resolved_tag_name"]:
                push_candidate(term["resolved_tag_name"])
            for candidate in term["candidate_tags"][:3]:
                push_candidate(candidate)

        candidate_tags: list[dict[str, Any]] = []
        for name in candidate_names[:12]:
            payload = self._build_candidate_tag_payload(name, cache=candidate_tag_cache, compact=True)
            if payload:
                candidate_tags.append(payload)

        return {
            "image_id": int(image.get("id") or image_id),
            "file_name": str(image.get("file_name", "") or ""),
            "width": int(image.get("width") or 0),
            "height": int(image.get("height") or 0),
            "format": str(image.get("format", "") or ""),
            "phash": str(image.get("phash", "") or ""),
            "updated_at": str(image.get("updated_at", "") or ""),
            "author": str(source.get("author", "") or ""),
            "post_url": str(source.get("post_url", "") or ""),
            "image_url": str(source.get("image_url", "") or ""),
            "title": str(extra.get("title") or extra.get("illust_title") or ""),
            "raw_tags": raw_tags,
            "translated_tags": translated_tags,
            "source_terms": source_terms,
            "candidate_tags": candidate_tags,
            "review_tasks": review_tasks,
            "current_tags": current_tags,
            "compact": compact,
        }

    async def ui_page(self, request: web.Request) -> web.Response:
        if self.access_token and not self._is_authorized(request):
            return web.Response(text=LOGIN_PAGE, content_type="text/html", charset="utf-8")
        static_index = WEBUI_STATIC_DIR / "index.html"
        if static_index.exists():
            return web.FileResponse(static_index, headers={"Cache-Control": "no-store"})
        return web.Response(text=HTML_PAGE, content_type="text/html", charset="utf-8")

    async def api_auth_login(self, request: web.Request) -> web.Response:
        if not self.access_token:
            response = self._json_response({"ok": True, "message": "当前未启用访问令牌。"})
            self._apply_access_cookie(response, clear=True)
            return response
        data = await self._json_body(request)
        token = str(data.get("token", "") or "").strip()
        if token != self.access_token:
            return self._json_response({"ok": False, "message": "访问令牌错误。"}, status=403)
        response = self._json_response({"ok": True, "message": "登录成功。"})
        self._apply_access_cookie(response, token=self.access_token)
        return response

    async def api_auth_logout(self, request: web.Request) -> web.Response:
        response = self._json_response({"ok": True, "message": "已退出登录。"})
        self._apply_access_cookie(response, clear=True)
        return response

    async def api_summary(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        return self._json_response(self.db.get_stats())

    async def api_images(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        args = request.query
        limit, offset = _pagination_from_query(args, default_limit=30, max_limit=100)
        total = self.db.count_search_images(
            keyword=args.get("keyword", ""),
            review_status=args.get("review_status", ""),
            tag_name=args.get("tag", ""),
            platform=args.get("platform", ""),
        )
        if total and offset >= total:
            page_count = max(1, (total + limit - 1) // limit)
            offset = (page_count - 1) * limit
        rows = self.db.search_images(
            keyword=args.get("keyword", ""),
            review_status=args.get("review_status", ""),
            tag_name=args.get("tag", ""),
            platform=args.get("platform", ""),
            limit=limit,
            offset=offset,
        )
        items: list[dict] = []
        for row in rows:
            image_id = int(row["id"])
            detail = self.db.get_image_detail(image_id, sync_files=False) or {}
            sources = detail.get("sources", [])
            source0 = sources[0] if sources else {}
            items.append(
                {
                    "id": image_id,
                    "file_name": str(row["file_name"]),
                    "width": int(row["width"] or 0),
                    "height": int(row["height"] or 0),
                    "format": str(row["format"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "phash": str(row["phash"] or ""),
                    "tags": detail.get("tags", []),
                    "sources": sources,
                    "platform": source0.get("platform", ""),
                    "post_url": source0.get("post_url", ""),
                    "similar_image_ids": source0.get("extra", {}).get("similar_image_ids", []),
                    "review_tasks": self._build_review_task_payloads(
                        image_id,
                        statuses=("pending", "uncertain", "rejected"),
                    ),
                }
            )
        return self._json_response({"items": items, **_pagination_payload(total, limit, offset)})

    async def api_image_detail(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        image_id = int(request.query.get("image_id", 0) or 0)
        detail = self.db.get_image_detail(image_id)
        if not detail:
            return self._json_response({"error": "image_not_found"}, status=404)
        payload = dict(detail)
        sources = payload.get("sources", [])
        source0 = sources[0] if sources else {}
        raw_similar_ids = source0.get("extra", {}).get("similar_image_ids", [])
        similar_ids: list[int] = []
        for item in raw_similar_ids:
            try:
                similar_id = int(item or 0)
            except (TypeError, ValueError):
                continue
            if similar_id > 0:
                similar_ids.append(similar_id)
        payload["similar_image_ids"] = self.db.filter_ignored_similar_image_ids(
            image_id,
            similar_ids,
        )
        payload["review_tasks"] = self._build_review_task_payloads(
            image_id,
            statuses=("pending", "uncertain", "rejected"),
        )
        return self._json_response(payload)

    async def api_image_file(self, request: web.Request) -> web.StreamResponse:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        image_id = int(request.query.get("image_id", 0) or 0)
        resolved_path = self.db.get_image_file_path(image_id)
        if not resolved_path:
            return self._json_response({"error": "image_not_found"}, status=404)
        path = Path(resolved_path)
        if not path.exists():
            return self._json_response({"error": "file_not_found"}, status=404)
        return web.FileResponse(path, headers=dict(IMAGE_FILE_CACHE_HEADERS))

    async def api_tags(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        args = request.query
        rows = self.db.list_tags(
            keyword=args.get("keyword", ""),
            limit=min(max(int(args.get("limit", 50) or 50), 1), 200),
        )
        items = [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "is_character": bool(row["is_character"]),
                "image_count": int(row["image_count"] or 0),
                "aliases": self.db.list_aliases(str(row["name"])),
            }
            for row in rows
        ]
        return self._json_response({"items": items})

    async def api_jobs(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        if request.method == "GET":
            rows = self.db.list_crawl_jobs(limit=50)
            return self._json_response({"items": [dict(row) for row in rows]})

        data = await self._json_body(request)
        platform = str(data.get("platform", "") or "").strip().lower()
        source_url = str(data.get("source_url", "") or "").strip()
        if platform == "pixiv" and not source_url:
            return self._json_response(
                {
                    "ok": False,
                    "message": "Pixiv URL 为空；请先用 Pixiv 搜索预览勾选作品后入队。",
                },
                status=400,
            )
        parsed_rules = parse_crawl_rule_text(str(data.get("tags", "")))
        try:
            job_id = await self.crawl_service.submit_job(
                platform,
                source_url,
                parsed_rules.manual_tags,
                include_tags=parse_tag_csv([*parsed_rules.include_tags, *parse_tag_csv(str(data.get("include_tags", "")))]),
                exclude_tags=parse_tag_csv([*parsed_rules.exclude_tags, *parse_tag_csv(str(data.get("exclude_tags", "")))]),
            )
        except Exception as exc:
            return self._json_response({"ok": False, "message": str(exc)}, status=400)
        return self._json_response({"ok": True, "job_id": job_id})

    async def api_jobs_pixiv_search_preview(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        tag_text = str(data.get("tag_text", "") or "").strip()
        if not tag_text:
            return self._json_response({"ok": False, "message": "请先填写 tag 词再搜索 Pixiv。"}, status=400)
        limit = _bounded_int(data.get("limit", 12), 12, min_value=1, max_value=30)
        resolved_tag = self._resolve_pixiv_search_tag(tag_text)
        if resolved_tag:
            canonical_name = str(resolved_tag["name"])
            query_terms = self._pixiv_query_terms_for_input(tag_text, canonical_name)
        else:
            canonical_name = ""
            query_terms = [tag_text]
        query_terms = self._dedupe_texts(query_terms)[:5]
        search_service = PixivSearchService(self.config)
        timeout_seconds = _bounded_int(
            self.config.get("platform_request_timeout", self.config.get("crawler_timeout_seconds", 20)),
            20,
            min_value=5,
            max_value=120,
        )
        max_pages = _bounded_int(self.config.get("pixiv_search_preview_max_pages", 2), 2, min_value=1, max_value=5)
        hits = []
        seen_ids: set[str] = set()
        try:
            for query_term in query_terms:
                remaining = max(1, limit - len(hits))
                term_hits = await search_service.search_tag(
                    query_term,
                    max_results=remaining,
                    max_pages=max_pages,
                    timeout_seconds=timeout_seconds,
                )
                for hit in term_hits:
                    if hit.illust_id in seen_ids:
                        continue
                    seen_ids.add(hit.illust_id)
                    hits.append(hit)
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
        except Exception as exc:
            return self._json_response({"ok": False, "message": f"Pixiv 搜索失败：{exc}"}, status=400)

        items = [
            {
                "illust_id": hit.illust_id,
                "post_url": hit.post_url,
                "title": hit.title,
                "author": hit.author,
                "raw_tags": hit.raw_tags or [],
                "translated_tags": hit.translated_tags or [],
                "already_exists": self.db.has_source_post_url(hit.post_url, platform="pixiv"),
                "rejected": self.db.is_rejected_source_post_url(hit.post_url, platform="pixiv"),
            }
            for hit in hits
        ]
        return self._json_response(
            {
                "ok": True,
                "resolved_tag": resolved_tag or {"id": 0, "name": canonical_name, "match_type": ""},
                "query_terms": query_terms,
                "items": items,
            }
        )

    async def api_jobs_pixiv_backfill(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        if request.method == "GET":
            rows = self.db.list_pixiv_backfill_tasks(
                limit=_bounded_int(request.query.get("limit", 30), 30, min_value=1, max_value=100),
            )
            return self._json_response({"items": [self._build_pixiv_backfill_task_item(row) for row in rows]})

        data = await self._json_body(request)
        try:
            task_id, info = await self.pixiv_backfill_service.create_task(
                tag_text=str(data.get("tag_text", "") or "").strip(),
                max_pages=_bounded_int(data.get("max_pages", 20), 20, min_value=1, max_value=100),
                max_results=_bounded_int(data.get("max_results", 200), 200, min_value=1, max_value=2000),
                max_new_jobs=_bounded_int(data.get("max_new_jobs", 100), 100, min_value=1, max_value=500),
                include_tags=parse_tag_csv(str(data.get("include_tags", "") or "")),
                exclude_tags=parse_tag_csv(str(data.get("exclude_tags", "") or "")),
            )
        except Exception as exc:
            return self._json_response({"ok": False, "message": str(exc)}, status=400)
        return self._json_response(
            {
                "ok": True,
                "task_id": task_id,
                "message": f"已创建 Pixiv 历史回填任务 #{task_id}",
                **info,
            }
        )

    async def api_jobs_pixiv_backfill_retry(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = await self.pixiv_backfill_service.retry_task(int(data.get("task_id", 0) or 0))
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    @staticmethod
    def _build_pixiv_backfill_task_item(row) -> dict[str, Any]:
        item = dict(row)
        try:
            query_terms = json.loads(str(item.get("query_terms_json") or "[]"))
        except json.JSONDecodeError:
            query_terms = []
        item["query_terms"] = query_terms if isinstance(query_terms, list) else []
        return item

    def _pixiv_query_terms_for_input(self, raw_text: str, canonical_name: str) -> list[str]:
        known_terms = self._known_pixiv_query_terms(raw_text, canonical_name)
        db_terms = self.db.get_pixiv_query_terms_for_tag(canonical_name) or [canonical_name]
        return self._dedupe_texts([*known_terms, *db_terms])

    @staticmethod
    def _known_pixiv_query_terms(*values: str) -> list[str]:
        return shared_known_pixiv_query_terms(*values)

    def _resolve_pixiv_search_tag(self, tag_text: str) -> dict[str, Any] | None:
        text = str(tag_text or "").strip()
        if not text:
            return None
        platform_match = self.db.resolve_platform_term("pixiv", text)
        if platform_match.matched and platform_match.tag_name:
            return {
                "id": int(platform_match.tag_id or 0),
                "name": str(platform_match.tag_name),
                "match_type": str(platform_match.match_type or "platform:pixiv"),
            }
        direct_match = self.db.resolve_tag(text, allow_fuzzy=True, candidate_limit=5)
        if direct_match.matched and direct_match.tag_name:
            return {
                "id": int(direct_match.tag_id or 0),
                "name": str(direct_match.tag_name),
                "match_type": str(direct_match.match_type or ""),
            }
        context = self.db.build_pixiv_review_search_context(text, platform="pixiv")
        matched_tags = context.get("matched_tags") if isinstance(context, dict) else []
        if isinstance(matched_tags, list) and matched_tags:
            first = matched_tags[0]
            if isinstance(first, dict) and first.get("name"):
                return {
                    "id": int(first.get("id", 0) or 0),
                    "name": str(first.get("name", "")),
                    "match_type": str(first.get("match_type", "")),
                }
        return None

    async def api_jobs_retry(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = await self.crawl_service.retry_job(int(data.get("job_id", 0) or 0))
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_reviews(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        args = request.query
        rows = self.db.list_review_tasks(
            status=args.get("status", "") or None,
            limit=min(max(int(args.get("limit", 20) or 20), 1), 100),
        )
        return self._json_response({"items": [dict(row) for row in rows]})

    async def api_pixiv_review_images(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        args = request.query
        statuses = [item.strip() for item in str(args.get("status", "") or "").split(",") if item.strip()]
        effective_statuses = tuple(statuses or ["pending", "uncertain"])
        keyword = str(args.get("keyword", "") or "").strip()
        limit, offset = _pagination_from_query(args, default_limit=30, max_limit=100)
        search_context = self.db.build_pixiv_review_search_context(keyword, platform="pixiv")
        total = self.db.count_pixiv_review_images(
            statuses=effective_statuses,
            keyword=keyword,
            search_context=search_context,
        )
        if total and offset >= total:
            page_count = max(1, (total + limit - 1) // limit)
            offset = (page_count - 1) * limit
        rows = self.db.list_pixiv_review_images(
            statuses=effective_statuses,
            limit=limit,
            offset=offset,
            keyword=keyword,
            search_context=search_context,
        )
        items: list[dict[str, Any]] = []
        source_term_cache: dict[tuple[str, str], dict[str, Any]] = {}
        candidate_tag_cache: dict[str, dict[str, Any] | None] = {}
        for row in rows:
            item = self._build_pixiv_review_item(
                int(row["image_id"]),
                review_statuses=effective_statuses,
                source_term_cache=source_term_cache,
                candidate_tag_cache=candidate_tag_cache,
                compact=True,
            )
            if item:
                items.append(item)
        return self._json_response({"items": items, "search_context": search_context, **_pagination_payload(total, limit, offset)})

    async def api_pixiv_review_image(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        image_id = int(request.query.get("image_id", 0) or 0)
        item = self._build_pixiv_review_item(image_id)
        if not item:
            return self._json_response({"ok": False, "message": "pixiv_review_image_not_found"}, status=404)
        return self._json_response({"ok": True, "item": item})

    async def api_pixiv_review_submit(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        selected_tag_names = data.get("selected_tag_names", []) or []
        if not isinstance(selected_tag_names, list):
            selected_tag_names = [selected_tag_names]
        source_terms = data.get("source_terms", []) or []
        if not isinstance(source_terms, list):
            source_terms = [source_terms]
        ok, result = self.db.apply_image_review(
            int(data.get("image_id", 0) or 0),
            selected_tag_names=selected_tag_names,
            source_terms=source_terms,
            platform="pixiv",
            reason=str(data.get("reason", "") or "").strip(),
            reject_unselected=bool(data.get("reject_unselected", True)),
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_pixiv_review_reject_image(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, result = self.db.reject_image_source(
            int(data.get("image_id", 0) or 0),
            platform="pixiv",
            reason=str(data.get("reason", "") or "").strip(),
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_pixiv_review_batch_preview(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, result = self.db.preview_batch_image_review(
            data.get("items", []) or [],
            platform="pixiv",
            reject_unselected=bool(data.get("reject_unselected", True)),
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_pixiv_review_batch_submit(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, result = self.db.apply_batch_image_review(
            data.get("items", []) or [],
            platform="pixiv",
            reject_unselected=bool(data.get("reject_unselected", True)),
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_pixiv_platform_terms(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        args = request.query
        term_type = str(args.get("term_type", "") or "").strip()
        rows = self.db.list_platform_terms(
            tag_name=str(args.get("tag_name", "") or "").strip(),
            platform="pixiv",
            term_types=[term_type] if term_type else None,
            keyword=str(args.get("keyword", "") or "").strip(),
            limit=min(max(int(args.get("limit", 80) or 80), 1), 200),
            offset=max(int(args.get("offset", 0) or 0), 0),
        )
        return self._json_response({"items": [self._build_platform_term_item(row) for row in rows]})

    async def api_pixiv_platform_terms_save(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        confidence_raw = data.get("confidence", None)
        confidence: float | None = None
        if confidence_raw not in (None, ""):
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                return self._json_response({"ok": False, "message": "confidence_invalid"}, status=400)
        term_id = int(data.get("term_id", 0) or 0)
        if term_id > 0:
            ok, message = self.db.update_platform_term(
                term_id,
                tag_name=str(data.get("tag_name", "") or "").strip(),
                term=str(data.get("term", "") or "").strip(),
                term_type=str(data.get("term_type", "") or "").strip(),
                source=str(data.get("source", "") or "").strip(),
                confidence=confidence,
            )
        else:
            ok, message = self.db.add_platform_term(
                str(data.get("tag_name", "") or "").strip(),
                str(data.get("term", "") or "").strip(),
                platform="pixiv",
                term_type=str(data.get("term_type", "") or "").strip() or "both",
                source=str(data.get("source", "") or "").strip() or "manual_review",
                confidence=(1.0 if confidence is None else confidence),
            )
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_pixiv_platform_terms_delete(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = self.db.remove_platform_term(int(data.get("term_id", 0) or 0))
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_pixiv_platform_suggestions(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        requested = str(request.query.get("tag_name", "") or "").strip()
        resolved = self.db.resolve_tag(requested, allow_fuzzy=False)
        canonical_tag = str(resolved.tag_name or requested).strip()
        row = self.db.get_tag_row(canonical_tag)
        if not row:
            return self._json_response({"ok": False, "message": "tag_not_found"}, status=404)
        canonical_name = str(row["name"])
        items = [
            self._build_platform_suggestion_item(item)
            for item in self.db.suggest_platform_terms_for_tag(
                tag_name=canonical_name,
                platform="pixiv",
                limit=min(max(int(request.query.get("limit", 20) or 20), 1), 50),
            )
        ]
        current_terms = [
            self._build_platform_term_item(term_row)
            for term_row in self.db.list_platform_terms(tag_id=int(row["id"]), platform="pixiv", limit=200)
        ]
        return self._json_response(
            {
                "ok": True,
                "tag": {
                    "id": int(row["id"]),
                    "name": canonical_name,
                    "is_character": bool(row["is_character"]),
                    "aliases": self.db.list_aliases(canonical_name),
                    "current_terms": current_terms,
                },
                "items": items,
            }
        )

    async def api_pixiv_platform_unresolved(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        items = [
            self._build_unresolved_platform_term_item(item)
            for item in self.db.list_unresolved_platform_terms(
                platform="pixiv",
                keyword=str(request.query.get("keyword", "") or "").strip(),
                limit=min(max(int(request.query.get("limit", 30) or 30), 1), 80),
            )
        ]
        return self._json_response({"items": items})

    async def api_pixiv_platform_batch_preview(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        terms_raw = data.get("terms", []) or []
        if isinstance(terms_raw, str):
            terms = parse_tag_csv(terms_raw)
        else:
            terms = parse_tag_csv([str(item or "") for item in terms_raw])
        ok, result = self.db.preview_batch_platform_terms(
            tag_name=str(data.get("tag_name", "") or "").strip(),
            terms=terms,
            platform="pixiv",
            term_type=str(data.get("term_type", "") or "").strip() or "both",
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_pixiv_platform_batch_submit(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        terms_raw = data.get("terms", []) or []
        if isinstance(terms_raw, str):
            terms = parse_tag_csv(terms_raw)
        else:
            terms = parse_tag_csv([str(item or "") for item in terms_raw])
        try:
            confidence = float(data.get("confidence", 0.8) or 0.8)
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "message": "confidence_invalid"}, status=400)
        ok, result = self.db.apply_batch_platform_terms(
            tag_name=str(data.get("tag_name", "") or "").strip(),
            terms=terms,
            platform="pixiv",
            term_type=str(data.get("term_type", "") or "").strip() or "both",
            source=str(data.get("source", "") or "").strip() or "pixiv_history",
            confidence=confidence,
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_tag_merge_candidates(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        items = self.db.list_tag_merge_candidates(
            keyword=str(request.query.get("keyword", "") or "").strip(),
            limit=min(max(int(request.query.get("limit", 40) or 40), 1), 120),
        )
        return self._json_response({"items": items})

    async def api_tag_merge_pending_candidates(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        items = self.db.list_tag_identity_candidates(
            status=str(request.query.get("status", "pending") or "pending").strip(),
            keyword=str(request.query.get("keyword", "") or "").strip(),
            limit=min(max(int(request.query.get("limit", 80) or 80), 1), 200),
        )
        return self._json_response({"items": items})

    async def api_tag_merge_identity_scan(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        service = TagIdentityService(self.context, self.db, self.config)
        try:
            result = await service.scan(
                limit=_bounded_int(data.get("limit", 80), 80, min_value=1, max_value=200),
                llm_limit=_bounded_int(data.get("llm_limit", self.config.get("tag_identity_llm_limit", 12)), 12, min_value=0, max_value=40),
            )
        except Exception as exc:
            return self._json_response({"ok": False, "message": f"身份候选扫描失败：{exc}"}, status=500)
        return self._json_response(result)

    async def api_tag_merge_candidate_ignore(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = self.db.ignore_tag_identity_candidate(int(data.get("candidate_id", 0) or 0))
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_tag_merge_preview(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        source_tags_raw = data.get("source_tags", []) or []
        if isinstance(source_tags_raw, str):
            source_tags = parse_tag_csv(source_tags_raw)
        else:
            source_tags = parse_tag_csv([str(item or "") for item in source_tags_raw])
        ok, result = self.db.preview_merge_tags(
            target_tag_name=str(data.get("target_tag", "") or "").strip(),
            source_tag_names=source_tags,
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_tag_merge_execute(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        source_tags_raw = data.get("source_tags", []) or []
        if isinstance(source_tags_raw, str):
            source_tags = parse_tag_csv(source_tags_raw)
        else:
            source_tags = parse_tag_csv([str(item or "") for item in source_tags_raw])
        ok, result = self.db.merge_tags(
            str(data.get("target_tag", "") or "").strip(),
            source_tags,
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_review_decision(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = self.db.apply_manual_review(
            int(data.get("review_id", 0) or 0),
            approved=bool(data.get("approved", False)),
        )
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_tag_create(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, result = self.db.create_or_get_tag(
            str(data.get("tag_name", "") or "").strip(),
            is_character=bool(data.get("is_character", True)),
        )
        payload = {"ok": ok}
        if isinstance(result, dict):
            payload.update(result)
        else:
            payload["message"] = str(result)
        if ok and payload.get("tag"):
            tag_payload = self._build_candidate_tag_payload(str(payload["tag"].get("name", "")))
            if tag_payload:
                payload["tag"] = tag_payload
        return self._json_response(payload, status=(200 if ok else 400))

    async def api_tag_alias(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        tag_name = str(data.get("tag_name", "")).strip()
        alias = str(data.get("alias", "")).strip()
        if request.method == "POST":
            ok, message = self.db.add_alias(tag_name, alias)
        else:
            ok, message = self.db.remove_alias(tag_name, alias)
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))

    async def api_tag_character(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        data = await self._json_body(request)
        ok, message = self.db.set_tag_character(
            str(data.get("tag_name", "")).strip(),
            bool(data.get("is_character", False)),
        )
        return self._json_response({"ok": ok, "message": message}, status=(200 if ok else 400))
