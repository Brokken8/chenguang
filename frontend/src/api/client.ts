import type {
  ActivitySettings,
  AdminParticipantDetail,
  AdminParticipantSummary,
  DailyCheckin,
  PaginationResponse,
  PublicDailyCheckin,
  PublicRanking,
  PublicUserStats,
  Ranking,
  SubmissionResult,
} from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "/api";
const CSRF_COOKIE_NAME = "admin_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";
const CSRF_STORAGE_KEY = "admin_csrf_token";

function readCookie(name: string): string {
  const prefix = `${name}=`;
  return (
    document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix))
      ?.slice(prefix.length) || ""
  );
}

function buildHeaders(options?: RequestInit): Headers {
  const headers = new Headers(options?.headers);
  const method = (options?.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = sessionStorage.getItem(CSRF_STORAGE_KEY) || readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
  }
  return headers;
}

function rememberCsrfToken(payload: unknown) {
  if (
    payload &&
    typeof payload === "object" &&
    "csrf_token" in payload &&
    typeof payload.csrf_token === "string"
  ) {
    sessionStorage.setItem(CSRF_STORAGE_KEY, payload.csrf_token);
  }
}

async function fetchApi<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      credentials: "include",
      ...options,
      headers: buildHeaders(options),
    });
  } catch {
    throw new Error("网络连接失败或图片过大，请检查网络、压缩图片后重试");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  const data = (await res.json()) as T;
  rememberCsrfToken(data);
  return data;
}

export async function getActivity(): Promise<ActivitySettings> {
  return fetchApi("/activity");
}

export async function getTodayCheckin(
  studentId: string,
  checkinDate?: string
): Promise<PublicDailyCheckin | null> {
  const params: Record<string, string> = { student_id: studentId };
  if (checkinDate) params.checkin_date = checkinDate;
  const query = new URLSearchParams(params).toString();
  return fetchApi(`/submissions/today?${query}`);
}

export async function getMyStats(studentId: string): Promise<PublicUserStats | null> {
  const query = new URLSearchParams({ student_id: studentId }).toString();
  return fetchApi(`/submissions/me?${query}`);
}

export async function listPublicRankings(): Promise<PublicRanking[]> {
  return fetchApi("/rankings");
}

export async function getMyAttachmentDownloadUrl(
  id: string,
  studentId: string,
  checkinDate?: string
): Promise<{ download_url: string }> {
  const params: Record<string, string> = { student_id: studentId };
  if (checkinDate) params.checkin_date = checkinDate;
  const query = new URLSearchParams(params).toString();
  return fetchApi(`/submissions/attachments/${id}/download?${query}`);
}

function isEmptyBodyFailure(status: number, detail: string) {
  return status === 422 && detail.includes("提交数据不完整");
}

type RawResult = { status: number; body: unknown };

function postXhr(url: string, data: FormData): Promise<RawResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new Error("network"));
    xhr.ontimeout = () => reject(new Error("timeout"));
    xhr.timeout = 180000;
    xhr.send(data);
  });
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function formDataToJson(data: FormData) {
  const payload: Record<string, unknown> = { attachment_item_types: [], attachments: [] };
  for (const [key, value] of data.entries()) {
    if (key === "attachments" && value instanceof File) {
      (payload.attachments as unknown[]).push({ filename: value.name, data: await fileToBase64(value) });
    } else if (key === "attachment_item_types") {
      (payload.attachment_item_types as unknown[]).push(String(value));
    } else if (key === "morning_makeup") {
      payload.morning_makeup = String(value) === "true";
    } else {
      payload[key] = String(value);
    }
  }
  return payload;
}

function detailOf(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  return typeof d === "string" ? d : fallback;
}

