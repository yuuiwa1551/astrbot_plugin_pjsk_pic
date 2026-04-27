<script setup lang="ts">
import { computed, reactive, ref, watch, type Component } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  BadgeCheck,
  Boxes,
  GitMerge,
  Images,
  LayoutDashboard,
  ListChecks,
  LogOut,
  RefreshCw,
  Search,
  Tags,
} from 'lucide-vue-next';
import { apiUrl, fetchJson, imageFileUrl } from './api';
import { pageRoutes, type PageKey } from './router';
import type { Dict, ImageItem, PaginatedResponse, PixivReviewItem, SummaryStats, TagItem } from './types';

const route = useRoute();
const router = useRouter();

const iconMap: Record<PageKey, Component> = {
  overview: LayoutDashboard,
  gallery: Images,
  jobs: ListChecks,
  tags: Tags,
  'pixiv-review': BadgeCheck,
  'pixiv-platform': Boxes,
  'tag-merge': GitMerge,
};

const pageTitle = computed(() => pageRoutes.find((page) => page.key === activePage.value)?.title || '概览');
const activePage = computed<PageKey>(() => (route.name as PageKey) || 'overview');
const busyCount = ref(0);
const toast = reactive({ show: false, message: '', detail: '' });
let toastTimer: number | null = null;

const summary = ref<SummaryStats>({});

const gallery = reactive({
  keyword: '',
  tag: '',
  status: 'approved,manual_approved',
  platform: '',
  page: 1,
  limit: 30,
  total: 0,
  pageCount: 1,
  jumpPage: 1,
  items: [] as ImageItem[],
});

const jobs = reactive({
  platform: 'pixiv',
  sourceUrl: '',
  tags: '',
  includeTags: '',
  excludeTags: '',
  items: [] as Dict[],
});

const tags = reactive({
  keyword: '',
  items: [] as TagItem[],
  createName: '',
  createCharacter: true,
  aliasTag: '',
  aliasValue: '',
  characterTag: '',
  characterValue: true,
});

const pixiv = reactive({
  status: 'pending,uncertain',
  keyword: '',
  searchContext: null as Dict | null,
  page: 1,
  limit: 30,
  total: 0,
  pageCount: 1,
  jumpPage: 1,
  items: [] as PixivReviewItem[],
  selectedImages: {} as Record<number, boolean>,
  selectedTags: {} as Record<number, string[]>,
  selectedTerms: {} as Record<number, string[]>,
  manualTagImageId: 0,
  manualTagName: '',
  manualTagCharacter: true,
  batchTags: '',
  batchTerms: '',
  batchRejectUnselected: true,
  batchPreview: [] as Dict[],
});
let pixivSearchTimer: number | null = null;

const platform = reactive({
  tagFilter: '',
  keyword: '',
  termType: '',
  mode: 'terms' as 'terms' | 'suggestions' | 'unresolved',
  terms: [] as Dict[],
  suggestions: [] as Dict[],
  unresolved: [] as Dict[],
  selectedTerms: {} as Record<string, boolean>,
  formTermId: 0,
  formTag: '',
  formTerm: '',
  formType: 'both',
  formSource: 'manual_review',
  formConfidence: 1,
  bulkTag: '',
  bulkTerms: '',
  bulkType: 'both',
  bulkSource: 'pixiv_history',
  bulkConfidence: 0.8,
  bulkPreview: [] as Dict[],
});

const merge = reactive({
  keyword: '',
  candidates: [] as Dict[],
  target: '',
  sources: '',
  preview: null as Dict | null,
});

const preview = reactive({
  open: false,
  mode: '' as '' | 'image' | 'pixiv',
  imageId: 0,
  imageDetail: null as Dict | null,
  pixivItem: null as PixivReviewItem | null,
});

function showToast(message: string, detail = ''): void {
  toast.message = message;
  toast.detail = detail;
  toast.show = true;
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.show = false;
  }, 3600);
}

async function withBusy<T>(task: () => Promise<T>, successMessage = ''): Promise<T | null> {
  busyCount.value += 1;
  try {
    const result = await task();
    if (successMessage) showToast(successMessage);
    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    showToast(message || '操作失败');
    console.error(error);
    return null;
  } finally {
    busyCount.value -= 1;
  }
}

function normalizeKey(value: unknown): string {
  return String(value || '').trim().toLowerCase();
}

function uniqueTexts(values: unknown[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const raw of values || []) {
    const text = String(raw || '').trim();
    const key = normalizeKey(text);
    if (!text || seen.has(key)) continue;
    seen.add(key);
    result.push(text);
  }
  return result;
}

function parseCsv(value: unknown): string[] {
  return uniqueTexts(String(value || '').split(/[,，\n]/g));
}

const pageSizeOptions = [15, 30, 60, 100];

const statusLabels: Record<string, string> = {
  pending: '待审核',
  uncertain: '待确认',
  approved: '已通过',
  manual_approved: '人工通过',
  rejected: '已拒绝',
  manual_rejected: '人工拒绝',
  running: '运行中',
  queued: '排队中',
  completed: '已完成',
  failed: '失败',
};

function statusLabel(status?: string): string {
  const text = String(status || '').trim();
  if (!text) return '未知';
  return text.split(',').map((item) => statusLabels[item.trim()] || item.trim()).filter(Boolean).join(' / ');
}

function clampPage(page: number, pageCount: number): number {
  const safeCount = Math.max(1, Number(pageCount || 1));
  const safePage = Number.isFinite(page) ? Math.trunc(page) : 1;
  return Math.min(Math.max(safePage || 1, 1), safeCount);
}

function applyPagination(target: { page: number; limit: number; total: number; pageCount: number; jumpPage: number }, data: Partial<PaginatedResponse<unknown>>): void {
  target.limit = Number(data.limit || target.limit || 30);
  target.total = Number(data.total || 0);
  target.pageCount = Math.max(1, Number(data.page_count || 1));
  target.page = clampPage(Number(data.page || 1), target.pageCount);
  target.jumpPage = target.page;
}

function taskStatusClass(status?: string): string {
  const text = String(status || '');
  if (['approved', 'manual_approved'].includes(text)) return 'success';
  if (['rejected', 'manual_rejected'].includes(text)) return 'danger';
  if (['uncertain'].includes(text)) return 'warning';
  return '';
}

function refreshCurrent(): void {
  void loadPage(activePage.value);
}

async function loadPage(page: PageKey): Promise<void> {
  await withBusy(async () => {
    if (page === 'overview') await loadSummary();
    if (page === 'gallery') await loadImages();
    if (page === 'jobs') await loadJobs();
    if (page === 'tags') await loadTags();
    if (page === 'pixiv-review') await loadPixivReviewImages();
    if (page === 'pixiv-platform') await loadPlatformTerms();
    if (page === 'tag-merge') await loadMergeCandidates();
  });
}

watch(
  () => activePage.value,
  (page) => {
    void loadPage(page);
  },
  { immediate: true },
);

