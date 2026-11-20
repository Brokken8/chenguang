import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, APIRouter, Response, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, asc, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from pydantic import BaseModel
from app.models import Admin, CheckinAttachment, CheckinItem, DailyCheckin, MakeupReview
from app.services.ai_review import load_config as load_ai_config, run_ai_review
from app.services.makeup import apply_decision
from app.services import forecast as forecast_service
from app.models import ForecastSnapshot
from app.schemas import (
    AdminParticipantDetail,
    AdminParticipantSummary,
    ActivitySettingsOut,
    ActivitySettingsUpdate,
    AdminLogin,
    AdminOut,
    AdminSessionOut,
    DailyCheckinOut,
    RankingOut,
)
from app.services.activity import (
    MAX_TOTAL_POINTS,
    MORNING_BONUS_POINTS,
    build_activity_response,
    get_or_create_activity_settings,
    should_earn_morning_bonus,
)
from app.services.auth import verify_password
from app.services.rankings import build_ranking_rows, ranking_sort_key
from app.services.session import (
    COOKIE_NAME,
    CSRF_COOKIE_NAME,
    create_csrf_token,
    create_session_cookie,
    get_current_admin_session,
    get_current_admin_username,
    require_admin_csrf,
)
from app.services.oss import get_oss_client

CHINA_TZ = ZoneInfo("Asia/Shanghai")
router = APIRouter(prefix="/api/admin", tags=["admin"])

ITEM_ORDER = ["listening", "reading", "writing", "vocabulary", "speaking", "running"]
LOGIN_FAILURES: dict[str, dict[str, float | int]] = {}


def _login_rate_key(request: Request, username: str) -> str:
    client_host = request.client.host if request.client else "-"
    return f"{client_host}:{username.strip().lower()}"


def _check_login_rate_limit(key: str) -> None:
    now = time.monotonic()
    failure = LOGIN_FAILURES.get(key)
    if not failure:
        return

    locked_until = float(failure.get("locked_until", 0))
    if locked_until > now:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")

    first_failed_at = float(failure.get("first_failed_at", now))
    if now - first_failed_at > settings.admin_login_window_seconds:
        LOGIN_FAILURES.pop(key, None)


def _record_login_failure(key: str) -> None:
    now = time.monotonic()
    failure = LOGIN_FAILURES.get(key)
    if (
        not failure
        or now - float(failure.get("first_failed_at", now)) > settings.admin_login_window_seconds
    ):
        failure = {"count": 0, "first_failed_at": now, "locked_until": 0}

    failure["count"] = int(failure.get("count", 0)) + 1
    if int(failure["count"]) >= settings.admin_login_max_attempts:
        failure["locked_until"] = now + settings.admin_login_lockout_seconds
    LOGIN_FAILURES[key] = failure