function reportDiag(event: string, data: FormData, extra: Record<string, unknown> = {}) {
  try {
    const files: unknown[] = [];
    let total = 0;
    let items = "";
    for (const [key, value] of data.entries()) {
      if (value instanceof File) {
        files.push({ n: value.name, s: value.size, t: value.type });
        total += value.size;
      } else if (key === "items_payload") {
        items = Object.keys(JSON.parse(String(value))).join(",");
      }
    }
    void fetch(`${API_BASE}/diag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ info: { event, items, total, files, ...extra } }),
    }).catch(() => undefined);
  } catch {
    /* 诊断失败不影响提交 */
  }
}

// 提交：先用 fetch；若请求体被丢成空（iPhone 的 QQ 等内置浏览器），依次改用 XHR、JSON+base64 重试
export async function submitForm(data: FormData): Promise<SubmissionResult> {
  const url = `${API_BASE}/submissions`;
  const attempts: (() => Promise<RawResult>)[] = [
    async () => {
      const res = await fetch(url, { method: "POST", body: data, credentials: "include" });
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    () => postXhr(url, data),
    async () => {
      const res = await fetch(`${url}/json`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(await formDataToJson(data)),
      });
      return { status: res.status, body: await res.json().catch(() => null) };
    },
  ];

  let last: RawResult | null = null;
  for (const attempt of attempts) {
    try {
      last = await attempt();
    } catch {
      last = null;
      continue; // 网络层失败，换下一种方式
    }
    if (last.status >= 200 && last.status < 300) {
      rememberCsrfToken(last.body);
      return last.body as SubmissionResult;
    }
    if (!isEmptyBodyFailure(last.status, detailOf(last.body, ""))) break; // 其它错误直接返回给用户
    reportDiag(`empty_body_attempt_${attempts.indexOf(attempt) + 1}`, data);
  }
  if (!last || last.status >= 300) reportDiag("submit_failed_final", data, { status: last?.status ?? 0 });
  if (last) throw new Error(detailOf(last.body, "提交失败，请稍后重试"));
  throw new Error("网络连接失败或图片过大，请检查网络、压缩图片后重试");
}

export async function loginAdmin(
  username: string,
  password: string
): Promise<{ success: boolean; csrf_token: string }> {
  return fetchApi("/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export async function logoutAdmin(): Promise<{ success: boolean }> {
  try {
    return await fetchApi("/admin/logout", { method: "POST" });
  } finally {
    sessionStorage.removeItem(CSRF_STORAGE_KEY);
  }
}

export async function getAdminMe(): Promise<unknown> {
  return fetchApi("/admin/me");
}

export async function listSubmissions(
  params: Record<string, string>
): Promise<PaginationResponse> {
  const query = new URLSearchParams(params).toString();
  return fetchApi(`/admin/submissions?${query}`);
}

export async function listRankings(): Promise<Ranking[]> {
  return fetchApi("/admin/rankings");
}

export async function listParticipants(): Promise<AdminParticipantSummary> {
  return fetchApi("/admin/participants");
}

export async function getParticipantDetail(studentId: string): Promise<AdminParticipantDetail> {
  return fetchApi(`/admin/participants/${encodeURIComponent(studentId)}`);
}

export async function getCheckinDetail(id: string): Promise<DailyCheckin> {
  return fetchApi(`/admin/checkins/${id}`);
}

export async function getAdminActivity(): Promise<ActivitySettings> {
  return fetchApi("/admin/activity");
}

export async function updateAdminActivity(
  payload: Pick<
    ActivitySettings,
    | "name"
    | "start_date"
    | "duration_days"
    | "checkin_start_time"
    | "checkin_end_time"
    | "morning_bonus_start_time"
    | "morning_bonus_end_time"
    | "is_active"
  >
): Promise<ActivitySettings> {
  return fetchApi("/admin/activity", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function getDownloadUrl(id: string): Promise<{ download_url: string }> {
  return fetchApi(`/admin/attachments/${id}/download`);
}

export async function revokeMakeup(id: string): Promise<{ success: boolean }> {
  return fetchApi(`/admin/submissions/${id}/revoke-makeup`, { method: "POST" });
}

export async function deleteSubmission(id: string): Promise<{ success: boolean }> {
  return fetchApi(`/admin/submissions/${id}`, { method: "DELETE" });
}

export interface MakeupReviewRecord {
  id: string;
  reviewer_type: "ai" | "human";
  reviewer_name: string;
  decision: "accept" | "reject" | "uncertain" | "error";
  confidence: number | null;
  observed_times: string[];
  reason: string | null;
  attachments_checked: number | null;
  auto_applied: boolean;
  created_at: string;
}

export interface MakeupReviewItem {
  id: string;
  name: string;
  student_id: string;
  checkin_date: string;
  status: "pending" | "needs_human" | "approved" | "rejected" | string;
  base_points: number;
  total_points: number;
  earned_morning_bonus: boolean;
  first_submitted_at: string | null;
  items: string[];
  attachments: { id: string; item_type: string; file_name: string; file_type: string }[];
  reviews: MakeupReviewRecord[];
}

export interface MakeupReviewList {
  items: MakeupReviewItem[];
  counts: Record<string, number>;
  ai_configured: boolean;
  ai_model: string;
  ai_accept_threshold: number;
  ai_reject_threshold: number;
}

export async function listMakeupReviews(): Promise<MakeupReviewList> {
  return fetchApi("/admin/makeup/reviews");
}

export async function decideMakeup(
  id: string,
  decision: "accept" | "reject",
  note: string
): Promise<{ success: boolean }> {
  return fetchApi(`/admin/makeup/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note }),
  });
}