async function logout(): Promise<void> {
  await withBusy(async () => {
    await fetchJson('/api/auth/logout', { method: 'POST' });
    window.location.href = apiUrl('/');
  });
}

async function loadSummary(): Promise<void> {
  summary.value = await fetchJson('/api/summary');
}

async function loadImages(): Promise<void> {
  const q = new URLSearchParams({
    keyword: gallery.keyword,
    tag: gallery.tag,
    review_status: gallery.status,
    platform: gallery.platform,
    page: String(gallery.page),
    limit: String(gallery.limit),
  });
  const data = await fetchJson<PaginatedResponse<ImageItem>>(`/api/images?${q.toString()}`);
  gallery.items = data.items || [];
  applyPagination(gallery, data);
}

function searchImages(): void {
  gallery.page = 1;
  void withBusy(loadImages);
}

function changeGalleryPage(page: number): void {
  gallery.page = clampPage(page, gallery.pageCount);
  void withBusy(loadImages);
}

function changeGalleryLimit(): void {
  gallery.page = 1;
  void withBusy(loadImages);
}

async function openImagePreview(imageId: number): Promise<void> {
  await withBusy(async () => {
    preview.mode = 'image';
    preview.imageId = imageId;
    preview.imageDetail = await fetchJson(`/api/image?image_id=${imageId}`);
    preview.pixivItem = null;
    preview.open = true;
  });
}

function closePreview(): void {
  preview.open = false;
  preview.mode = '';
  preview.imageId = 0;
  preview.imageDetail = null;
  preview.pixivItem = null;
}

async function loadJobs(): Promise<void> {
  const data = await fetchJson<{ items: Dict[] }>('/api/jobs');
  jobs.items = data.items || [];
}

async function createJob(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        platform: jobs.platform,
        source_url: jobs.sourceUrl,
        tags: jobs.tags,
        include_tags: jobs.includeTags,
        exclude_tags: jobs.excludeTags,
      }),
    });
    jobs.sourceUrl = '';
    jobs.tags = '';
    jobs.includeTags = '';
    jobs.excludeTags = '';
    await loadJobs();
    showToast(String(result.message || '采集任务已创建'), result.job_id ? `#${result.job_id}` : '');
  });
}

async function retryJob(jobId: number): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/jobs/retry', {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId }),
    });
    await loadJobs();
    showToast(String(result.message || '已提交重试'));
  });
}

async function reviewDecision(reviewId: number, approved: boolean): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/reviews/decision', {
      method: 'POST',
      body: JSON.stringify({ review_id: reviewId, approved }),
    });
    await loadImages();
    await loadSummary();
    showToast(String(result.message || '审核状态已更新'));
  });
}

async function loadTags(): Promise<void> {
  const q = new URLSearchParams({ keyword: tags.keyword, limit: '80' });
  const data = await fetchJson<{ items: TagItem[] }>(`/api/tags?${q.toString()}`);
  tags.items = data.items || [];
}

async function createTag(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/tag/create', {
      method: 'POST',
      body: JSON.stringify({ tag_name: tags.createName, is_character: tags.createCharacter }),
    });
    tags.createName = '';
    await loadTags();
    showToast(String(result.message || 'tag 已保存'));
  });
}

async function saveAlias(remove = false): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/tag/alias', {
      method: remove ? 'DELETE' : 'POST',
      body: JSON.stringify({ tag_name: tags.aliasTag, alias: tags.aliasValue }),
    });
    await loadTags();
    showToast(String(result.message || (remove ? 'alias 已删除' : 'alias 已保存')));
  });
}

async function setCharacter(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/tag/character', {
      method: 'POST',
      body: JSON.stringify({ tag_name: tags.characterTag, is_character: tags.characterValue }),
    });
    await loadTags();
    showToast(String(result.message || '角色标记已更新'));
  });
}

function ensurePixivSelection(item: PixivReviewItem): void {
  const imageId = item.image_id;
  if (!pixiv.selectedTags[imageId]) {
    const defaults = uniqueTexts((item.review_tasks || []).filter((task) => task.status !== 'manual_rejected').map((task) => task.tag_name));
    const candidates = uniqueTexts((item.candidate_tags || []).slice(0, 1).map((tag) => tag.name));
    const canonical = defaults[0] || candidates[0] || '';
    pixiv.selectedTags[imageId] = canonical ? [canonical] : [];
  }
  if (!pixiv.selectedTerms[imageId]) {
    pixiv.selectedTerms[imageId] = [];
  }
}

function prunePixivSelections(): void {
  const currentIds = new Set(pixiv.items.map((item) => item.image_id));
  for (const key of Object.keys(pixiv.selectedImages)) if (!currentIds.has(Number(key))) delete pixiv.selectedImages[Number(key)];
  for (const key of Object.keys(pixiv.selectedTags)) if (!currentIds.has(Number(key))) delete pixiv.selectedTags[Number(key)];
  for (const key of Object.keys(pixiv.selectedTerms)) if (!currentIds.has(Number(key))) delete pixiv.selectedTerms[Number(key)];
}

async function loadPixivReviewImages(): Promise<void> {
  const q = new URLSearchParams({
    status: pixiv.status,
    keyword: pixiv.keyword.trim(),
    page: String(pixiv.page),
    limit: String(pixiv.limit),
  });
  const data = await fetchJson<PaginatedResponse<PixivReviewItem> & { search_context?: Dict }>(`/api/pixiv-review-images?${q.toString()}`);
  pixiv.items = data.items || [];
  pixiv.searchContext = data.search_context || null;
  pixiv.batchPreview = [];
  applyPagination(pixiv, data);
  for (const item of pixiv.items) ensurePixivSelection(item);
  prunePixivSelections();
}

async function refillPixivPageIfEmpty(): Promise<void> {
  if (pixiv.items.length) return;
  await loadPixivReviewImages();
}

function resetPixivPage(): void {
  pixiv.page = 1;
  pixiv.jumpPage = 1;
}

function schedulePixivSearch(): void {
  if (pixivSearchTimer) window.clearTimeout(pixivSearchTimer);
  pixivSearchTimer = window.setTimeout(() => {
    resetPixivPage();
    void withBusy(loadPixivReviewImages);
  }, 420);
}

function submitPixivSearch(): void {
  if (pixivSearchTimer) window.clearTimeout(pixivSearchTimer);
  resetPixivPage();
  void withBusy(loadPixivReviewImages);
}

function clearPixivSearch(): void {
  pixiv.keyword = '';
  submitPixivSearch();
}

function changePixivStatus(): void {
  resetPixivPage();
  void withBusy(loadPixivReviewImages);
}

function changePixivPage(page: number): void {
  pixiv.page = clampPage(page, pixiv.pageCount);
  void withBusy(loadPixivReviewImages);
}

function changePixivLimit(): void {
  resetPixivPage();
  void withBusy(loadPixivReviewImages);
}

