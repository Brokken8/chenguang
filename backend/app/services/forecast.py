"""打卡留存预测：用贝叶斯 + 蒙特卡洛模拟，预测活动结束时满足各奖项条件的人数。

模型（每个同学每天只有“打卡/没打卡”，状态是“已经连续缺了几天”：0、1、2、3 天及以上）：
- 状态转移：昨天打了 → 今天继续打的概率；连续缺 1 / 2 / ≥3 天 → 今天回来打的概率。缺得越久通常越难回来。
- 全体各状态的概率用 Beta 先验 + 已观察到的转移次数更新；数据少时主要靠先验，区间会很宽。
- 每个人的概率向全体均值收缩（经验贝叶斯），收缩强度由数据里人与人的差异大小决定。
- 今天还没结束：还没打卡的人，按“往常这个时刻之后才打卡的比例”折算今天打卡的概率。
- 允许新同学陆续加入（人数逐日衰减）。
- 蒙特卡洛把剩余天数各模拟很多次，统计最终的累计天数、最长连续天数，再套用奖项规则。
纯 Python 实现，不依赖 numpy；几百人、二十多天，一次计算在几秒内。
"""
import asyncio
import json
import logging
import math
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, desc, select

from app.database import AsyncSessionLocal
from app.models import DailyCheckin, ForecastSnapshot
from app.services.activity import (
    get_checkin_date_at,
    get_or_create_activity_settings,
    parse_time_value,
    to_china_datetime,
)

logger = logging.getLogger(__name__)
CHINA_TZ = ZoneInfo("Asia/Shanghai")

# 奖项规则（来自活动公告）
UMBRELLA_DAYS = 14  # “毅力之证”：累计打卡 ≥14 天，不限名额，可与其他奖叠加
PRIZES = [  # (名称, 连续天数下限, 名额)，依次从高到低，已获高等奖者不再参与低等奖
    ("一等奖", 22, 5),
    ("二等奖", 20, 8),
    ("三等奖", 18, 12),
]

# 先验：(均值, 等效样本量)，依次对应昨天打了、连续缺 1 天、缺 2 天、缺 ≥3 天。
# 这是没有数据时的起点，随数据增多逐渐被覆盖
PRIORS = [(0.85, 6.0), (0.25, 4.0), (0.12, 4.0), (0.05, 6.0)]
N_STATES = len(PRIORS)
SHRINK_GRID = (1.0, 2.0, 4.0, 8.0, 12.0, 16.0, 32.0, 64.0)  # 个人向全体收缩强度的候选值
SHRINK_DEFAULT = 12.0


def _quantiles(values: list[float], qs=(0.1, 0.5, 0.9)) -> list[float]:
    v = sorted(values)
    n = len(v)
    return [v[min(n - 1, max(0, int(q * n)))] for q in qs]


def _load_history(rows, start: date, today: date):
    """rows: (student_id, checkin_date, base_points)。返回每人的已完成天数序列等。"""
    by: dict[str, dict[date, bool]] = {}
    for sid, d, pts in rows:
        if start <= d <= today:
            by.setdefault(sid, {})[d] = pts > 0
    completed = [start + timedelta(days=i) for i in range((today - start).days)]  # 不含今天
    students = []
    first_seen: dict[str, date] = {}
    for sid, days in by.items():
        first = min(days)
        first_seen[sid] = first
        seq = [(d, days.get(d, False)) for d in completed if d >= first]
        students.append({"sid": sid, "seq": seq, "today": days.get(today, False)})
    return students, completed, first_seen


def _next_gap(gap: int, active: bool) -> int:
    return 0 if active else min(N_STATES - 1, gap + 1)


def _counts(students):
    """返回全体与每人的各状态 [打卡次数, 缺卡次数]。只统计已完成日之间的转移。"""
    total = [[0, 0] for _ in range(N_STATES)]
    per = []
    for st in students:
        mine = [[0, 0] for _ in range(N_STATES)]
        gap = None
        for _, a in st["seq"]:
            if gap is None:
                gap = 0 if a else 1
                continue
            mine[gap][0 if a else 1] += 1
            total[gap][0 if a else 1] += 1
            gap = _next_gap(gap, a)
        per.append(mine)
    return total, per