@router.post("/login")
async def admin_login(
    payload: AdminLogin,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    rate_key = _login_rate_key(request, payload.username)
    _check_login_rate_limit(rate_key)

    result = await db.execute(select(Admin).where(Admin.username == payload.username))
    admin = result.scalar_one_or_none()

    if not admin:
        _record_login_failure(rate_key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not await verify_password(payload.password, admin.password_hash):
        _record_login_failure(rate_key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    LOGIN_FAILURES.pop(rate_key, None)
    csrf_token = create_csrf_token()
    cookie = create_session_cookie(admin.username, csrf_token)
    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie,
        httponly=True,
        secure=settings.effective_admin_cookie_secure,
        samesite="lax",
        max_age=86400,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=settings.effective_admin_cookie_secure,
        samesite="lax",
        max_age=86400,
    )
    return {"success": True, "csrf_token": csrf_token}


@router.post("/logout")
async def admin_logout(
    response: Response,
    _: str = Depends(require_admin_csrf),
):
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.effective_admin_cookie_secure,
        samesite="lax",
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        httponly=False,
        secure=settings.effective_admin_cookie_secure,
        samesite="lax",
    )
    return {"success": True}


@router.get("/me", response_model=AdminSessionOut)
async def admin_me(
    session_data: dict = Depends(get_current_admin_session),
    db: AsyncSession = Depends(get_db),
):
    username = session_data["username"]
    result = await db.execute(select(Admin).where(Admin.username == username))
    admin = result.scalar_one_or_none()
    if not admin:
        raise HTTPException(status_code=401, detail="管理员不存在")
    return {
        "id": admin.id,
        "username": admin.username,
        "created_at": admin.created_at,
        "csrf_token": session_data["csrf_token"],
    }


@router.get("/submissions", response_model=dict)
async def list_submissions(
    name: str = Query(None),
    student_id: str = Query(None),
    start_date: str = Query(None),
    end_date: str = Query(None),
    makeup: str = Query(None),
    sort_by: str = Query("checkin_date"),
    sort_order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(DailyCheckin).options(
        selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments)
    )
    count_stmt = select(func.count()).select_from(DailyCheckin)

    if name:
        stmt = stmt.where(DailyCheckin.name.ilike(f"%{name}%"))
        count_stmt = count_stmt.where(DailyCheckin.name.ilike(f"%{name}%"))

    if student_id:
        stmt = stmt.where(DailyCheckin.student_id.ilike(f"%{student_id}%"))
        count_stmt = count_stmt.where(DailyCheckin.student_id.ilike(f"%{student_id}%"))

    if start_date:
        stmt = stmt.where(DailyCheckin.checkin_date >= start_date)
        count_stmt = count_stmt.where(DailyCheckin.checkin_date >= start_date)

    if end_date:
        stmt = stmt.where(DailyCheckin.checkin_date <= end_date)
        count_stmt = count_stmt.where(DailyCheckin.checkin_date <= end_date)

    if makeup == "1":
        stmt = stmt.where(DailyCheckin.makeup_bonus.is_(True))
        count_stmt = count_stmt.where(DailyCheckin.makeup_bonus.is_(True))

    if sort_by in {"name", "student_id", "checkin_date", "base_points", "total_points", "updated_at"}:
        order_col = getattr(DailyCheckin, sort_by)
        if sort_order == "asc":
            stmt = stmt.order_by(asc(order_col))
        else:
            stmt = stmt.order_by(desc(order_col))
    else:
        stmt = stmt.order_by(desc(DailyCheckin.checkin_date), desc(DailyCheckin.updated_at))

    total_result = await db.execute(count_stmt)
    total = total_result.scalar()

    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await db.execute(stmt)
    items = result.scalars().all()
    for checkin in items:
        checkin.items.sort(key=lambda item: ITEM_ORDER.index(item.item_type))

    return {
        "items": [DailyCheckinOut.model_validate(checkin) for checkin in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/rankings", response_model=list[RankingOut])
async def list_rankings(
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    activity = await get_or_create_activity_settings(db)
    end_date = activity.start_date + timedelta(days=activity.duration_days - 1)
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.checkin_date >= activity.start_date)
        .where(DailyCheckin.checkin_date <= end_date)
        .options(selectinload(DailyCheckin.items))
    )
    checkins = result.scalars().all()

    rows = build_ranking_rows(checkins)
    rows.sort(key=ranking_sort_key)

    ranked_rows = []
    for index, row in enumerate(rows):
        ranked_row = {**row, "rank": index + 1, "total_volume": round(row["total_volume"], 4)}
        ranked_rows.append(RankingOut(**ranked_row))
    return ranked_rows


@router.get("/participants", response_model=AdminParticipantSummary)
async def list_participants(
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    activity = await get_or_create_activity_settings(db)
    end_date = activity.start_date + timedelta(days=activity.duration_days - 1)
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.checkin_date >= activity.start_date)
        .where(DailyCheckin.checkin_date <= end_date)
        .options(selectinload(DailyCheckin.items))
    )
    checkins = result.scalars().all()
    rows = build_ranking_rows(checkins)
    rows.sort(key=ranking_sort_key)
    ranked = [
        RankingOut(rank=index + 1, **{**row, "total_volume": round(row["total_volume"], 4)})
        for index, row in enumerate(rows)
    ]
    return {
        "total_students": len(rows),
        "valid_checkin_records": sum(1 for checkin in checkins if checkin.base_points > 0),
        "students": ranked,
    }


@router.get("/participants/{student_id}", response_model=AdminParticipantDetail)
async def get_participant_detail(
    student_id: str,
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    activity = await get_or_create_activity_settings(db)
    end_date = activity.start_date + timedelta(days=activity.duration_days - 1)
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.student_id == student_id)
        .where(DailyCheckin.checkin_date >= activity.start_date)
        .where(DailyCheckin.checkin_date <= end_date)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
        .order_by(desc(DailyCheckin.checkin_date))
    )
    checkins = result.scalars().all()
    if not checkins:
        raise HTTPException(status_code=404, detail="同学不存在")

    for checkin in checkins:
        checkin.items.sort(key=lambda item: ITEM_ORDER.index(item.item_type))

    row = build_ranking_rows(checkins)[0]
    summary = RankingOut(rank=1, **{**row, "total_volume": round(row["total_volume"], 4)})
    return {
        "summary": summary,
        "checkins": [DailyCheckinOut.model_validate(checkin) for checkin in checkins],
    }