const pixivSearchMessage = computed(() => {
  const keyword = pixiv.keyword.trim();
  if (!keyword) return '先筛选角色相关待审图，再勾选图片做批量审核。';
  const ctx = pixiv.searchContext || {};
  const matched = ((ctx.matched_tags as Dict[] | undefined) || []).map((item) => item.name).filter(Boolean);
  const terms = uniqueTexts((((ctx.expanded_terms || ctx.terms) as string[] | undefined) || [])).slice(0, 12);
  if (matched.length) {
    return `${keyword} 命中：${matched.join('、')}；展开词：${terms.join('、') || '无'}；共 ${pixiv.total} 张`;
  }
  return `${keyword} 未命中主 tag；已按待审 tag / Pixiv 来源词直接模糊搜索；共 ${pixiv.total} 张`;
});

function selectedPixivIds(): number[] {
  return Object.keys(pixiv.selectedImages).filter((key) => pixiv.selectedImages[Number(key)]).map(Number);
}

function togglePixivImage(imageId: number): void {
  if (pixiv.selectedImages[imageId]) delete pixiv.selectedImages[imageId];
  else pixiv.selectedImages[imageId] = true;
}

function selectAllPixiv(): void {
  for (const item of pixiv.items) pixiv.selectedImages[item.image_id] = true;
}

function clearPixivSelected(): void {
  for (const key of Object.keys(pixiv.selectedImages)) delete pixiv.selectedImages[Number(key)];
  pixiv.batchPreview = [];
}

function toggleCandidateTag(imageId: number, tagName: string): void {
  const current = uniqueTexts(pixiv.selectedTags[imageId] || []);
  pixiv.selectedTags[imageId] = normalizeKey(current[0]) === normalizeKey(tagName) ? [] : [tagName];
}

function toggleSourceTerm(imageId: number, term: string, resolvedTagName = ''): void {
  const current = uniqueTexts(pixiv.selectedTerms[imageId] || []);
  const exists = current.some((item) => normalizeKey(item) === normalizeKey(term));
  pixiv.selectedTerms[imageId] = exists ? current.filter((item) => normalizeKey(item) !== normalizeKey(term)) : [...current, term];
  if (!exists && resolvedTagName && !uniqueTexts(pixiv.selectedTags[imageId] || []).length) {
    pixiv.selectedTags[imageId] = [resolvedTagName];
  }
}

function resetPixivSelection(imageId: number): void {
  const item = pixiv.items.find((entry) => entry.image_id === imageId);
  if (!item) return;
  delete pixiv.selectedTags[imageId];
  delete pixiv.selectedTerms[imageId];
  ensurePixivSelection(item);
}

function toggleManualTagForm(imageId: number): void {
  pixiv.manualTagImageId = pixiv.manualTagImageId === imageId ? 0 : imageId;
  pixiv.manualTagName = '';
  pixiv.manualTagCharacter = true;
}

async function createPixivMainTag(imageId: number): Promise<void> {
  await withBusy(async () => {
    const tagName = pixiv.manualTagName.trim();
    if (!tagName) {
      showToast('请先填写主 tag');
      return;
    }
    const result = await fetchJson<Dict>('/api/tag/create', {
      method: 'POST',
      body: JSON.stringify({ tag_name: tagName, is_character: pixiv.manualTagCharacter }),
    });
    const item = pixiv.items.find((entry) => entry.image_id === imageId);
    const tag = (result.tag as TagItem | undefined) || { name: tagName, is_character: pixiv.manualTagCharacter };
    if (item && !(item.candidate_tags || []).some((candidate) => normalizeKey(candidate.name) === normalizeKey(tag.name))) {
      item.candidate_tags = [...(item.candidate_tags || []), tag];
    }
    pixiv.selectedTags[imageId] = [tag.name];
    pixiv.manualTagImageId = 0;
    pixiv.manualTagName = '';
    showToast(String(result.message || '已添加主 tag'), tag.name);
  });
}

function removePixivFromQueue(imageIds: number[]): void {
  const set = new Set(imageIds);
  pixiv.items = pixiv.items.filter((item) => !set.has(item.image_id));
  pixiv.total = Math.max(0, pixiv.total - set.size);
  pixiv.pageCount = Math.max(1, Math.ceil(pixiv.total / Math.max(1, pixiv.limit)));
  pixiv.page = clampPage(pixiv.page, pixiv.pageCount);
  pixiv.jumpPage = pixiv.page;
  for (const imageId of imageIds) {
    delete pixiv.selectedImages[imageId];
    delete pixiv.selectedTags[imageId];
    delete pixiv.selectedTerms[imageId];
  }
}

async function submitPixivReview(imageId: number): Promise<void> {
  await withBusy(async () => {
    const selectedTags = uniqueTexts(pixiv.selectedTags[imageId] || []);
    if (!selectedTags.length) {
      showToast('请至少选择一个归入主 tag');
      return;
    }
    const result = await fetchJson<Dict>('/api/pixiv-review/submit', {
      method: 'POST',
      body: JSON.stringify({
        image_id: imageId,
        selected_tag_names: selectedTags,
        source_terms: uniqueTexts(pixiv.selectedTerms[imageId] || []),
        reject_unselected: true,
      }),
    });
    closePreview();
    removePixivFromQueue([imageId]);
    await refillPixivPageIfEmpty();
    showToast(String(result.message || '已完成审核'), '已从当前审批队列移除。');
  });
}

async function rejectPixivReviewImage(imageId: number): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/pixiv-review/reject-image', {
      method: 'POST',
      body: JSON.stringify({ image_id: imageId }),
    });
    closePreview();
    removePixivFromQueue([imageId]);
    await refillPixivPageIfEmpty();
    showToast(String(result.message || '已拒绝图片'), result.post_url ? `已记录跳过来源：${String(result.post_url)}` : '已从当前审批队列移除。');
  });
}

function applyBatchTags(): void {
  const ids = selectedPixivIds();
  const batchTags = parseCsv(pixiv.batchTags);
  const batchTerms = parseCsv(pixiv.batchTerms);
  if (!ids.length) return showToast('请先勾选至少一张图片');
  if (!batchTags.length && !batchTerms.length) return showToast('请至少填写批量归入主 tag 或 alias / 来源词');
  for (const imageId of ids) {
    if (batchTags.length) pixiv.selectedTags[imageId] = [...batchTags];
    if (batchTerms.length) pixiv.selectedTerms[imageId] = [...batchTerms];
  }
  showToast('已应用到当前选择');
}

function buildPixivBatchItems(): Dict[] {
  const overrideTags = parseCsv(pixiv.batchTags);
  const overrideTerms = parseCsv(pixiv.batchTerms);
  return selectedPixivIds().map((imageId) => ({
    image_id: imageId,
    selected_tag_names: overrideTags.length ? overrideTags : uniqueTexts(pixiv.selectedTags[imageId] || []),
    source_terms: overrideTerms.length ? overrideTerms : uniqueTexts(pixiv.selectedTerms[imageId] || []),
  }));
}

async function previewPixivBatch(): Promise<void> {
  await withBusy(async () => {
    const items = buildPixivBatchItems();
    if (!items.length) return showToast('请先勾选至少一张图片');
    const result = await fetchJson<Dict>('/api/pixiv-review/batch-preview', {
      method: 'POST',
      body: JSON.stringify({ items, reject_unselected: pixiv.batchRejectUnselected }),
    });
    pixiv.batchPreview = (result.items as Dict[] | undefined) || [];
  });
}

