import { useCallback, useEffect, useState } from "react";
import { getForecast, runForecast } from "../api/client";
import type { ForecastData, ForecastResponse, ForecastSummary } from "../api/client";

function Range({ s }: { s: ForecastSummary }) {
  return (
    <span>
      <span className="text-2xl font-semibold text-gray-900">{s.p50}</span>
      <span className="ml-2 text-xs text-gray-500">
        80% 区间 {s.p10} ~ {s.p90}
      </span>
    </span>
  );
}

const percent = (v: number) => `${Math.round(v * 100)}%`;

function Chart({ data }: { data: ForecastData }) {
  const obs = data.observed_daily_active;
  // 今天还没结束：当天人数不是最终值，单独标出，不连进实际曲线
  const done = obs.filter((o) => o.date !== data.activity.today);
  const partial = obs.find((o) => o.date === data.activity.today);
  const pred = data.daily;
  const all = [...obs.map((o) => o.date), ...pred.map((p) => p.date)];
  const dates = Array.from(new Set(all)).sort();
  const max = Math.max(1, ...pred.map((p) => p.p90), ...obs.map((o) => o.n)) * 1.08;
  const W = 640;
  const H = 220;
  const pad = { l: 36, r: 10, t: 10, b: 24 };
  const x = (d: string) => pad.l + (dates.indexOf(d) / Math.max(1, dates.length - 1)) * (W - pad.l - pad.r);
  const y = (v: number) => H - pad.b - (v / max) * (H - pad.t - pad.b);
  const band =
    pred.map((p) => `${x(p.date)},${y(p.p90)}`).join(" ") +
    " " +
    [...pred].reverse().map((p) => `${x(p.date)},${y(p.p10)}`).join(" ");
  const med = pred.map((p) => `${x(p.date)},${y(p.p50)}`).join(" ");
  const real = done.map((o) => `${x(o.date)},${y(o.n)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      {[0, 0.25, 0.5, 0.75, 1].map((f) => (
        <g key={f}>
          <line x1={pad.l} x2={W - pad.r} y1={y(max * f)} y2={y(max * f)} stroke="#e5e7eb" />
          <text x={pad.l - 4} y={y(max * f) + 3} fontSize="9" textAnchor="end" fill="#6b7280">
            {Math.round(max * f)}
          </text>
        </g>
      ))}
      <polygon points={band} fill="#10b981" opacity="0.18" />
      <polyline points={med} fill="none" stroke="#059669" strokeWidth="2" strokeDasharray="5 3" />
      <polyline points={real} fill="none" stroke="#111827" strokeWidth="2" />
      {done.map((o) => (
        <circle key={o.date} cx={x(o.date)} cy={y(o.n)} r="3" fill="#111827" />
      ))}
      {partial && (
        <g>
          <circle cx={x(partial.date)} cy={y(partial.n)} r="3.5" fill="#fff" stroke="#6b7280" strokeWidth="1.5" />
          <text x={x(partial.date)} y={y(partial.n) + 14} fontSize="9" textAnchor="middle" fill="#6b7280">
            今日进行中 {partial.n}
          </text>
        </g>
      )}
      {dates
        .filter((_, i) => i % Math.max(1, Math.ceil(dates.length / 8)) === 0)
        .map((d) => (
          <text key={d} x={x(d)} y={H - 6} fontSize="9" textAnchor="middle" fill="#6b7280">
            {d.slice(5)}
          </text>
        ))}
    </svg>
  );
}

export default function ForecastPanel() {
  const [res, setRes] = useState<ForecastResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setRes(await getForecast());
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const rerun = async () => {
    setBusy(true);
    setError("");
    try {
      await runForecast();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重算失败");
    } finally {
      setBusy(false);
    }
  };

  const d = res?.latest;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <span className="text-sm text-gray-600">
          {d
            ? `更新于 ${new Date(d.generated_at).toLocaleString("zh-CN", { hour12: false })} · 第 ${d.activity.day_index} / ${d.activity.total_days} 天 · 已有 ${d.activity.completed_days} 个完整打卡日 · 参与 ${d.participants} 人`
            : "还没有预测结果"}
        </span>
        <button
          type="button"
          onClick={rerun}
          disabled={busy}
          className="ml-auto rounded-lg bg-emerald-600 px-3 py-1.5 text-sm text-white hover:bg-emerald-700 disabled:opacity-40"
        >
          {busy ? "计算中…" : "立即重算"}
        </button>
      </div>
      {error && <p className="mb-3 text-sm text-red-600">{error}</p>}
      {d?.prior_driven && (
        <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm leading-6 text-amber-900">
          {d.confidence_note} 现在的数字更像"假设下的演示"，不要据此做决定；到第 4-5 天以后才有参考价值。
        </div>
      )}
      {d && !d.prior_driven && <p className="mb-3 text-xs text-gray-500">{d.confidence_note}</p>}

      {d && (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-xs text-gray-500">毅力之证（累计 ≥14 天，不限名额）</div>
              <Range s={d.umbrella} />
              <div className="mt-1 text-xs text-gray-400">目前已达成 {d.current.umbrella} 人</div>
            </div>
            {(
              [
                ["一等奖", "连续 ≥22 天，前 5 名", d.winners.first, d.eligible.streak22, d.current.streak22],
                ["二等奖", "连续 ≥20 天，前 8 名", d.winners.second, d.eligible.streak20, d.current.streak20],
                ["三等奖", "连续 ≥18 天，前 12 名", d.winners.third, d.eligible.streak18, d.current.streak18],
              ] as const
            ).map(([name, rule, w, el, cur]) => (
              <div key={name} className="rounded-lg border border-gray-200 bg-white p-3">
                <div className="text-xs text-gray-500">
                  {name}（{rule}）
                </div>
                <span className="text-2xl font-semibold text-gray-900">{w.p50}</span>
                <span className="ml-1 text-xs text-gray-500">/ {w.cap} 个名额能发出</span>
                <div className="mt-1 text-xs text-gray-500">名额发满的概率 {Math.round(w.p_full * 100)}%</div>
                <div className="text-xs text-gray-400">
                  达到连续天数条件的人：{el.p50}（{el.p10} ~ {el.p90}），目前 {cur} 人
                </div>
              </div>
            ))}
          </div>

          <div className="mb-4 rounded-lg border border-gray-200 bg-white p-3">
            <div className="mb-1 text-sm font-medium text-gray-800">每日有效打卡人数：实际与预测</div>
            <Chart data={d} />
            <div className="mt-1 text-xs text-gray-500">黑线为已结束打卡日的实际人数，空心点是今天截至更新时的人数（还会增加）；绿色虚线为预测中位数，浅绿带是 80% 区间。</div>
          </div>

          <details className="rounded-lg border border-gray-200 bg-white p-3 text-sm text-gray-600">
            <summary className="cursor-pointer text-gray-800">模型说明与局限</summary>
            <ul className="mt-2 list-disc space-y-1 pl-5 leading-6">
              <li>
                每人每天只有"打卡 / 没打卡"。分别估计"昨天打了今天还打"和"连续缺 1 天 / 2 天 / 3 天及以上后回来"的概率，
                按贝叶斯方法随数据更新，并把每个人的概率向全体平均收缩。
              </li>
              <li>
                再把剩余天数模拟 {d.runs} 次，统计每个人的累计天数和最长连续天数，套用奖项规则：
                高等奖落选的人仍可参评低等奖；毅力之证与其他奖叠加。
              </li>
              <li>
                当前全体估计：昨天打了今天继续的概率 {percent(d.params.stay_posterior_mean)}（{d.params.stay_obs.join(" / ")} 次观测）
                {d.params.return_by_gap?.map((g, i) => (
                  <span key={i}>
                    ；{["缺 1 天", "缺 2 天", "缺 3 天及以上"][i]}后回来 {percent(g.mean)}（{g.obs.join(" / ")}）
                  </span>
                ))}
                。观测少的项主要靠先验假设。
              </li>
              <li>
                今天还没打卡的人，按往常此刻之后才打卡的比例折算今天打卡的可能。
              </li>
              <li>未考虑：考试周、放假、奖项公布等会改变打卡习惯的事件；同天数同积分的名次并列；新同学加入按逐日衰减估计。</li>
              <li>"连续"按活动期内最长连续天数计算，"≥22 天"按含 22 天理解。</li>
              <li>每小时自动重算一次，单次计算约 {d.compute_seconds} 秒。2026-11-26 起启用当前模型，此前的历史预测值来自旧模型。</li>
            </ul>
          </details>

          {res && res.history.length > 1 && (
            <div className="mt-4 rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-600">
              <div className="mb-1 text-sm font-medium text-gray-800">预测值随时间的变化（中位数）</div>
              <table className="w-full text-left">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1">时间</th>
                    <th>完整日</th>
                    <th>毅力之证</th>
                    <th>一等奖</th>
                    <th>二等奖</th>
                    <th>三等奖</th>
                  </tr>
                </thead>
                <tbody>
                  {res.history.slice(-12).reverse().map((h) => (
                    <tr key={h.t} className="border-t border-gray-100">
                      <td className="py-1">{new Date(h.t).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
                      <td>{h.completed_days}</td>
                      <td>{h.umbrella}</td>
                      <td>{h.first}</td>
                      <td>{h.second}</td>
                      <td>{h.third}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
