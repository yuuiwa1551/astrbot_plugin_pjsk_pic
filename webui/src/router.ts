import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router';

export type PageKey =
  | 'overview'
  | 'gallery'
  | 'jobs'
  | 'tags'
  | 'pixiv-review'
  | 'pixiv-platform'
  | 'tag-merge';

export const pageRoutes: Array<{ key: PageKey; path: string; label: string; title: string }> = [
  { key: 'overview', path: '/overview', label: '概览', title: '概览' },
  { key: 'gallery', path: '/gallery', label: '图片检索', title: '图片检索' },
  { key: 'jobs', path: '/jobs', label: '采集任务', title: '采集任务' },
  { key: 'tags', path: '/tags', label: 'tag 管理', title: 'tag 管理' },
  { key: 'pixiv-review', path: '/pixiv-review', label: 'Pixiv 审批', title: 'Pixiv 审批' },
  { key: 'pixiv-platform', path: '/pixiv-platform', label: 'Pixiv 平台词', title: 'Pixiv 平台词' },
  { key: 'tag-merge', path: '/tag-merge', label: 'tag 归并', title: 'tag 归并' },
];

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/overview' },
  { path: '/reviews', redirect: '/overview' },
  ...pageRoutes.map((page) => ({ path: page.path, name: page.key, component: { template: '<span />' } })),
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
];

export function normalizeEntryUrl(): void {
  const url = new URL(window.location.href);
  let changed = false;
  if (url.searchParams.has('v')) {
    url.searchParams.delete('v');
    changed = true;
  }
  const legacy = pageRoutes.find((page) => url.hash === `#${page.key}`);
  if (legacy) {
    url.hash = legacy.path;
    changed = true;
  } else if (url.hash === '#reviews') {
    url.hash = '/overview';
    changed = true;
  }
  if (!changed) return;
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
});