export async function aiReviewMakeup(id: string): Promise<{ queued: number }> {
  return fetchApi(`/admin/makeup/${id}/ai-review`, { method: "POST" });
}

export async function aiReviewAllPending(): Promise<{ queued: number }> {
  return fetchApi("/admin/makeup/ai-review-pending", { method: "POST" });
}

export async function getAttachmentPreview(
  id: string,
  page = 0,
  width = 900
): Promise<{ url: string; pages: number }> {
  const res = await fetch(`${API_BASE}/admin/attachments/${id}/preview?page=${page}&width=${width}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error("预览失败");
  const pages = Number(res.headers.get("X-Page-Count") || "1");
  return { url: URL.createObjectURL(await res.blob()), pages };
}

export async function getAttachmentPreviewBlobUrl(id: string, page = 0, width = 900): Promise<string> {
  return (await getAttachmentPreview(id, page, width)).url;
}

export interface ForecastSummary {
  mean: number;
  p10: number;
  p50: number;
  p90: number;
}
export interface ForecastWinner extends ForecastSummary {
  cap: number;
  p_full: number;
}
export interface ForecastData {
  generated_at: string;
  activity: { start: string; end: string; total_days: number; today: string; day_index: number; completed_days: number };
  participants: number;
  current: { umbrella: number; streak22: number; streak20: number; streak18: number };
  umbrella: ForecastSummary;
  eligible: { streak22: ForecastSummary; streak20: ForecastSummary; streak18: ForecastSummary };
  winners: { first: ForecastWinner; second: ForecastWinner; third: ForecastWinner };
  daily: { date: string; p10: number; p50: number; p90: number }[];
  observed_daily_active: { date: string; n: number }[];
  params: {
    stay_obs: number[];
    return_obs: number[];
    stay_posterior_mean: number;
    return_by_gap?: { obs: number[]; mean: number }[];
    today_fraction?: number;
  };
  runs: number;
  compute_seconds: number;
  prior_driven: boolean;
  confidence_note: string;
}
export interface ForecastResponse {
  latest: ForecastData | null;
  history: { t: string; completed_days: number; umbrella: number; first: number; second: number; third: number }[];
}

export async function getForecast(): Promise<ForecastResponse> {
  return fetchApi("/admin/forecast");
}

export async function runForecast(): Promise<ForecastData> {
  return fetchApi("/admin/forecast/run", { method: "POST" });
}