@router.get("/checkins/{checkin_id}", response_model=DailyCheckinOut)
async def get_checkin_detail(
    checkin_id: str,
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.id == checkin_id)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    checkin = result.scalar_one_or_none()
    if not checkin:
        raise HTTPException(status_code=404, detail="打卡记录不存在")
    checkin.items.sort(key=lambda item: ITEM_ORDER.index(item.item_type))
    return DailyCheckinOut.model_validate(checkin)


@router.get("/activity", response_model=ActivitySettingsOut)
async def get_admin_activity(
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    activity = await get_or_create_activity_settings(db)
    await db.commit()
    return build_activity_response(activity)


@router.put("/activity", response_model=ActivitySettingsOut)
async def update_admin_activity(
    payload: ActivitySettingsUpdate,
    _: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="活动名称不能为空")
    if payload.duration_days <= 0:
        raise HTTPException(status_code=400, detail="持续天数必须大于 0")
    for field_name, value in {
        "打卡开始时间": payload.checkin_start_time,
        "打卡结束时间": payload.checkin_end_time,
        "早起加分开始时间": payload.morning_bonus_start_time,
        "早起加分结束时间": payload.morning_bonus_end_time,
    }.items():
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{field_name}格式必须为 HH:MM")

    activity = await get_or_create_activity_settings(db)
    activity.name = payload.name.strip()
    activity.start_date = payload.start_date
    activity.duration_days = payload.duration_days
    activity.checkin_start_time = payload.checkin_start_time
    activity.checkin_end_time = payload.checkin_end_time
    activity.morning_bonus_start_time = payload.morning_bonus_start_time
    activity.morning_bonus_end_time = payload.morning_bonus_end_time
    activity.is_active = payload.is_active
    await _recalculate_activity_morning_bonus(db, activity)
    await db.commit()
    await db.refresh(activity)
    return build_activity_response(activity)


async def _recalculate_activity_morning_bonus(db: AsyncSession, activity) -> None:
    result = await db.execute(select(DailyCheckin))

    for checkin in result.scalars():
        earned_morning_bonus = should_earn_morning_bonus(
            activity,
            checkin.checkin_date,
            checkin.first_valid_at,
            checkin.base_points,
        )
        bonus_points = MORNING_BONUS_POINTS if earned_morning_bonus else 0
        total_points = min(checkin.base_points + bonus_points, MAX_TOTAL_POINTS)
        if (
            checkin.earned_morning_bonus != earned_morning_bonus
            or checkin.total_points != total_points
        ):
            checkin.earned_morning_bonus = earned_morning_bonus
            checkin.total_points = total_points


@router.get("/attachments/{attachment_id}/preview")
async def preview_attachment(
    attachment_id: str,
    page: int = Query(0, ge=0, le=20),
    width: int = Query(900, ge=200, le=2000),
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    """把 PDF 的第 page 页渲染成 JPEG，方便浏览器直接当图片显示。"""
    import asyncio
    import io

    attachment = (
        await db.execute(select(CheckinAttachment).where(CheckinAttachment.id == attachment_id))
    ).scalar_one_or_none()
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")

    def render() -> tuple[bytes, int]:
        import fitz
        from PIL import Image

        client = get_oss_client()
        raw = client.client.get_object(Bucket=client.bucket_name, Key=attachment.oss_key)["Body"].read()
        doc = fitz.open(stream=raw, filetype="pdf")
        count = len(doc)
        pg = doc[min(page, count - 1)]
        zoom = width / max(pg.rect.width, 1)
        png = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
        with Image.open(io.BytesIO(png)) as im:
            out = io.BytesIO()
            im.convert("RGB").save(out, format="JPEG", quality=82)
        return out.getvalue(), count

    try:
        data, count = await asyncio.to_thread(render)
    except Exception:
        raise HTTPException(status_code=422, detail="无法预览该文件")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"X-Page-Count": str(count), "Cache-Control": "private, max-age=300"},
    )


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: str,
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CheckinAttachment).where(CheckinAttachment.id == attachment_id))
    attachment = result.scalar_one_or_none()

    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")

    presigned_url = get_oss_client().get_presigned_url(attachment.oss_key, expire=3600)
    return {"download_url": presigned_url}


