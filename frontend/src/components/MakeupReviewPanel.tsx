import { useCallback, useEffect, useMemo, useState } from "react";
import {
  aiReviewAllPending,
  aiReviewMakeup,
  decideMakeup,
  getAttachmentPreview,
  getAttachmentPreviewBlobUrl,
  getDownloadUrl,
  listMakeupReviews,
} from "../api/client";
import type { MakeupReviewItem, MakeupReviewList, MakeupReviewRecord } from "../api/client";

const STATUS_LABEL: Record<string, string> = {
  pending: "等待 AI 审核",
  needs_human: "待人工审核",
  approved: "已通过",
  rejected: "已拒绝",
};
const STATUS_STYLE: Record<string, string> = {
  pending: "bg-sky-100 text-sky-800",
  needs_human: "bg-amber-100 text-amber-800",
  approved: "bg-emerald-100 text-emerald-800",
  rejected: "bg-gray-200 text-gray-600",
};
const DECISION_LABEL: Record<string, string> = {
  accept: "建议通过",
  reject: "建议拒绝",
  uncertain: "拿不准",
  error: "出错",
};
const FILTERS: { key: string; label: string }[] = [
  { key: "needs_human", label: "待人工审核" },
  { key: "pending", label: "等待 AI" },
  { key: "approved", label: "已通过" },
  { key: "rejected", label: "已拒绝" },
  { key: "all", label: "全部" },
];
const ITEM_LABEL: Record<string, string> = {
  listening: "听力",
  reading: "阅读",
  writing: "作文",
  vocabulary: "背单词",
  speaking: "口语",
  running: "跑步",
};

function fmtTime(value: string | null) {
  if (!value) return "-";
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
}

type Att = MakeupReviewItem["attachments"][number];

function useAttachmentImage(att: Att, width: number) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let alive = true;
    let objectUrl = "";
    const isPdf = att.file_type === "application/pdf";
    (isPdf
      ? getAttachmentPreviewBlobUrl(att.id, 0, width).then((u) => {
          objectUrl = u;
          return u;
        })
      : getDownloadUrl(att.id).then((r) => r.download_url)
    )
      .then((u) => alive && setSrc(u))
      .catch(() => undefined);
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [att.id, att.file_type, width]);
  return src;
}

function Thumb({ att, onOpen }: { att: Att; onOpen: () => void }) {
  const src = useAttachmentImage(att, 300);
  const isPdf = att.file_type === "application/pdf";
  return (
    <button
      type="button"
      onClick={onOpen}
      title={att.file_name}
      className="relative h-24 w-24 shrink-0 overflow-hidden rounded-lg border border-gray-200 bg-gray-50 text-xs text-gray-500 hover:ring-2 hover:ring-emerald-300"
    >
      {src ? (
        <img src={src} alt={att.file_name} className="h-full w-full object-cover" />
      ) : (
        <span className="flex h-full w-full items-center justify-center p-1 text-center">加载中</span>
      )}
      {isPdf && (
        <span className="absolute bottom-0 right-0 rounded-tl bg-black/60 px-1 text-[10px] text-white">PDF</span>
      )}
    </button>
  );
}