def _current_gap(st) -> int:
    gap = None
    for _, a in st["seq"]:
        gap = (0 if a else 1) if gap is None else _next_gap(gap, a)
    return 1 if gap is None else gap


def _fit_shrink(pairs: list[list[int]], mean: float) -> float:
    """用 Beta-二项边际似然挑收缩强度：人与人差异越大，收缩越弱。数据少时偏向默认值。"""
    data = [(s, f) for s, f in pairs if s + f > 0]

    def score(k: float) -> float:
        a, b = k * mean, k * (1 - mean)
        base = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        ll = sum(base + math.lgamma(a + s) + math.lgamma(b + f) - math.lgamma(a + b + s + f) for s, f in data)
        return ll - math.log2(k / SHRINK_DEFAULT) ** 2 / (2 * 1.5**2)

    return max(SHRINK_GRID, key=score)


def _today_probability(p: float, fraction: float) -> float:
    """今天打卡概率为 p、往常此刻已有 fraction 的打卡发生时，至今未打卡的人今天仍会打卡的概率。"""
    if fraction >= 1:
        return 0.0
    return p * (1 - fraction) / max(1e-9, 1 - p * fraction)


def arrival_fraction(rows, completed: list[date], now: datetime, today: date, day_start: time) -> float:
    """rows: (checkin_date, first_valid_at)。已完成日里，到“当天同一时刻”为止到达的有效打卡占比。"""

    def offset(moment: datetime, d: date) -> timedelta:
        return to_china_datetime(moment).replace(tzinfo=None) - datetime.combine(d, day_start)

    done = set(completed)
    offsets = [offset(t, d) for d, t in rows if t is not None and d in done]
    if not offsets:
        return 0.0
    limit = offset(now, today)
    return sum(1 for o in offsets if o <= limit) / len(offsets)


def _stats_of_history(st):
    total = longest = run = 0
    for _, a in st["seq"]:
        if a:
            total += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    last_active = bool(st["seq"] and st["seq"][-1][1])
    return total, longest, run, last_active