@router.post("/submissions/{submission_id}/revoke-makeup")
async def revoke_makeup(
    submission_id: str,
    admin_username: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await _human_decision(db, submission_id, "reject", "管理员在记录列表中撤销", admin_username)


async def _human_decision(db: AsyncSession, checkin_id: str, decision: str, note: str, admin_username: str):
    if decision not in {"accept", "reject"}:
        raise HTTPException(status_code=400, detail="decision 必须是 accept 或 reject")
    checkin = (await db.execute(select(DailyCheckin).where(DailyCheckin.id == checkin_id))).scalar_one_or_none()
    if not checkin:
        raise HTTPException(status_code=404, detail="记录不存在")
    if not checkin.makeup_bonus:
        raise HTTPException(status_code=400, detail="该记录不是补早起")
    apply_decision(checkin, decision == "accept")
    db.add(
        MakeupReview(
            checkin_id=checkin.id,
            reviewer_type="human",
            reviewer_name=admin_username,
            decision=decision,
            reason=(note or "").strip()[:1000] or None,
            auto_applied=True,
            created_at=datetime.now(CHINA_TZ),
        )
    )
    await db.commit()
    return {"success": True, "status": checkin.makeup_status, "total_points": checkin.total_points}


def _iso_cn(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None).isoformat() + "+08:00"


@router.get("/forecast")
async def get_forecast(
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(ForecastSnapshot).order_by(desc(ForecastSnapshot.created_at)).limit(120))
    ).scalars().all()
    if not rows:
        return {"latest": None, "history": []}
    latest = json.loads(rows[0].payload)
    history = []
    for r in reversed(rows):
        p = json.loads(r.payload)
        history.append(
            {
                "t": p.get("generated_at"),
                "completed_days": p.get("activity", {}).get("completed_days"),
                "umbrella": p["umbrella"]["p50"],
                "first": p["winners"]["first"]["p50"],
                "second": p["winners"]["second"]["p50"],
                "third": p["winners"]["third"]["p50"],
            }
        )
    return {"latest": latest, "history": history}


@router.post("/forecast/run")
async def run_forecast_now(_: str = Depends(require_admin_csrf)):
    return await forecast_service.compute_and_store()