function Lightbox({
  atts,
  index,
  onChange,
  onClose,
}: {
  atts: Att[];
  index: number;
  onChange: (i: number) => void;
  onClose: () => void;
}) {
  const att = atts[index];
  const isPdf = att.file_type === "application/pdf";
  const [pdfPages, setPdfPages] = useState<string[]>([]);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [rawUrl, setRawUrl] = useState("");
  const [fit, setFit] = useState(true); // 默认“适合页面”，可切换原始大小
  const image = useAttachmentImage(att, 1600);
  // 适合页面：整张图在一屏内完整显示（每页 PDF 各占一屏）；原始大小：按像素原样显示，可滚动
  const imgClass = fit
    ? "max-h-[calc(100vh-5rem)] max-w-full w-auto rounded object-contain"
    : "max-w-none rounded";

  useEffect(() => {
    let alive = true;
    const urls: string[] = [];
    setPdfPages([]);
    setRawUrl("");
    getDownloadUrl(att.id)
      .then((r) => alive && setRawUrl(r.download_url))
      .catch(() => undefined);
    if (isPdf) {
      setPdfLoading(true);
      // 先取第 1 页拿到总页数，再逐页取剩余页（最多 10 页）
      (async () => {
        try {
          const first = await getAttachmentPreview(att.id, 0, 1400);
          urls.push(first.url);
          if (alive) setPdfPages([first.url]);
          for (let p = 1; p < Math.min(first.pages, 10); p += 1) {
            const next = await getAttachmentPreview(att.id, p, 1400);
            urls.push(next.url);
            if (!alive) return;
            setPdfPages([...urls]);
          }
        } catch {
          /* 预览失败时可用“新标签页打开原文件” */
        } finally {
          if (alive) setPdfLoading(false);
        }
      })();
    }
    return () => {
      alive = false;
      urls.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [att.id, isPdf]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && index > 0) onChange(index - 1);
      if (e.key === "ArrowRight" && index < atts.length - 1) onChange(index + 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, atts.length, onChange, onClose]);

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/80" onClick={onClose}>
      <div className="flex items-center gap-3 px-4 py-2 text-sm text-white" onClick={(e) => e.stopPropagation()}>
        <span className="truncate">
          {index + 1} / {atts.length} · {att.file_name}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <button
            type="button"
            disabled={index === 0}
            onClick={() => onChange(index - 1)}
            className="rounded bg-white/20 px-3 py-1 disabled:opacity-30"
          >
            上一张
          </button>
          <button
            type="button"
            disabled={index === atts.length - 1}
            onClick={() => onChange(index + 1)}
            className="rounded bg-white/20 px-3 py-1 disabled:opacity-30"
          >
            下一张
          </button>
          <button type="button" onClick={() => setFit((v) => !v)} className="rounded bg-white/20 px-3 py-1">
            {fit ? "原始大小" : "适合页面"}
          </button>
          {rawUrl && (
            <a href={rawUrl} target="_blank" rel="noreferrer" className="rounded bg-white/20 px-3 py-1">
              新标签页打开原文件
            </a>
          )}
          <button type="button" onClick={onClose} className="rounded bg-white/30 px-3 py-1">
            关闭
          </button>
        </span>
      </div>
      <div
        className={`flex-1 px-4 pb-4 ${fit ? "overflow-y-auto" : "overflow-auto"}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={`mx-auto flex flex-col items-center gap-3 ${fit ? "max-w-full" : "w-max"}`}>
          {!isPdf && (image ? <img src={image} alt={att.file_name} className={imgClass} /> : <span className="text-white">加载中…</span>)}
          {isPdf && pdfPages.map((u, k) => <img key={k} src={u} alt={`${att.file_name} 第${k + 1}页`} className={`${imgClass} bg-white`} />)}
          {isPdf && pdfLoading && pdfPages.length === 0 && <span className="text-white">加载中…</span>}
        </div>
      </div>
    </div>
  );
}

function ReviewLine({ r }: { r: MakeupReviewRecord }) {
  const who = r.reviewer_type === "ai" ? `AI · ${r.reviewer_name}` : `人工 · ${r.reviewer_name}`;
  const pct = r.confidence == null ? "" : ` · 置信度 ${Math.round(r.confidence * 100)}%`;
  const color =
    r.decision === "accept"
      ? "text-emerald-700"
      : r.decision === "reject"
        ? "text-red-600"
        : r.decision === "error"
          ? "text-red-500"
          : "text-amber-700";
  return (
    <div className="rounded-lg bg-gray-50 px-3 py-2 text-xs leading-5 text-gray-600">
      <div>
        <span className="font-semibold text-gray-800">{who}</span>
        <span className={`ml-2 font-semibold ${color}`}>
          {r.reviewer_type === "human"
            ? r.decision === "accept"
              ? "通过"
              : "拒绝"
            : DECISION_LABEL[r.decision] ?? r.decision}
          {pct}
        </span>
        {r.reviewer_type === "ai" && (
          <span className="ml-2">{r.auto_applied ? "· 已自动生效" : "· 未自动生效，转人工"}</span>
        )}
        <span className="ml-2 text-gray-400">{fmtTime(r.created_at)}</span>
      </div>
      {r.observed_times.length > 0 && <div>识别到的时间：{r.observed_times.join("；")}</div>}
      {r.reason && <div>理由：{r.reason}</div>}
    </div>
  );
}

export default function MakeupReviewPanel() {
  const [data, setData] = useState<MakeupReviewList | null>(null);
  const [filter, setFilter] = useState("needs_human");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ atts: Att[]; index: number } | null>(null);
  // 已点“AI 审核”、还在等结果的记录：id -> 点击时最新一条审核记录的 id
  const [waiting, setWaiting] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      setData(await listMakeupReviews());
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 有新的审核记录出现，就说明该条 AI 审核已完成
  useEffect(() => {
    if (!data) return;
    setWaiting((prev) => {
      const next = { ...prev };
      for (const item of data.items) {
        if (item.id in next && (item.reviews[0]?.id ?? "") !== next[item.id]) delete next[item.id];
      }
      return Object.keys(next).length === Object.keys(prev).length ? prev : next;
    });
  }, [data]);

  const waitingCount = Object.keys(waiting).length;
  useEffect(() => {
    if (waitingCount === 0) return;
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [waitingCount, load]);

  // AI 审核是后台异步进行的：有“等待 AI”的记录时，每 6 秒刷新一次
  const hasPending = (data?.counts.pending ?? 0) > 0;
  useEffect(() => {
    if (!hasPending) return;
    const timer = window.setInterval(load, 6000);
    return () => window.clearInterval(timer);
  }, [hasPending, load]);

  const shown = useMemo(
    () => (data?.items ?? []).filter((i) => filter === "all" || i.status === filter),
    [data, filter]
  );

  const decide = async (item: MakeupReviewItem, decision: "accept" | "reject") => {
    const text =
      decision === "accept"
        ? `通过 ${item.name} 的补早起？将保留早起加分。`
        : `拒绝 ${item.name} 的补早起？将取消早起加分，且该同学不能再次申请。`;
    if (!confirm(text)) return;
    const note = prompt("备注（可留空）") ?? "";
    setBusy(true);
    try {
      await decideMakeup(item.id, decision, note);
      await load();
    } catch (err) {
      alert(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const runAi = async (id?: string) => {
    setBusy(true);
    setMessage("");
    try {
      const res = id ? await aiReviewMakeup(id) : await aiReviewAllPending();
      setMessage(`已提交 ${res.queued} 条给 AI 审核，每条约需 20-60 秒，结果会自动出现`);
      setWaiting((prev) => {
        const next = { ...prev };
        for (const item of data?.items ?? []) {
          if (id ? item.id === id : item.status === "pending") next[item.id] = item.reviews[0]?.id ?? "";
        }
        return next;
      });
      await load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "触发失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => {
          const count = f.key === "all" ? data?.items.length ?? 0 : data?.counts[f.key] ?? 0;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              className={`rounded-lg border px-3 py-1.5 text-sm ${
                filter === f.key
                  ? "border-emerald-500 bg-emerald-50 text-emerald-800"
                  : "border-gray-200 bg-white text-gray-700 hover:border-gray-300"
              }`}
            >
              {f.label} {count}
            </button>
          );
        })}
        <button
          type="button"
          disabled={busy || !data?.ai_configured}
          onClick={() => runAi()}
          className="ml-auto rounded-lg bg-emerald-600 px-3 py-1.5 text-sm text-white hover:bg-emerald-700 disabled:opacity-40"
        >
          AI 审核全部「等待 AI」
        </button>
      </div>
      <p className="mb-3 text-xs leading-5 text-gray-500">
        AI 判断截图里的时间是否在 11-19 上午 6:00-8:00。建议通过且置信度 ≥{" "}
        {Math.round((data?.ai_accept_threshold ?? 0.8) * 100)}% 会自动通过，建议拒绝且置信度 ≥{" "}
        {Math.round((data?.ai_reject_threshold ?? 0.9) * 100)}% 才会自动拒绝，其余转到「待人工审核」。
        拒绝＝早起加分无效。人工可随时覆盖 AI 的结论。
        {data && !data.ai_configured && (
          <span className="ml-1 font-semibold text-red-600">尚未配置 AI 密钥，AI 审核不可用。</span>
        )}
        {data?.ai_configured && <span className="ml-1">当前模型：{data.ai_model}</span>}
      </p>
      {message && <p className="mb-3 text-sm text-emerald-700">{message}</p>}

      <div className="space-y-3">
        {shown.length === 0 && (
          <div className="rounded-lg border border-gray-200 bg-white p-8 text-center text-gray-500">暂无记录</div>
        )}
        {shown.map((item) => (
          <div key={item.id} className="rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-gray-900">{item.name}</span>
              <span className="text-sm text-gray-500">{item.student_id}</span>
              <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[item.status] ?? "bg-gray-100"}`}>
                {STATUS_LABEL[item.status] ?? item.status}
              </span>
              <span className="text-xs text-gray-500">
                提交于 {fmtTime(item.first_submitted_at)} · 项目：
                {item.items.map((t) => ITEM_LABEL[t] ?? t).join("、")} · 当前 {item.total_points} 分
                {item.earned_morning_bonus ? "（含早起分）" : "（无早起分）"}
              </span>
            </div>

            <div className="mt-3 flex flex-wrap gap-2">
              {item.attachments.map((att) => (
                <Thumb key={att.id} att={att} onOpen={() => setPreview({ atts: item.attachments, index: item.attachments.indexOf(att) })} />
              ))}
              {item.attachments.length === 0 && <span className="text-sm text-gray-400">没有补早起附件</span>}
            </div>

            <div className="mt-3 space-y-1.5">
              {item.reviews.length === 0 && <div className="text-xs text-gray-400">暂无审核记录</div>}
              {item.reviews.map((r) => (
                <ReviewLine key={r.id} r={r} />
              ))}
            </div>

            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => decide(item, "accept")}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm text-white hover:bg-emerald-700 disabled:opacity-40"
              >
                通过
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => decide(item, "reject")}
                className="rounded-lg bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700 disabled:opacity-40"
              >
                拒绝
              </button>
              <button
                type="button"
                disabled={busy || !data?.ai_configured || item.id in waiting}
                onClick={() => runAi(item.id)}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                {item.id in waiting ? "AI 审核中…（约 20-60 秒）" : "让 AI 重新审核"}
              </button>
            </div>
          </div>
        ))}
      </div>
      {preview && (
        <Lightbox
          atts={preview.atts}
          index={preview.index}
          onChange={(i) => setPreview({ atts: preview.atts, index: i })}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}