def simulate(
    students,
    completed,
    today: date,
    end: date,
    n_runs: int = 600,
    seed: int | None = None,
    today_fraction: float = 0.0,
) -> dict:
    rng = random.Random(seed)
    rem_days = [today + timedelta(days=i) for i in range((end - today).days + 1)]  # 含今天
    n_rem = len(rem_days)
    total_counts, per = _counts(students)
    shrink = []
    for g, (m, w) in enumerate(PRIORS):
        s, f = total_counts[g]
        shrink.append(_fit_shrink([mine[g] for mine in per], (m * w + s) / (w + s + f)))
    gaps = [_current_gap(st) for st in students]
    hist = [_stats_of_history(st) for st in students]
    today_known = [st["today"] for st in students]

    # 新加入者：每天的期望新增人数，逐日衰减
    first_counts: dict[date, int] = {}
    for st in students:
        first_counts[st["seq"][0][0] if st["seq"] else today] = first_counts.get(st["seq"][0][0] if st["seq"] else today, 0) + 1
    if len(completed) >= 3:
        recent = [first_counts.get(d, 0) for d in completed[-2:]]
        lam0 = max(0.0, sum(recent) / len(recent))
    else:
        lam0 = 0.03 * len(students)
    lam = [lam0 * (0.7 ** (k + 1)) for k in range(n_rem)]

    totals_um, e22, e20, e18 = [], [], [], []
    w1s, w2s, w3s, um_totals = [], [], [], []
    daily_active = [[] for _ in range(n_rem)]
    beta = rng.betavariate

    for _ in range(n_runs):
        group = [beta(m * w + total_counts[g][0], (1 - m) * w + total_counts[g][1]) for g, (m, w) in enumerate(PRIORS)]
        days_active = [0] * n_rem
        um = c22 = c20 = c18 = 0

        def run_person(total, longest, run, gap, probs, known_today, start_idx=0):
            for k in range(start_idx, n_rem):
                if k == 0 and known_today:
                    a = True
                else:
                    p = probs[gap]
                    if k == 0:
                        p = _today_probability(p, today_fraction)
                    a = rng.random() < p
                if a:
                    total += 1
                    run += 1
                    if run > longest:
                        longest = run
                    days_active[k] += 1
                    gap = 0
                else:
                    run = 0
                    gap = min(N_STATES - 1, gap + 1)
            return total, longest

        for i, st in enumerate(students):
            probs = [
                beta(shrink[g] * group[g] + per[i][g][0], shrink[g] * (1 - group[g]) + per[i][g][1])
                for g in range(N_STATES)
            ]
            total, longest, run, _ = hist[i]
            total, longest = run_person(total, longest, run, gaps[i], probs, today_known[i])
            if total >= UMBRELLA_DAYS:
                um += 1
            if longest >= 22:
                c22 += 1
            if longest >= 20:
                c20 += 1
            if longest >= 18:
                c18 += 1

        # 新加入者：从加入当天起视为已打卡，之后按全体平均习惯
        for k in range(n_rem):
            n_new = _poisson(rng, lam[k])
            for _j in range(n_new):
                days_active[k] += 1
                total, longest = run_person(1, 1, 1, 0, group, False, start_idx=k + 1)
                if total >= UMBRELLA_DAYS:
                    um += 1
                if longest >= 22:
                    c22 += 1
                if longest >= 20:
                    c20 += 1
                if longest >= 18:
                    c18 += 1

        # 奖项名额级联：高等奖落选/未入选者仍可参与低等奖
        w1 = min(PRIZES[0][2], c22)
        w2 = min(PRIZES[1][2], max(0, c20 - w1))
        w3 = min(PRIZES[2][2], max(0, c18 - w1 - w2))
        um_totals.append(um)
        e22.append(c22)
        e20.append(c20)
        e18.append(c18)
        w1s.append(w1)
        w2s.append(w2)
        w3s.append(w3)
        for k in range(n_rem):
            daily_active[k].append(days_active[k])

    def summ(vals):
        p10, p50, p90 = _quantiles(vals)
        return {"mean": round(sum(vals) / len(vals), 1), "p10": p10, "p50": p50, "p90": p90}

    return {
        "umbrella": summ(um_totals),
        "eligible": {"streak22": summ(e22), "streak20": summ(e20), "streak18": summ(e18)},
        "winners": {
            "first": {**summ(w1s), "cap": PRIZES[0][2], "p_full": round(sum(1 for x in w1s if x >= PRIZES[0][2]) / n_runs, 3)},
            "second": {**summ(w2s), "cap": PRIZES[1][2], "p_full": round(sum(1 for x in w2s if x >= PRIZES[1][2]) / n_runs, 3)},
            "third": {**summ(w3s), "cap": PRIZES[2][2], "p_full": round(sum(1 for x in w3s if x >= PRIZES[2][2]) / n_runs, 3)},
        },
        "daily": [
            {"date": rem_days[k].isoformat(), **dict(zip(("p10", "p50", "p90"), _quantiles(daily_active[k])))}
            for k in range(n_rem)
        ],
        "params": {
            "stay_obs": [total_counts[0][0], sum(total_counts[0])],
            "return_obs": [sum(c[0] for c in total_counts[1:]), sum(sum(c) for c in total_counts[1:])],
            "stay_posterior_mean": _posterior_mean(total_counts, 0),
            "return_by_gap": [
                {"obs": [total_counts[g][0], sum(total_counts[g])], "mean": _posterior_mean(total_counts, g)}
                for g in range(1, N_STATES)
            ],
            "shrink": shrink,
            "today_fraction": round(today_fraction, 3),
            "new_joiners_day0": round(lam0, 1),
        },
    }


