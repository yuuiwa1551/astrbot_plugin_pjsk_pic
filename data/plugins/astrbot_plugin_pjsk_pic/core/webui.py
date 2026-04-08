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

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>PJSK 图片库管理</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f5f6f8; color: #222; }
    header { padding: 16px 20px; background: #3c65f5; color: white; }
    main { padding: 16px; display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 16px; }
    section { background: white; border-radius: 10px; padding: 16px; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
    section.wide { grid-column: 1 / -1; }
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
    .card .body { padding: 10px; font-size: 13px; }
    .list { display: grid; gap: 10px; }
    .item { border: 1px solid #eceef2; border-radius: 8px; padding: 10px; }
    .review-item { display: grid; grid-template-columns: 92px 1fr; gap: 10px; align-items: start; }
    .review-item img { width: 92px; height: 92px; object-fit: cover; border-radius: 8px; background: #ddd; }
    .muted { color: #666; font-size: 12px; }
    .pill { display: inline-block; background: #eef2ff; color: #2f52d6; border-radius: 999px; padding: 2px 8px; margin: 2px 4px 2px 0; }
    .chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
    .chip { display: inline-block; background: white; color: #2f52d6; border: 1px solid #cfd8ff; border-radius: 999px; padding: 6px 10px; margin: 2px 4px 2px 0; font-size: 12px; }
    .chip.selected { background: #3c65f5; color: white; border-color: #3c65f5; }
    .chip.resolved { border-color: #93a7ff; }
    .chip.unresolved { border-style: dashed; }
    .notice { margin-top: 8px; font-size: 12px; color: #dce4ff; }
    .subheading { font-size: 12px; color: #666; margin: 10px 0 6px; }
    .tag-block { border: 1px dashed #e5e7eb; border-radius: 10px; padding: 10px; }
    .empty { padding: 20px 0; text-align: center; color: #666; }
    .pixiv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
    .pixiv-card { border: 1px solid #eceef2; border-radius: 12px; overflow: hidden; background: #fff; }
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
    .term-head { display: flex; justify-content: space-between; gap: 10px; align-items: start; flex-wrap: wrap; }
    .term-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .mini-btn { padding: 6px 10px; font-size: 12px; }
    button.danger { background: #dc2626; }
    .sample-links { display: grid; gap: 4px; }
    pre.json { background: #0f172a; color: #d7e2ff; padding: 10px; border-radius: 12px; overflow: auto; font-size: 12px; }
    @media (max-width: 1000px) {
      main { grid-template-columns: 1fr; }
      section.wide { grid-column: auto; }
      .modal-body { grid-template-columns: 1fr; }
      .pixiv-grid { grid-template-columns: 1fr; }
      .split-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<header>
  <h1 style="margin:0;">PJSK 图片库管理台</h1>
  <div class="muted" style="color:#dce4ff;">独立 WebUI：支持图库检索、Pixiv 图片审批、Pixiv 平台词管理、tag/别名管理、审核任务、采集任务与平台来源信息查看</div>
  <div class="notice" id="notice"></div>
</header>
<main>
  <div>
    <section>
      <h2>概览</h2>
      <div class="stats" id="stats"></div>
    </section>
    <section>
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
    <section>
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
    <section>
      <h2>审核任务</h2>
      <div class="row">
        <select id="reviewStatus"><option value="">全部</option><option>pending</option><option>uncertain</option><option>rejected</option><option>approved</option><option>manual_approved</option><option>manual_rejected</option></select>
        <button onclick="loadReviews()">刷新审核</button>
      </div>
      <div class="list" id="reviews"></div>
    </section>
    <section>
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
  <section class="wide">
    <h2>Pixiv 审批页</h2>
    <div class="row">
      <select id="pixivReviewStatus">
        <option value="">全部待处理</option>
        <option value="pending">pending</option>
        <option value="uncertain">uncertain</option>
        <option value="rejected">rejected</option>
      </select>
      <button onclick="loadPixivReviewImages()">刷新 Pixiv 审批</button>
      <span class="muted">点击来源 tag 可勾选本次要沉淀的 Pixiv 词；点击候选主 tag 可确认最终入库 tag。</span>
    </div>
    <div class="pixiv-grid" id="pixivReviewImages"></div>
  </section>
  <section class="wide">
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
          <div class="list" id="pixivPlatformUnresolved"></div>
        </div>
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
<script>
const params = new URLSearchParams(location.search);
const token = params.get('token') || '';
document.getElementById('notice').textContent = token ? '当前已附带访问令牌。' : '当前未附带访问令牌。';

function api(path) {
  const url = new URL(path, location.origin);
  if (token) url.searchParams.set('token', token);
  return url.toString();
}

async function fetchJson(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if (token) headers['X-PJSK-Token'] = token;
  const resp = await fetch(api(path), {...options, headers});
  if (!resp.ok) throw new Error(await resp.text());
  return await resp.json();
}

const pixivReviewState = {
  items: {},
  selectedTagsByImage: {},
  selectedTermsByImage: {},
  previewImageId: null,
};

const pixivPlatformState = {
  items: [],
  suggestionsTag: '',
  suggestions: [],
  unresolved: [],
  editingTermId: 0,
};

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
  document.getElementById('images').innerHTML = (data.items || []).map(item => `
    <div class="card">
      <img src="${api(`/api/image-file?image_id=${item.id}`)}" loading="lazy" />
      <div class="body">
        <div><strong>#${item.id}</strong> ${item.file_name}</div>
        <div class="muted">${item.width}x${item.height} · ${item.format} · ${item.platform || 'local'}</div>
        <div class="muted">phash: ${item.phash || '-'}</div>
        <div class="muted">来源: ${item.post_url || '-'}</div>
        <div class="muted">疑似重复: ${(item.similar_image_ids || []).join(', ') || '无'}</div>
        <div>${(item.tags || []).map(t => `<span class="pill">${t.name}(${t.review_status})</span>`).join('')}</div>
      </div>
    </div>
  `).join('') || '<div class="muted">暂无结果</div>';
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
  await fetchJson('/api/reviews/decision', {method: 'POST', body: JSON.stringify({review_id: reviewId, approved})});
  await Promise.all([loadReviews(), loadImages(), loadSummary(), loadPixivReviewImages()]);
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
    pixivReviewState.selectedTagsByImage[imageId] = defaults.length ? defaults : uniqueTexts((item.candidate_tags || []).slice(0, 1).map(tag => tag.name));
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

function toggleCandidateTag(imageId, tagName) {
  const current = uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []);
  const key = normalizeKey(tagName);
  const exists = current.some(item => normalizeKey(item) === key);
  pixivReviewState.selectedTagsByImage[imageId] = exists ? current.filter(item => normalizeKey(item) !== key) : [...current, tagName];
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
    if (!selectedTags.some(item => normalizeKey(item) === normalizeKey(resolvedTagName))) {
      pixivReviewState.selectedTagsByImage[imageId] = [...selectedTags, resolvedTagName];
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
  return `
    <div class="pixiv-card">
      <div class="pixiv-thumb-wrap">
        <img src="${api(`/api/image-file?image_id=${imageId}`)}" loading="lazy" onclick="openPixivPreview(${imageId})" />
        <button class="preview-btn" onclick="openPixivPreview(${imageId})">预览</button>
      </div>
      <div class="pixiv-body">
        <div><strong>#${imageId}</strong> ${escapeHtml(item.file_name || '')}</div>
        <div class="muted">${item.width}x${item.height} · ${escapeHtml(item.author || '-')}</div>
        <div class="muted">标题：${escapeHtml(item.title || '-')}</div>
        <div class="muted">来源：${item.post_url ? `<a href="${escapeAttr(item.post_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.post_url)}</a>` : '-'}</div>
        <div class="tag-block">
          <div class="subheading">当前审核项</div>
          <div>${(item.review_tasks || []).map(task => `<span class="pill">${escapeHtml(task.tag_name)}(${escapeHtml(task.status)})</span>`).join('') || '<span class="muted">无</span>'}</div>
          <div class="subheading">候选主 tag</div>
          <div class="chip-row">${(item.candidate_tags || []).map(tag => renderCandidateTagChip(imageId, tag, selectedTags.some(name => normalizeKey(name) === normalizeKey(tag.name)))).join('') || '<span class="muted">暂无候选主 tag</span>'}</div>
          <div class="subheading">Pixiv 来源 tag</div>
          <div class="chip-row">${(item.source_terms || []).map(term => renderSourceTermChip(imageId, term, selectedTerms.some(name => normalizeKey(name) === normalizeKey(term.term)))).join('') || '<span class="muted">暂无来源 tag</span>'}</div>
          <div class="muted">已选主 tag：${selectedTags.map(escapeHtml).join('、') || '无'}；已选来源词：${selectedTerms.map(escapeHtml).join('、') || '无'}</div>
        </div>
        <div class="pixiv-actions">
          <button onclick="submitPixivReview(${imageId})">确认审核</button>
          <button class="secondary" onclick="resetPixivReviewSelection(${imageId})">重置</button>
        </div>
      </div>
    </div>
  `;
}

function renderPixivReviewList() {
  const items = Object.values(pixivReviewState.items).sort((a, b) => (b.image_id || 0) - (a.image_id || 0));
  document.getElementById('pixivReviewImages').innerHTML = items.length ? items.map(renderPixivReviewCard).join('') : '<div class="empty">暂无 Pixiv 待审图片</div>';
}

async function loadPixivReviewImages() {
  const q = new URLSearchParams({status: document.getElementById('pixivReviewStatus').value, limit: '24'});
  const data = await fetchJson(`/api/pixiv-review-images?${q.toString()}`);
  pixivReviewState.items = {};
  for (const item of (data.items || [])) {
    pixivReviewState.items[item.image_id] = item;
    ensurePixivReviewSelection(item);
  }
  renderPixivReviewList();
  renderPixivPreview();
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

function renderPixivPreview() {
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
      <div class="subheading">候选主 tag</div>
      <div class="chip-row">${(item.candidate_tags || []).map(tag => renderCandidateTagChip(imageId, tag, selectedTags.some(name => normalizeKey(name) === normalizeKey(tag.name)))).join('') || '<span class="muted">暂无候选主 tag</span>'}</div>
      <div class="subheading">Pixiv 来源 tag</div>
      <div class="chip-row">${(item.source_terms || []).map(term => renderSourceTermChip(imageId, term, selectedTerms.some(name => normalizeKey(name) === normalizeKey(term.term)))).join('') || '<span class="muted">暂无来源 tag</span>'}</div>
      <div class="muted">已选主 tag：${selectedTags.map(escapeHtml).join('、') || '无'}；已选来源词：${selectedTerms.map(escapeHtml).join('、') || '无'}</div>
      <div class="row">
        <button onclick="submitPixivReview(${imageId})">确认审核</button>
        <button class="secondary" onclick="resetPixivReviewSelection(${imageId})">重置</button>
      </div>
      <div class="subheading">Pixiv 映射信息</div>
      ${buildCandidateMappingsHtml(item)}
    </div>
  `;
}

async function openPixivPreview(imageId) {
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
  document.getElementById('pixivPreviewModal').classList.remove('show');
  renderPixivPreview();
}

async function submitPixivReview(imageId) {
  const selectedTags = uniqueTexts(pixivReviewState.selectedTagsByImage[imageId] || []);
  if (!selectedTags.length) {
    alert('请至少选择一个主 tag。');
    return;
  }
  const payload = {
    image_id: imageId,
    selected_tag_names: selectedTags,
    source_terms: uniqueTexts(pixivReviewState.selectedTermsByImage[imageId] || []),
    reject_unselected: true,
  };
  const result = await fetchJson('/api/pixiv-review/submit', {method: 'POST', body: JSON.stringify(payload)});
  const mappedText = (result.mapped_terms || []).map(item => `${item.term}→${item.tag_name}`).join('、');
  const skippedText = (result.skipped_terms || []).join('；');
  alert([result.message || '已完成审核', mappedText ? `已沉淀：${mappedText}` : '', skippedText ? `未沉淀：${skippedText}` : ''].filter(Boolean).join('\\n'));
  delete pixivReviewState.selectedTagsByImage[imageId];
  delete pixivReviewState.selectedTermsByImage[imageId];
  closePixivPreview();
  await Promise.all([loadPixivReviewImages(), loadImages(), loadReviews(), loadSummary(), loadPixivPlatformTerms(), loadPixivPlatformUnresolved(), loadPixivPlatformSuggestions()]);
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

function renderPixivPlatformUnresolvedItem(item) {
  return `
    <div class="term-item">
      <div class="term-head">
        <div>
          <div><strong>${escapeHtml(item.term)}</strong> <span class="pill">待确认</span></div>
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
  await Promise.all([loadPixivPlatformTerms(), loadPixivPlatformSuggestions(tagName), loadPixivPlatformUnresolved(), loadPixivReviewImages()]);
}

async function deletePixivPlatformTerm(termId) {
  if (!confirm('确认删除这条 Pixiv 平台词吗？')) return;
  const result = await fetchJson('/api/pixiv-platform-terms', {
    method: 'DELETE',
    body: JSON.stringify({term_id: termId}),
  });
  alert(result.message || '已删除平台词');
  if (pixivPlatformState.editingTermId === Number(termId || 0)) resetPixivPlatformForm();
  await Promise.all([loadPixivPlatformTerms(), loadPixivPlatformSuggestions(), loadPixivPlatformUnresolved(), loadPixivReviewImages()]);
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
  await Promise.all([loadPixivPlatformTerms(), loadPixivPlatformSuggestions(resolvedTag), loadPixivPlatformUnresolved(), loadPixivReviewImages()]);
}

resetPixivPlatformForm();
Promise.all([loadSummary(), loadImages(), loadJobs(), loadReviews(), loadTags(), loadPixivReviewImages(), loadPixivPlatformTerms(), loadPixivPlatformUnresolved()]).catch(err => { console.error(err); alert(err.message || err); });
</script>
</body>
</html>
"""


class GalleryWebUI:
    def __init__(self, db: ImageIndexDB, crawl_service) -> None:
        self.db = db
        self.crawl_service = crawl_service
        self.host = "0.0.0.0"
        self.port = 9099
        self.access_token = ""
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
        app.add_routes(
            [
                web.get("/", self.ui_page),
                web.get("/api/summary", self.api_summary),
                web.get("/api/images", self.api_images),
                web.get("/api/image", self.api_image_detail),
                web.get("/api/image-file", self.api_image_file),
                web.get("/api/tags", self.api_tags),
                web.get("/api/jobs", self.api_jobs),
                web.post("/api/jobs", self.api_jobs),
                web.post("/api/jobs/retry", self.api_jobs_retry),
                web.get("/api/reviews", self.api_reviews),
                web.get("/api/pixiv-review-images", self.api_pixiv_review_images),
                web.get("/api/pixiv-review-image", self.api_pixiv_review_image),
                web.post("/api/pixiv-review/submit", self.api_pixiv_review_submit),
                web.get("/api/pixiv-platform-terms", self.api_pixiv_platform_terms),
                web.post("/api/pixiv-platform-terms/save", self.api_pixiv_platform_terms_save),
                web.delete("/api/pixiv-platform-terms", self.api_pixiv_platform_terms_delete),
                web.get("/api/pixiv-platform-suggestions", self.api_pixiv_platform_suggestions),
                web.get("/api/pixiv-platform-unresolved", self.api_pixiv_platform_unresolved),
                web.post("/api/reviews/decision", self.api_review_decision),
                web.post("/api/tag/alias", self.api_tag_alias),
                web.delete("/api/tag/alias", self.api_tag_alias),
                web.post("/api/tag/character", self.api_tag_character),
            ]
        )

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
        token_suffix = f"?token={self.access_token}" if self.access_token else ""

        if self.host in {"0.0.0.0", "::"}:
            urls = [f"http://127.0.0.1:{port}/{token_suffix}"]
            lan_ip = self._detect_lan_ip()
            if lan_ip:
                urls.insert(0, f"http://{lan_ip}:{port}/{token_suffix}")
            return urls
        return [f"http://{self.host}:{port}/{token_suffix}"]

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

    def _check_access(self, request: web.Request) -> web.Response | None:
        if not self.access_token:
            return None
        token = (
            request.query.get("token", "")
            or request.headers.get("X-PJSK-Token", "")
            or self._bearer_token(request.headers.get("Authorization", ""))
        )
        if token == self.access_token:
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

    def _build_source_term_payload(self, term: str, origin: str) -> dict[str, Any]:
        text = str(term or "").strip()
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
        return {
            "term": text,
            "origin": str(origin or "raw"),
            "resolved_tag_name": resolved_tag_name,
            "resolution": resolution,
            "match_type": match_type,
            "candidate_tags": candidates,
        }

    def _build_candidate_tag_payload(self, tag_name: str) -> dict[str, Any] | None:
        row = self.db.get_tag_row(tag_name)
        if not row:
            return None
        canonical_name = str(row["name"])
        return {
            "name": canonical_name,
            "is_character": bool(row["is_character"]),
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

    def _build_pixiv_review_item(self, image_id: int) -> dict[str, Any] | None:
        detail = self.db.get_image_detail(image_id)
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
        seen_terms: set[str] = set()
        for origin, terms in (("raw", raw_tags), ("translated", translated_tags)):
            for term in terms:
                normalized = normalize_tag_name(term)
                if not normalized or normalized in seen_terms:
                    continue
                seen_terms.add(normalized)
                source_terms.append(self._build_source_term_payload(term, origin))

        review_task_rows = self.db.get_review_tasks_for_image(image_id)
        review_tasks = [
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
            for row in review_task_rows
        ]

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
            payload = self._build_candidate_tag_payload(name)
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
        }

    async def ui_page(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        return web.Response(text=HTML_PAGE, content_type="text/html", charset="utf-8")

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
        rows = self.db.search_images(
            keyword=args.get("keyword", ""),
            review_status=args.get("review_status", ""),
            tag_name=args.get("tag", ""),
            platform=args.get("platform", ""),
            limit=min(max(int(args.get("limit", 30) or 30), 1), 100),
            offset=max(int(args.get("offset", 0) or 0), 0),
        )
        items: list[dict] = []
        for row in rows:
            detail = self.db.get_image_detail(int(row["id"])) or {}
            sources = detail.get("sources", [])
            source0 = sources[0] if sources else {}
            items.append(
                {
                    "id": int(row["id"]),
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
                }
            )
        return self._json_response({"items": items})

    async def api_image_detail(self, request: web.Request) -> web.Response:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        image_id = int(request.query.get("image_id", 0) or 0)
        detail = self.db.get_image_detail(image_id)
        if not detail:
            return self._json_response({"error": "image_not_found"}, status=404)
        return self._json_response(detail)

    async def api_image_file(self, request: web.Request) -> web.StreamResponse:
        denied = self._check_access(request)
        if denied is not None:
            return denied
        image_id = int(request.query.get("image_id", 0) or 0)
        detail = self.db.get_image_detail(image_id)
        if not detail:
            return self._json_response({"error": "image_not_found"}, status=404)
        resolved_path = self.db.get_image_file_path(image_id)
        path = Path(resolved_path) if resolved_path else Path(str(detail["image"]["file_path"]))
        if not path.exists():
            return self._json_response({"error": "file_not_found"}, status=404)
        return web.FileResponse(path)

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
        parsed_rules = parse_crawl_rule_text(str(data.get("tags", "")))
        try:
            job_id = await self.crawl_service.submit_job(
                str(data.get("platform", "")).strip(),
                str(data.get("source_url", "")).strip(),
                parsed_rules.manual_tags,
                include_tags=parse_tag_csv([*parsed_rules.include_tags, *parse_tag_csv(str(data.get("include_tags", "")))]),
                exclude_tags=parse_tag_csv([*parsed_rules.exclude_tags, *parse_tag_csv(str(data.get("exclude_tags", "")))]),
            )
        except Exception as exc:
            return self._json_response({"ok": False, "message": str(exc)}, status=400)
        return self._json_response({"ok": True, "job_id": job_id})

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
        rows = self.db.list_pixiv_review_images(
            statuses=statuses or None,
            limit=min(max(int(args.get("limit", 24) or 24), 1), 100),
            offset=max(int(args.get("offset", 0) or 0), 0),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._build_pixiv_review_item(int(row["image_id"]))
            if item:
                items.append(item)
        return self._json_response({"items": items})

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