async function submitPixivBatch(): Promise<void> {
  await withBusy(async () => {
    const items = buildPixivBatchItems();
    if (!items.length) return showToast('请先勾选至少一张图片');
    if (!window.confirm(`确认批量审核这 ${items.length} 张图片吗？`)) return;
    const result = await fetchJson<Dict>('/api/pixiv-review/batch-submit', {
      method: 'POST',
      body: JSON.stringify({ items, reject_unselected: pixiv.batchRejectUnselected }),
    });
    pixiv.batchPreview = (result.items as Dict[] | undefined) || [];
    removePixivFromQueue(items.map((item) => Number(item.image_id)).filter(Boolean));
    await refillPixivPageIfEmpty();
    showToast(String(result.message || '批量审核完成'), '已从当前审批队列移除。');
  });
}

async function openPixivPreview(imageId: number): Promise<void> {
  await withBusy(async () => {
    const data = await fetchJson<{ item: PixivReviewItem }>(`/api/pixiv-review-image?image_id=${imageId}`);
    const item = data.item || pixiv.items.find((entry) => entry.image_id === imageId) || null;
    if (!item) return;
    ensurePixivSelection(item);
    const index = pixiv.items.findIndex((entry) => entry.image_id === imageId);
    if (index >= 0) pixiv.items[index] = item;
    preview.mode = 'pixiv';
    preview.imageId = imageId;
    preview.pixivItem = item;
    preview.imageDetail = null;
    preview.open = true;
  });
}

async function loadPlatformTerms(): Promise<void> {
  const q = new URLSearchParams({
    tag_name: platform.tagFilter,
    keyword: platform.keyword,
    term_type: platform.termType,
    limit: '100',
  });
  const data = await fetchJson<{ items: Dict[] }>(`/api/pixiv-platform-terms?${q.toString()}`);
  platform.mode = 'terms';
  platform.terms = data.items || [];
}

async function loadPlatformSuggestions(): Promise<void> {
  await withBusy(async () => {
    const q = new URLSearchParams({ tag_name: platform.tagFilter, limit: '30' });
    const data = await fetchJson<Dict>(`/api/pixiv-platform-suggestions?${q.toString()}`);
    platform.mode = 'suggestions';
    platform.suggestions = (data.items as Dict[] | undefined) || [];
  });
}

async function loadPlatformUnresolved(): Promise<void> {
  await withBusy(async () => {
    const q = new URLSearchParams({ keyword: platform.keyword, limit: '50' });
    const data = await fetchJson<{ items: Dict[] }>(`/api/pixiv-platform-unresolved?${q.toString()}`);
    platform.mode = 'unresolved';
    platform.unresolved = data.items || [];
  });
}

function resetPlatformForm(): void {
  platform.formTermId = 0;
  platform.formTag = '';
  platform.formTerm = '';
  platform.formType = 'both';
  platform.formSource = 'manual_review';
  platform.formConfidence = 1;
}

function editPlatformTerm(item: Dict): void {
  platform.formTermId = Number(item.id || 0);
  platform.formTag = String(item.tag_name || '');
  platform.formTerm = String(item.term || '');
  platform.formType = String(item.term_type || 'both');
  platform.formSource = String(item.source || 'manual_review');
  platform.formConfidence = Number(item.confidence ?? 1);
}

async function savePlatformTerm(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/pixiv-platform-terms/save', {
      method: 'POST',
      body: JSON.stringify({
        term_id: platform.formTermId,
        tag_name: platform.formTag,
        term: platform.formTerm,
        term_type: platform.formType,
        source: platform.formSource,
        confidence: platform.formConfidence,
      }),
    });
    resetPlatformForm();
    await loadPlatformTerms();
    showToast(String(result.message || '平台词已保存'));
  });
}

async function quickSavePlatformTerm(tagName: string, term: string): Promise<void> {
  platform.formTag = tagName;
  platform.formTerm = term;
  platform.formType = 'both';
  platform.formSource = 'pixiv_history';
  platform.formConfidence = 0.8;
  await savePlatformTerm();
}

async function deletePlatformTerm(termId: number): Promise<void> {
  if (!window.confirm('确认删除这个平台词吗？')) return;
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/pixiv-platform-terms', {
      method: 'DELETE',
      body: JSON.stringify({ term_id: termId }),
    });
    await loadPlatformTerms();
    showToast(String(result.message || '平台词已删除'));
  });
}

function toggleBulkTerm(term: string): void {
  if (platform.selectedTerms[term]) delete platform.selectedTerms[term];
  else platform.selectedTerms[term] = true;
}

function selectedPlatformTerms(): string[] {
  return uniqueTexts([...Object.keys(platform.selectedTerms).filter((term) => platform.selectedTerms[term]), ...parseCsv(platform.bulkTerms)]);
}

async function previewPlatformBulk(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/pixiv-platform/batch-preview', {
      method: 'POST',
      body: JSON.stringify({
        tag_name: platform.bulkTag,
        terms: selectedPlatformTerms(),
        term_type: platform.bulkType,
      }),
    });
    platform.bulkPreview = (result.items as Dict[] | undefined) || [];
  });
}

async function submitPlatformBulk(): Promise<void> {
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/pixiv-platform/batch-submit', {
      method: 'POST',
      body: JSON.stringify({
        tag_name: platform.bulkTag,
        terms: selectedPlatformTerms(),
        term_type: platform.bulkType,
        source: platform.bulkSource,
        confidence: platform.bulkConfidence,
      }),
    });
    platform.bulkPreview = (result.items as Dict[] | undefined) || [];
    platform.selectedTerms = {};
    platform.bulkTerms = '';
    await loadPlatformTerms();
    showToast(String(result.message || '批量映射已完成'));
  });
}

async function loadMergeCandidates(): Promise<void> {
  const q = new URLSearchParams({ keyword: merge.keyword, limit: '60' });
  const data = await fetchJson<{ items: Dict[] }>(`/api/tag-merge/candidates?${q.toString()}`);
  merge.candidates = data.items || [];
}

function useMergeCandidate(source: string, target: string): void {
  merge.target = target;
  merge.sources = source;
  void previewMerge();
}

async function previewMerge(): Promise<void> {
  await withBusy(async () => {
    merge.preview = await fetchJson<Dict>('/api/tag-merge/preview', {
      method: 'POST',
      body: JSON.stringify({ target_tag: merge.target, source_tags: parseCsv(merge.sources) }),
    });
  });
}

async function executeMerge(): Promise<void> {
  if (!window.confirm('确认执行 tag 归并吗？')) return;
  await withBusy(async () => {
    const result = await fetchJson<Dict>('/api/tag-merge/execute', {
      method: 'POST',
      body: JSON.stringify({ target_tag: merge.target, source_tags: parseCsv(merge.sources) }),
    });
    merge.preview = result;
    await loadMergeCandidates();
    showToast(String(result.message || 'tag 归并完成'));
  });
}
</script>