def _posterior_mean(total_counts, g: int) -> float:
    m, w = PRIORS[g]
    s, f = total_counts[g]
    return round((m * w + s) / (w + s + f), 3)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


async def compute_forecast(n_runs: int = 600) -> dict:
    async with AsyncSessionLocal() as db:
        activity = await get_or_create_activity_settings(db)
        start = activity.start_date
        end = start + timedelta(days=activity.duration_days - 1)
        now = datetime.now(CHINA_TZ)
        today = get_checkin_date_at(activity, now)
        rows = (
            await db.execute(
                select(
                    DailyCheckin.student_id,
                    DailyCheckin.checkin_date,
                    DailyCheckin.base_points,
                    DailyCheckin.first_valid_at,
                ).where(
                    DailyCheckin.checkin_date >= start, DailyCheckin.checkin_date <= end
                )
            )
        ).all()

    # 活动结束后最后一天也算作已完成
    cutoff = min(today, end + timedelta(days=1))
    students, completed, _ = _load_history([r[:3] for r in rows], start, cutoff)
    fraction = arrival_fraction(
        [(d, t) for _, d, pts, t in rows if pts > 0],
        completed,
        to_china_datetime(now),
        cutoff,
        parse_time_value(activity.checkin_start_time),
    )
    day_index = (min(today, end) - start).days + 1
    observed_daily = {}
    for _sid, d, pts, _t in rows:
        if pts > 0:
            observed_daily[d] = observed_daily.get(d, 0) + 1
    t0 = datetime.now()
    result = await asyncio.to_thread(simulate, students, completed, cutoff, end, n_runs, None, fraction)
    # 当前实际已满足条件的人数（不含预测）
    cur_um = cur22 = cur20 = cur18 = 0
    for st in students:
        total, longest, run, _ = _stats_of_history(st)
        if st["today"]:
            total += 1
            run += 1
            longest = max(longest, run)
        cur_um += total >= UMBRELLA_DAYS
        cur22 += longest >= 22
        cur20 += longest >= 20
        cur18 += longest >= 18
    result.update(
        {
            "generated_at": datetime.now(CHINA_TZ).isoformat(),
            "activity": {"start": start.isoformat(), "end": end.isoformat(), "total_days": activity.duration_days,
                         "today": today.isoformat(), "day_index": day_index, "completed_days": len(completed)},
            "participants": len(students),
            "observed_daily_active": [{"date": d.isoformat(), "n": observed_daily[d]} for d in sorted(observed_daily)],
            "current": {"umbrella": cur_um, "streak22": cur22, "streak20": cur20, "streak18": cur18},
            "runs": n_runs,
            "compute_seconds": round((datetime.now() - t0).total_seconds(), 2),
            "prior_driven": len(completed) < 3,
            "confidence_note": (
                "目前只有 %d 个已完成的打卡日，预测主要由先验假设决定，区间很宽，仅供参考。" % len(completed)
                if len(completed) < 5
                else "已有 %d 个完整打卡日的数据，预测会随数据增加逐渐收窄。" % len(completed)
            ),
        }
    )
    return result


async def compute_and_store(n_runs: int = 600) -> dict:
    result = await compute_forecast(n_runs)
    async with AsyncSessionLocal() as db:
        db.add(ForecastSnapshot(created_at=datetime.now(CHINA_TZ), payload=json.dumps(result, ensure_ascii=False)))
        await db.commit()
        # 只保留最近 300 份
        ids = [r[0] for r in (await db.execute(select(ForecastSnapshot.id).order_by(desc(ForecastSnapshot.created_at)).offset(300))).all()]
        if ids:
            await db.execute(delete(ForecastSnapshot).where(ForecastSnapshot.id.in_(ids)))
            await db.commit()
    return result


if __name__ == "__main__":
    out = asyncio.run(compute_and_store())
    print(f"forecast ok participants={out['participants']} seconds={out['compute_seconds']}")
