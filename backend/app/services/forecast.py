"""打卡留存预测：用贝叶斯 + 蒙特卡洛模拟，预测活动结束时满足各奖项条件的人数。

模型（每个同学每天只有“打卡/没打卡”两种状态）：
- 状态转移：昨天打了 → 今天继续打的概率 p_stay；昨天没打 → 今天回来打的概率 p_return。
- 全体的 p_stay、p_return 用 Beta 先验 + 已观察到的转移次数更新；数据少时主要靠先验，区间会很宽。
- 每个人的概率向全体均值收缩（经验贝叶斯）：自己的历史越多，越接近自己的习惯。
- 允许新同学陆续加入（人数逐日衰减）。
- 蒙特卡洛把剩余天数各模拟很多次，统计最终的累计天数、最长连续天数，再套用奖项规则。
纯 Python 实现，不依赖 numpy；几百人、二十多天，一次计算在几秒内。
"""
import asyncio
import json
import logging
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, desc, select

from app.database import AsyncSessionLocal
from app.models import DailyCheckin, ForecastSnapshot
from app.services.activity import get_checkin_date_at, get_or_create_activity_settings

logger = logging.getLogger(__name__)
CHINA_TZ = ZoneInfo("Asia/Shanghai")

# 奖项规则（来自活动公告）
UMBRELLA_DAYS = 14  # “毅力之证”：累计打卡 ≥14 天，不限名额，可与其他奖叠加
PRIZES = [  # (名称, 连续天数下限, 名额)，依次从高到低，已获高等奖者不再参与低等奖
    ("一等奖", 22, 5),
    ("二等奖", 20, 8),
    ("三等奖", 18, 12),
]

# 先验：(均值, 等效样本量)。这是没有数据时的起点，随数据增多逐渐被覆盖
PRIOR_STAY = (0.85, 6.0)
PRIOR_RETURN = (0.20, 4.0)
SHRINK_K = 12.0  # 个人向全体均值收缩的强度


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


def _counts(students):
    S = B = R = O = 0
    per = []
    for st in students:
        s = b = r = o = 0
        prev = None
        for _, a in st["seq"]:
            if prev is not None:
                if prev and a:
                    s += 1
                elif prev and not a:
                    b += 1
                elif (not prev) and a:
                    r += 1
                else:
                    o += 1
            prev = a
        # 昨天(最后一个已完成日) → 今天，只有今天已打卡的才算已知转移，避免半天数据偏向“没打”
        per.append((s, b, r, o))
        S, B, R, O = S + s, B + b, R + r, O + o
    return S, B, R, O, per


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


def simulate(students, completed, today: date, end: date, n_runs: int = 600, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    rem_days = [today + timedelta(days=i) for i in range((end - today).days + 1)]  # 含今天
    n_rem = len(rem_days)
    S, B, R, O, per = _counts(students)
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
        ps_g = beta(PRIOR_STAY[0] * PRIOR_STAY[1] + S, (1 - PRIOR_STAY[0]) * PRIOR_STAY[1] + B)
        pr_g = beta(PRIOR_RETURN[0] * PRIOR_RETURN[1] + R, (1 - PRIOR_RETURN[0]) * PRIOR_RETURN[1] + O)
        days_active = [0] * n_rem
        um = c22 = c20 = c18 = 0

        def run_person(total, longest, run, last_active, ps, pr, known_today, start_idx=0):
            for k in range(start_idx, n_rem):
                if k == 0 and known_today:
                    a = True
                else:
                    a = rng.random() < (ps if last_active else pr)
                if a:
                    total += 1
                    run += 1
                    if run > longest:
                        longest = run
                    days_active[k] += 1
                else:
                    run = 0
                last_active = a
            return total, longest

        for i, st in enumerate(students):
            s, b, r, o = per[i]
            ps_i = beta(SHRINK_K * ps_g + s, SHRINK_K * (1 - ps_g) + b)
            pr_i = beta(SHRINK_K * pr_g + r, SHRINK_K * (1 - pr_g) + o)
            total, longest, run, last = hist[i]
            total, longest = run_person(total, longest, run, last, ps_i, pr_i, today_known[i])
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
                total, longest = run_person(1, 1, 1, True, ps_g, pr_g, False, start_idx=k + 1)
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
            "stay_obs": [S, S + B],
            "return_obs": [R, R + O],
            "stay_posterior_mean": round((PRIOR_STAY[0] * PRIOR_STAY[1] + S) / (PRIOR_STAY[1] + S + B), 3),
            "return_posterior_mean": round((PRIOR_RETURN[0] * PRIOR_RETURN[1] + R) / (PRIOR_RETURN[1] + R + O), 3),
            "new_joiners_day0": round(lam0, 1),
        },
    }


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    import math

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
                select(DailyCheckin.student_id, DailyCheckin.checkin_date, DailyCheckin.base_points).where(
                    DailyCheckin.checkin_date >= start, DailyCheckin.checkin_date <= end
                )
            )
        ).all()

    students, completed, _ = _load_history(rows, start, min(today, end))
    day_index = (min(today, end) - start).days + 1
    observed_daily = {}
    for _sid, d, pts in rows:
        if pts > 0:
            observed_daily[d] = observed_daily.get(d, 0) + 1
    t0 = datetime.now()
    result = await asyncio.to_thread(simulate, students, completed, min(today, end), end, n_runs)
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