<template>
  <div v-if="busyCount > 0" class="loading-bar" />
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">P</div>
        <div>
          <h1 class="brand-title">PJSK 图片管理</h1>
          <div class="brand-subtitle">Vue 工作台</div>
        </div>
      </div>
      <nav class="nav-stack" aria-label="功能导航">
        <RouterLink v-for="page in pageRoutes" :key="page.key" class="nav-link" :to="page.path">
          <component :is="iconMap[page.key]" />
          <span>{{ page.label }}</span>
        </RouterLink>
      </nav>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <h2 class="page-title">{{ pageTitle }}</h2>
        <div class="top-actions">
          <button class="secondary" type="button" @click="refreshCurrent">
            <RefreshCw />
            刷新
          </button>
          <button class="ghost" type="button" @click="logout">
            <LogOut />
            退出登录
          </button>
        </div>
      </header>

      <section v-if="activePage === 'overview'" class="page">
        <div class="stats-grid">
          <div class="stat-tile">
            <span class="muted">图片</span>
            <span class="stat-value">{{ summary.images ?? 0 }}</span>
          </div>
          <div class="stat-tile">
            <span class="muted">tag</span>
            <span class="stat-value">{{ summary.tags ?? 0 }}</span>
          </div>
          <div class="stat-tile">
            <span class="muted">alias</span>
            <span class="stat-value">{{ summary.aliases ?? 0 }}</span>
          </div>
          <div class="stat-tile">
            <span class="muted">采集任务</span>
            <span class="stat-value">{{ summary.crawl_jobs ?? 0 }}</span>
          </div>
          <div class="stat-tile">
            <span class="muted">待审核</span>
            <span class="stat-value">{{ summary.pending_reviews ?? 0 }}</span>
          </div>
        </div>
      </section>

      <section v-if="activePage === 'gallery'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <input v-model="gallery.keyword" class="grow" placeholder="关键词 / tag / alias" @keydown.enter="searchImages" />
            <input v-model="gallery.tag" placeholder="精确 tag" @keydown.enter="searchImages" />
            <select v-model="gallery.status" @change="searchImages">
              <option value="">全部状态</option>
              <option value="approved,manual_approved">已通过</option>
              <option value="approved">已通过（自动）</option>
              <option value="manual_approved">人工通过</option>
              <option value="pending">待审核</option>
              <option value="uncertain">待确认</option>
              <option value="rejected">已拒绝</option>
              <option value="manual_rejected">人工拒绝</option>
            </select>
            <select v-model="gallery.platform" @change="searchImages">
              <option value="">全部平台</option>
              <option>pixiv</option>
              <option>x</option>
              <option>xiaohongshu</option>
              <option>generic</option>
              <option>submission</option>
            </select>
            <button type="button" @click="searchImages">
              <Search />
              搜索
            </button>
          </div>
        </div>
        <div v-if="!gallery.items.length" class="empty">暂无图片</div>
        <div v-else class="grid">
          <article v-for="item in gallery.items" :key="item.id" class="item image-card">
            <div class="thumb">
              <img :src="imageFileUrl(item.id)" loading="lazy" alt="" @click="openImagePreview(item.id)" />
              <button type="button" @click="openImagePreview(item.id)">预览</button>
            </div>
            <div class="item-body">
              <div class="title-line">
                <strong>#{{ item.id }}</strong>
                <span class="truncate">{{ item.file_name }}</span>
              </div>
              <div class="muted">{{ item.width || 0 }}x{{ item.height || 0 }} · {{ item.format || '-' }} · {{ item.platform || 'local' }}</div>
              <div class="chips">
                <span v-for="tag in item.tags || []" :key="String(tag.name)" class="pill" :class="taskStatusClass(String(tag.review_status || ''))">
                  {{ tag.name }}{{ tag.review_status ? `(${statusLabel(String(tag.review_status))})` : '' }}
                </span>
              </div>
              <div class="actions">
                <button v-for="task in item.review_tasks || []" :key="task.id" class="mini secondary" type="button" @click="reviewDecision(task.id, true)">通过 {{ task.tag_name }}</button>
                <button v-for="task in item.review_tasks || []" :key="`reject-${task.id}`" class="mini danger" type="button" @click="reviewDecision(task.id, false)">拒绝</button>
              </div>
            </div>
          </article>
        </div>
        <div class="pagination-bar">
          <span class="muted">第 {{ gallery.page }} / {{ gallery.pageCount }} 页，共 {{ gallery.total }} 张</span>
          <button class="secondary mini" type="button" :disabled="gallery.page <= 1" @click="changeGalleryPage(gallery.page - 1)">上一页</button>
          <button class="secondary mini" type="button" :disabled="gallery.page >= gallery.pageCount" @click="changeGalleryPage(gallery.page + 1)">下一页</button>
          <label class="muted">跳到 <input v-model.number="gallery.jumpPage" class="page-input" type="number" min="1" :max="gallery.pageCount" @keydown.enter="changeGalleryPage(gallery.jumpPage)" /> 页</label>
          <button class="secondary mini" type="button" @click="changeGalleryPage(gallery.jumpPage)">跳转</button>
          <label class="muted">每页
            <select v-model.number="gallery.limit" class="page-size" @change="changeGalleryLimit">
              <option v-for="size in pageSizeOptions" :key="`gallery-size-${size}`" :value="size">{{ size }}</option>
            </select>
            张
          </label>
        </div>
      </section>

      <section v-if="activePage === 'jobs'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <select v-model="jobs.platform">
              <option>pixiv</option>
              <option>x</option>
              <option>xiaohongshu</option>
              <option>generic</option>
            </select>
            <input v-model="jobs.sourceUrl" class="grow" placeholder="帖子链接或图片直链" />
            <input v-model="jobs.tags" class="grow" placeholder="tags_csv，例如 初音未来,miku" />
          </div>
          <div class="toolbar-row">
            <input v-model="jobs.includeTags" class="grow" placeholder="include tags" />
            <input v-model="jobs.excludeTags" class="grow" placeholder="exclude tags" />
            <button type="button" @click="createJob">新建任务</button>
          </div>
        </div>
        <div class="grid review-grid">
          <article v-for="item in jobs.items" :key="Number(item.id)" class="item">
            <div class="title-line">
              <strong>#{{ item.id }}</strong>
              <span class="pill" :class="taskStatusClass(String(item.status || ''))">{{ statusLabel(String(item.status || '')) }}</span>
            </div>
            <div class="muted">{{ item.platform }} · {{ item.source_url || item.url || '-' }}</div>
            <div class="muted">{{ item.message || item.error || '' }}</div>
            <div class="actions">
              <button class="secondary mini" type="button" @click="retryJob(Number(item.id))">重试</button>
            </div>
          </article>
        </div>
      </section>

      <section v-if="activePage === 'tags'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <input v-model="tags.keyword" class="grow" placeholder="搜索 tag" @keydown.enter="loadTags" />
            <button type="button" @click="loadTags">
              <Search />
              搜索
            </button>
          </div>
          <div class="toolbar-row">
            <input v-model="tags.createName" placeholder="新增 tag" />
            <label class="muted"><input v-model="tags.createCharacter" type="checkbox" /> 角色</label>
            <button type="button" @click="createTag">新增</button>
          </div>
          <div class="toolbar-row">
            <input v-model="tags.aliasTag" placeholder="tag" />
            <input v-model="tags.aliasValue" placeholder="alias" />
            <button type="button" @click="saveAlias(false)">添加别名</button>
            <button class="secondary" type="button" @click="saveAlias(true)">删除别名</button>
          </div>
          <div class="toolbar-row">
            <input v-model="tags.characterTag" placeholder="tag" />
            <select v-model="tags.characterValue">
              <option :value="true">设为角色</option>
              <option :value="false">设为普通</option>
            </select>
            <button type="button" @click="setCharacter">提交</button>
          </div>
        </div>
        <div class="grid review-grid">
          <article v-for="item in tags.items" :key="item.name" class="item">
            <div class="title-line">
              <strong>{{ item.name }}</strong>
              <span v-if="item.is_character" class="pill success">角色</span>
              <span class="pill">{{ item.image_count || 0 }} 图</span>
            </div>
            <div class="muted">alias：{{ (item.aliases || []).join('、') || '无' }}</div>
          </article>
        </div>
      </section>

      <section v-if="activePage === 'pixiv-review'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <select v-model="pixiv.status" @change="changePixivStatus">
              <option value="pending,uncertain">待处理（待审核 / 待确认）</option>
              <option value="pending">仅待审核</option>
              <option value="uncertain">仅待确认</option>
              <option value="rejected">仅已拒绝</option>
            </select>
            <input
              v-model="pixiv.keyword"
              class="grow"
              placeholder="搜索角色 / alias / Pixiv tag，例如 mzk、Akiyama Mizuki"
              @input="schedulePixivSearch"
              @keydown.enter.prevent="submitPixivSearch"
            />
            <button class="secondary" type="button" @click="clearPixivSearch">清空搜索</button>
            <button type="button" @click="loadPixivReviewImages">刷新 Pixiv 审批</button>
          </div>
          <div class="muted">{{ pixivSearchMessage }}</div>
        </div>

        <div class="panel batch-box" :class="{ idle: !selectedPixivIds().length }">
          <div class="panel-header">
            <div>
              <h3 class="panel-title">批量审核</h3>
              <div class="muted">当前已选 {{ selectedPixivIds().length }} 张图片。</div>
            </div>
            <div class="actions">
              <button class="secondary" type="button" @click="selectAllPixiv">全选当前页</button>
              <button class="secondary" type="button" @click="clearPixivSelected">清空已选</button>
            </div>
          </div>
          <div class="batch-controls">
            <div class="toolbar-row">
              <input v-model="pixiv.batchTags" class="grow" placeholder="批量归入主 tag；多个时第 1 个为主 tag，其余作为别名词" />
              <input v-model="pixiv.batchTerms" class="grow" placeholder="批量 alias / Pixiv 来源词，逗号分隔" />
              <label class="muted"><input v-model="pixiv.batchRejectUnselected" type="checkbox" /> 拒绝未选 tag</label>
            </div>
            <div class="actions">
              <button class="secondary" type="button" @click="applyBatchTags">应用到已选图片</button>
              <button type="button" @click="previewPixivBatch">批量预览审核</button>
              <button type="button" @click="submitPixivBatch">批量确认审核</button>
            </div>
            <div v-if="pixiv.batchPreview.length" class="grid review-grid" style="margin-top: 12px;">
              <article v-for="item in pixiv.batchPreview" :key="String(item.image_id)" class="item">
                <strong>#{{ item.image_id }}</strong>
                <div class="muted">归入：{{ ((item.approved_tags as string[]) || []).join('、') || '无' }}</div>
                <div class="muted">拒绝：{{ ((item.rejected_tags as string[]) || []).join('、') || '无' }}</div>
              </article>
            </div>
          </div>
        </div>

        <div v-if="!pixiv.items.length" class="empty">
          {{ pixiv.keyword.trim() ? '没有找到相关待审图；可以先补 alias / Pixiv 平台词，或清空搜索查看默认队列。' : '当前筛选没有 Pixiv 待审图片' }}
        </div>
        <div v-else class="grid review-grid">
          <article v-for="item in pixiv.items" :key="item.image_id" class="item image-card" :class="{ selected: pixiv.selectedImages[item.image_id] }">
            <div class="thumb">
              <img :src="imageFileUrl(item.image_id)" loading="lazy" alt="" @click="openPixivPreview(item.image_id)" />
              <button type="button" @click="openPixivPreview(item.image_id)">预览</button>
            </div>
            <div class="item-body">
              <div class="title-line">
                <label><input :checked="!!pixiv.selectedImages[item.image_id]" type="checkbox" @change="togglePixivImage(item.image_id)" /> 选中</label>
                <strong>#{{ item.image_id }}</strong>
                <span class="truncate">{{ item.file_name }}</span>
              </div>
              <div class="muted">{{ item.width }}x{{ item.height }} · {{ item.author || '-' }}</div>
              <div class="muted">标题：{{ item.title || '-' }}</div>
              <div class="muted">来源：<a v-if="item.post_url" :href="item.post_url" target="_blank" rel="noreferrer">{{ item.post_url }}</a><span v-else>-</span></div>
              <div class="chips">
                <span v-for="task in item.review_tasks || []" :key="task.id" class="pill" :class="taskStatusClass(task.status)">{{ task.tag_name }}({{ statusLabel(task.status) }})</span>
              </div>
              <div class="panel review-subpanel">
                <div class="panel-header">
                  <h3 class="panel-title">归入主 tag</h3>
                  <button class="secondary mini" type="button" @click="toggleManualTagForm(item.image_id)">+</button>
                </div>
                <div v-if="pixiv.manualTagImageId === item.image_id" class="toolbar-row">
                  <input v-model="pixiv.manualTagName" class="grow" placeholder="新增主 tag" />
                  <label class="muted"><input v-model="pixiv.manualTagCharacter" type="checkbox" /> 角色</label>
                  <button class="mini" type="button" @click="createPixivMainTag(item.image_id)">添加</button>
                </div>
                <div class="chips scroll-chips">
                  <button
                    v-for="tag in item.candidate_tags || []"
                    :key="tag.name"
                    class="chip"
                    :class="{ selected: normalizeKey((pixiv.selectedTags[item.image_id] || [])[0]) === normalizeKey(tag.name) }"
                    type="button"
                    @click="toggleCandidateTag(item.image_id, tag.name)"
                  >
                    {{ tag.name }}{{ tag.is_character ? ' ·角色' : '' }}
                  </button>
                </div>
              </div>
              <div>
                <div class="muted" style="margin-bottom: 6px;">alias / Pixiv 来源词</div>
                <div class="chips scroll-chips">
                  <button
                    v-for="term in item.source_terms || []"
                    :key="`${term.origin}-${term.term}`"
                    class="chip"
                    :class="{ selected: (pixiv.selectedTerms[item.image_id] || []).some((name) => normalizeKey(name) === normalizeKey(term.term)), unresolved: !term.resolved_tag_name }"
                    :title="term.resolution || ''"
                    type="button"
                    @click="toggleSourceTerm(item.image_id, term.term, term.resolved_tag_name)"
                  >
                    {{ term.origin === 'translated' ? '译' : '原' }}·{{ term.term }}{{ term.resolved_tag_name ? ` → ${term.resolved_tag_name}` : '' }}
                  </button>
                </div>
              </div>
              <div class="muted">归入主 tag：{{ (pixiv.selectedTags[item.image_id] || [])[0] || '无' }}；alias / 搜索词：{{ (pixiv.selectedTerms[item.image_id] || []).join('、') || '无' }}</div>
              <div class="actions card-actions">
                <button type="button" @click="submitPixivReview(item.image_id)">确认审核</button>
                <button class="danger" type="button" @click="rejectPixivReviewImage(item.image_id)">拒绝图片</button>
                <button class="secondary" type="button" @click="resetPixivSelection(item.image_id)">重置</button>
              </div>
            </div>
          </article>
        </div>
        <div class="pagination-bar">
          <span class="muted">第 {{ pixiv.page }} / {{ pixiv.pageCount }} 页，共 {{ pixiv.total }} 张</span>
          <button class="secondary mini" type="button" :disabled="pixiv.page <= 1" @click="changePixivPage(pixiv.page - 1)">上一页</button>
          <button class="secondary mini" type="button" :disabled="pixiv.page >= pixiv.pageCount" @click="changePixivPage(pixiv.page + 1)">下一页</button>
          <label class="muted">跳到 <input v-model.number="pixiv.jumpPage" class="page-input" type="number" min="1" :max="pixiv.pageCount" @keydown.enter="changePixivPage(pixiv.jumpPage)" /> 页</label>
          <button class="secondary mini" type="button" @click="changePixivPage(pixiv.jumpPage)">跳转</button>
          <label class="muted">每页
            <select v-model.number="pixiv.limit" class="page-size" @change="changePixivLimit">
              <option v-for="size in pageSizeOptions" :key="`pixiv-size-${size}`" :value="size">{{ size }}</option>
            </select>
            张
          </label>
        </div>
      </section>

      <section v-if="activePage === 'pixiv-platform'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <input v-model="platform.tagFilter" placeholder="主 tag，例如 初音未来" />
            <input v-model="platform.keyword" class="grow" placeholder="Pixiv 词搜索 / 未解决词" />
            <select v-model="platform.termType">
              <option value="">全部类型</option>
              <option value="raw">raw</option>
              <option value="translated">translated</option>
              <option value="both">both</option>
            </select>
            <button type="button" @click="loadPlatformTerms">查询平台词</button>
            <button class="secondary" type="button" @click="loadPlatformSuggestions">建议词</button>
            <button class="secondary" type="button" @click="loadPlatformUnresolved">未解决词</button>
          </div>
          <div class="toolbar-row">
            <input v-model="platform.formTag" placeholder="主 tag" />
            <input v-model="platform.formTerm" class="grow" placeholder="Pixiv 词，例如 初音ミク" />
            <select v-model="platform.formType">
              <option value="both">both</option>
              <option value="raw">raw</option>
              <option value="translated">translated</option>
            </select>
            <input v-model="platform.formSource" placeholder="来源" />
            <input v-model.number="platform.formConfidence" type="number" step="0.01" min="0" max="1" style="width: 110px;" />
            <button type="button" @click="savePlatformTerm">{{ platform.formTermId ? '保存平台词' : '新增平台词' }}</button>
            <button class="secondary" type="button" @click="resetPlatformForm">重置</button>
          </div>
        </div>
        <div class="split">
          <div>
            <div v-if="platform.mode === 'terms'" class="grid review-grid">
              <article v-for="item in platform.terms" :key="String(item.id)" class="item">
                <div class="title-line">
                  <strong>{{ item.tag_name }}</strong>
                  <span class="pill">{{ item.term_type }}</span>
                </div>
                <div>{{ item.term }}</div>
                <div class="muted">alias：{{ ((item.aliases as string[]) || []).join('、') || '无' }}</div>
                <div class="actions">
                  <button class="secondary mini" type="button" @click="editPlatformTerm(item)">编辑</button>
                  <button class="danger mini" type="button" @click="deletePlatformTerm(Number(item.id))">删除</button>
                </div>
              </article>
            </div>
            <div v-if="platform.mode === 'suggestions'" class="grid review-grid">
              <article v-for="item in platform.suggestions" :key="String(item.term)" class="item">
                <strong>{{ item.term }}</strong>
                <div class="muted">出现 {{ item.count || 0 }} 次</div>
                <div class="chips">
                  <button v-for="candidate in (item.candidate_tags as string[]) || []" :key="candidate" class="chip" type="button" @click="quickSavePlatformTerm(candidate, String(item.term))">
                    映射到 {{ candidate }}
                  </button>
                </div>
              </article>
            </div>
            <div v-if="platform.mode === 'unresolved'" class="grid review-grid">
              <article v-for="item in platform.unresolved" :key="String(item.term)" class="item" :class="{ selected: platform.selectedTerms[String(item.term)] }">
                <label class="strong"><input :checked="!!platform.selectedTerms[String(item.term)]" type="checkbox" @change="toggleBulkTerm(String(item.term))" /> {{ item.term }}</label>
                <div class="muted">出现 {{ item.count || 0 }} 次</div>
                <div class="chips">
                  <button v-for="candidate in (item.candidate_tags as string[]) || []" :key="candidate" class="chip" type="button" @click="quickSavePlatformTerm(candidate, String(item.term))">
                    映射到 {{ candidate }}
                  </button>
                </div>
              </article>
            </div>
          </div>
          <div class="panel batch-box">
            <div class="panel-header">
              <h3 class="panel-title">批量映射</h3>
              <span class="muted">已选 {{ Object.values(platform.selectedTerms).filter(Boolean).length }} 个词</span>
            </div>
            <div class="toolbar-row">
              <input v-model="platform.bulkTag" placeholder="目标主 tag" />
              <input v-model="platform.bulkTerms" class="grow" placeholder="额外 Pixiv 词，逗号分隔" />
              <select v-model="platform.bulkType">
                <option value="both">both</option>
                <option value="raw">raw</option>
                <option value="translated">translated</option>
              </select>
            </div>
            <div class="toolbar-row">
              <input v-model="platform.bulkSource" placeholder="来源" />
              <input v-model.number="platform.bulkConfidence" type="number" step="0.01" min="0" max="1" style="width: 110px;" />
              <button type="button" @click="previewPlatformBulk">批量映射预览</button>
              <button type="button" @click="submitPlatformBulk">批量确认映射</button>
            </div>
            <div v-if="platform.bulkPreview.length" class="grid" style="margin-top: 12px;">
              <article v-for="item in platform.bulkPreview" :key="String(item.term)" class="item">
                <strong>{{ item.term }}</strong>
                <div class="muted">{{ item.message || item.action || '' }}</div>
              </article>
            </div>
          </div>
        </div>
      </section>

      <section v-if="activePage === 'tag-merge'" class="page">
        <div class="toolbar">
          <div class="toolbar-row">
            <input v-model="merge.keyword" class="grow" placeholder="按 source / target / 词搜索候选" @keydown.enter="loadMergeCandidates" />
            <button type="button" @click="loadMergeCandidates">刷新候选</button>
          </div>
          <div class="toolbar-row">
            <input v-model="merge.target" placeholder="目标主 tag" />
            <input v-model="merge.sources" class="grow" placeholder="来源 tag，逗号分隔" />
            <button type="button" @click="previewMerge">预览归并</button>
            <button type="button" @click="executeMerge">执行归并</button>
          </div>
        </div>
        <div class="split">
          <div class="grid review-grid">
            <article v-for="item in merge.candidates" :key="`${item.source_tag}-${item.target_tag}`" class="item">
              <div class="title-line">
                <strong>{{ item.source_tag }}</strong>
                <span>→</span>
                <strong>{{ item.target_tag }}</strong>
              </div>
              <div class="muted">score: {{ item.score ?? '-' }} · {{ item.reason || '' }}</div>
              <button class="secondary mini" type="button" @click="useMergeCandidate(String(item.source_tag), String(item.target_tag))">使用并预览</button>
            </article>
          </div>
          <div class="panel">
            <h3 class="panel-title">预览</h3>
            <pre class="json">{{ JSON.stringify(merge.preview || {}, null, 2) }}</pre>
          </div>
        </div>
      </section>
    </main>
  </div>

  <div v-if="preview.open" class="modal-mask" @click.self="closePreview">
    <section class="modal">
      <header class="modal-head">
        <div>
          <strong>#{{ preview.imageId }}</strong>
          <div class="muted">{{ preview.mode === 'pixiv' ? preview.pixivItem?.file_name : preview.imageDetail?.image?.file_name }}</div>
        </div>
        <button class="secondary" type="button" @click="closePreview">关闭</button>
      </header>
      <div class="modal-body">
        <div v-if="preview.mode === 'image' && preview.imageDetail" class="modal-grid">
          <div class="modal-image-pane">
            <img class="modal-image" :src="imageFileUrl(preview.imageId)" alt="" />
          </div>
          <div class="panel">
            <h3 class="panel-title">图片信息</h3>
            <div class="muted">{{ preview.imageDetail.image?.width || 0 }}x{{ preview.imageDetail.image?.height || 0 }} · {{ preview.imageDetail.image?.format || '-' }}</div>
            <div class="chips" style="margin: 10px 0;">
              <span v-for="tag in preview.imageDetail.tags || []" :key="String(tag.name)" class="pill" :class="taskStatusClass(String(tag.review_status || ''))">
                {{ tag.name }}{{ tag.review_status ? `(${statusLabel(String(tag.review_status))})` : '' }}
              </span>
            </div>
            <pre class="json">{{ JSON.stringify(preview.imageDetail.sources || [], null, 2) }}</pre>
          </div>
        </div>
        <div v-if="preview.mode === 'pixiv' && preview.pixivItem" class="modal-grid">
          <div class="modal-left-pane">
            <div class="modal-image-pane">
              <img class="modal-image" :src="imageFileUrl(preview.pixivItem.image_id)" alt="" />
            </div>
            <div class="panel" style="margin-top: 12px;">
              <a v-if="preview.pixivItem.post_url" :href="preview.pixivItem.post_url" target="_blank" rel="noreferrer">{{ preview.pixivItem.post_url }}</a>
              <pre class="json" style="margin-top: 10px;">{{ JSON.stringify({ raw_tags: preview.pixivItem.raw_tags || [], translated_tags: preview.pixivItem.translated_tags || [] }, null, 2) }}</pre>
            </div>
          </div>
          <div class="modal-review-pane">
            <div class="panel modal-subpanel">
              <div class="panel-header">
                <h3 class="panel-title">归入主 tag</h3>
                <button class="secondary mini" type="button" @click="toggleManualTagForm(preview.pixivItem.image_id)">+</button>
              </div>
              <div v-if="pixiv.manualTagImageId === preview.pixivItem.image_id" class="toolbar-row">
                <input v-model="pixiv.manualTagName" class="grow" placeholder="新增主 tag" />
                <label class="muted"><input v-model="pixiv.manualTagCharacter" type="checkbox" /> 角色</label>
                <button class="mini" type="button" @click="createPixivMainTag(preview.pixivItem.image_id)">添加</button>
              </div>
              <div class="chips scroll-chips">
                <button
                  v-for="tag in preview.pixivItem.candidate_tags || []"
                  :key="tag.name"
                  class="chip"
                  :class="{ selected: normalizeKey((pixiv.selectedTags[preview.pixivItem.image_id] || [])[0]) === normalizeKey(tag.name) }"
                  type="button"
                  @click="toggleCandidateTag(preview.pixivItem.image_id, tag.name)"
                >
                  {{ tag.name }}{{ tag.is_character ? ' ·角色' : '' }}
                </button>
              </div>
            </div>
            <div class="panel modal-subpanel">
              <h3 class="panel-title">alias / Pixiv 来源词</h3>
              <div class="chips scroll-chips">
                <button
                  v-for="term in preview.pixivItem.source_terms || []"
                  :key="`${term.origin}-${term.term}`"
                  class="chip"
                  :class="{ selected: (pixiv.selectedTerms[preview.pixivItem.image_id] || []).some((name) => normalizeKey(name) === normalizeKey(term.term)), unresolved: !term.resolved_tag_name }"
                  :title="term.resolution || ''"
                  type="button"
                  @click="toggleSourceTerm(preview.pixivItem.image_id, term.term, term.resolved_tag_name)"
                >
                  {{ term.origin === 'translated' ? '译' : '原' }}·{{ term.term }}{{ term.resolved_tag_name ? ` → ${term.resolved_tag_name}` : '' }}
                </button>
              </div>
              <div class="muted" style="margin-top: 8px;">归入主 tag：{{ (pixiv.selectedTags[preview.pixivItem.image_id] || [])[0] || '无' }}；alias / 搜索词：{{ (pixiv.selectedTerms[preview.pixivItem.image_id] || []).join('、') || '无' }}</div>
              <div class="actions card-actions">
                <button type="button" @click="submitPixivReview(preview.pixivItem.image_id)">确认审核</button>
                <button class="danger" type="button" @click="rejectPixivReviewImage(preview.pixivItem.image_id)">拒绝图片</button>
                <button class="secondary" type="button" @click="resetPixivSelection(preview.pixivItem.image_id)">重置</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <div v-if="toast.show" class="toast">
    <strong>{{ toast.message }}</strong>
    <small v-if="toast.detail">{{ toast.detail }}</small>
  </div>
</template>
