export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export function apiUrl(path: string): string {
  return path.startsWith('/') ? path : `/${path}`;
}

export async function fetchJson<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(apiUrl(path), {
    ...options,
    headers,
    credentials: 'same-origin',
  });
  const text = await response.text();
  let payload: any = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { message: text };
    }
  }
  if (!response.ok) {
    throw new ApiError(String(payload.message || payload.error || response.statusText), response.status, payload);
  }
  return payload as T;
}

export function imageFileUrl(imageId: number | string): string {
  return apiUrl(`/api/image-file?image_id=${encodeURIComponent(String(imageId))}`);
}
