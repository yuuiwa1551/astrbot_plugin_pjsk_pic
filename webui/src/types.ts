export type Dict<T = any> = Record<string, T>;

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  page: number;
  page_count: number;
  [key: string]: unknown;
}

export interface SummaryStats {
  images?: number;
  tags?: number;
  aliases?: number;
  crawl_jobs?: number;
  pending_reviews?: number;
  [key: string]: unknown;
}

export interface SourceInfo {
  platform?: string;
  post_url?: string;
  image_url?: string;
  author?: string;
  raw_tags?: string[];
  extra?: Dict;
}

export interface ReviewTask {
  id: number;
  image_id?: number;
  tag_id?: number;
  tag_name: string;
  status: string;
  reason?: string;
  manual_result?: string;
  model_result?: string;
  source_type?: string;
  is_character?: boolean;
}

export interface ImageItem {
  id: number;
  image_id?: number;
  file_name?: string;
  width?: number;
  height?: number;
  format?: string;
  phash?: string;
  updated_at?: string;
  tags?: Dict[];
  sources?: SourceInfo[];
  platform?: string;
  post_url?: string;
  review_tasks?: ReviewTask[];
  similar_image_ids?: number[];
}

export interface TagItem {
  id?: number;
  name: string;
  is_character?: boolean;
  image_count?: number;
  aliases?: string[];
}

export interface PixivSourceTerm {
  term: string;
  origin: 'raw' | 'translated' | string;
  resolved_tag_name?: string;
  resolution?: string;
  match_type?: string;
  candidate_tags?: string[];
}

export interface PixivReviewItem {
  image_id: number;
  file_name?: string;
  width?: number;
  height?: number;
  format?: string;
  author?: string;
  post_url?: string;
  image_url?: string;
  title?: string;
  raw_tags?: string[];
  translated_tags?: string[];
  source_terms?: PixivSourceTerm[];
  candidate_tags?: TagItem[];
  review_tasks?: ReviewTask[];
  current_tags?: Dict[];
}