@router.get("/makeup/reviews")
async def list_makeup_reviews(
    _: str = Depends(get_current_admin_username),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.makeup_bonus.is_(True))
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
        .order_by(asc(DailyCheckin.first_submitted_at))
    )
    checkins = result.scalars().all()
    ids = [c.id for c in checkins]
    reviews_by: dict[str, list] = {}
    if ids:
        rr = await db.execute(
            select(MakeupReview).where(MakeupReview.checkin_id.in_(ids)).order_by(desc(MakeupReview.created_at))
        )
        for r in rr.scalars():
            reviews_by.setdefault(r.checkin_id, []).append(r)

    cfg = load_ai_config()
    items = []
    counts: dict[str, int] = {}
    for c in checkins:
        counts[c.makeup_status] = counts.get(c.makeup_status, 0) + 1
        atts = [
            {"id": a.id, "item_type": it.item_type, "file_name": a.file_name, "file_type": a.file_type}
            for it in c.items
            for a in it.attachments
            if "补早起" in (a.file_name or "")
        ]
        items.append(
            {
                "id": c.id,
                "name": c.name,
                "student_id": c.student_id,
                "checkin_date": c.checkin_date.isoformat(),
                "status": c.makeup_status,
                "base_points": c.base_points,
                "total_points": c.total_points,
                "earned_morning_bonus": c.earned_morning_bonus,
                "first_submitted_at": _iso_cn(c.first_submitted_at),
                "items": [it.item_type for it in c.items],
                "attachments": atts,
                "reviews": [
                    {
                        "id": r.id,
                        "reviewer_type": r.reviewer_type,
                        "reviewer_name": r.reviewer_name,
                        "decision": r.decision,
                        "confidence": r.confidence,
                        "observed_times": json.loads(r.observed_times) if r.observed_times else [],
                        "reason": r.reason,
                        "attachments_checked": r.attachments_checked,
                        "auto_applied": r.auto_applied,
                        "created_at": _iso_cn(r.created_at),
                    }
                    for r in reviews_by.get(c.id, [])
                ],
            }
        )
    return {
        "items": items,
        "counts": counts,
        "ai_configured": bool(cfg["api_key"]),
        "ai_model": cfg["model"],
        "ai_accept_threshold": cfg["accept_threshold"],
        "ai_reject_threshold": cfg["reject_threshold"],
    }


class MakeupDecisionIn(BaseModel):
    decision: str
    note: str | None = None


@router.post("/makeup/{checkin_id}/decision")
async def makeup_decision(
    checkin_id: str,
    payload: MakeupDecisionIn,
    admin_username: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    return await _human_decision(db, checkin_id, payload.decision, payload.note or "", admin_username)


@router.post("/makeup/ai-review-pending")
async def makeup_ai_review_pending(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    if not load_ai_config()["api_key"]:
        raise HTTPException(status_code=400, detail="尚未配置 AI 审核密钥")
    rows = await db.execute(
        select(DailyCheckin.id).where(DailyCheckin.makeup_bonus.is_(True), DailyCheckin.makeup_status == "pending")
    )
    ids = [r[0] for r in rows.all()]
    for cid in ids:
        background_tasks.add_task(run_ai_review, cid)
    return {"queued": len(ids)}


@router.post("/makeup/{checkin_id}/ai-review")
async def makeup_ai_review_one(
    checkin_id: str,
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    if not load_ai_config()["api_key"]:
        raise HTTPException(status_code=400, detail="尚未配置 AI 审核密钥")
    checkin = (await db.execute(select(DailyCheckin).where(DailyCheckin.id == checkin_id))).scalar_one_or_none()
    if not checkin or not checkin.makeup_bonus:
        raise HTTPException(status_code=404, detail="补早起记录不存在")
    background_tasks.add_task(run_ai_review, checkin_id)
    return {"queued": 1}


@router.delete("/submissions/{submission_id}")
async def delete_submission(
    submission_id: str,
    _: str = Depends(require_admin_csrf),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.id == submission_id)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    submission = result.scalar_one_or_none()

    if not submission:
        raise HTTPException(status_code=404, detail="记录不存在")

    for item in submission.items:
        for attachment in item.attachments:
            get_oss_client().delete_file(attachment.oss_key)

    for review in (await db.execute(select(MakeupReview).where(MakeupReview.checkin_id == submission.id))).scalars():
        await db.delete(review)
    await db.delete(submission)
    await db.commit()

    return {"success": True}
