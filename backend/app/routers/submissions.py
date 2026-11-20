import asyncio
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, APIRouter, UploadFile, File, Form, Request, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import CheckinAttachment, CheckinItem, DailyCheckin
from app.services.oss import get_oss_client
from app.services.ai_review import run_ai_review
from app.services.activity import (
    to_china_datetime,
    build_activity_response,
    checkin_window_label,
    get_checkin_date_at,
    get_or_create_activity_settings,
    is_within_checkin_window,
    MAX_TOTAL_POINTS,
    MORNING_BONUS_POINTS,
    should_earn_morning_bonus,
)
from app.services.rankings import build_ranking_rows, public_ranking_payload, ranking_sort_key
from app.schemas import (
    ActivitySettingsOut,
    PublicDailyCheckinOut,
    PublicRankingOut,
    PublicSubmissionResult,
    PublicUserStatsOut,
)

router = APIRouter(tags=["submissions"])
logger = logging.getLogger(__name__)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
MAKEUP_CHECKIN_DATE = date(2026, 5, 15)
MAKEUP_WINDOW_START = date(2026, 5, 16)
MAKEUP_WINDOW_END = date(2026, 5, 17)
MAX_BASE_POINTS = 6
MORNING_MAKEUP_DATE = date(2026, 11, 19)
MORNING_MAKEUP_CLAIM_AT = datetime(2026, 11, 19, 7, 59, tzinfo=CHINA_TZ)

ITEM_LABELS = {
    "listening": "听力",
    "reading": "阅读",
    "writing": "英语作文",
    "vocabulary": "背单词",
    "speaking": "口语",
    "running": "跑步",
}
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
}
UPLOAD_EXTENSION_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


@router.get("/api/activity", response_model=ActivitySettingsOut)
async def get_activity(db: AsyncSession = Depends(get_db)):
    settings = await get_or_create_activity_settings(db)
    await db.commit()
    return build_activity_response(settings)


@router.get("/api/submissions/today", response_model=PublicDailyCheckinOut | None)
async def get_today_submission(
    student_id: str = Query(...),
    checkin_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if not student_id.strip():
        raise HTTPException(status_code=400, detail="学号不能为空")

    activity = await get_or_create_activity_settings(db)
    now = datetime.now(CHINA_TZ)
    target_date = _resolve_checkin_date(activity, now, checkin_date)

    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.student_id == student_id.strip())
        .where(DailyCheckin.checkin_date == target_date)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    checkin = result.scalar_one_or_none()
    if not checkin:
        return None

    checkin.items.sort(key=lambda item: list(ITEM_LABELS).index(item.item_type))
    return _build_public_checkin(checkin)


@router.get("/api/rankings", response_model=list[PublicRankingOut])
async def get_public_rankings(db: AsyncSession = Depends(get_db)):
    activity = await get_or_create_activity_settings(db)
    end_date = activity.start_date + timedelta(days=activity.duration_days - 1)
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.checkin_date >= activity.start_date)
        .where(DailyCheckin.checkin_date <= end_date)
        .options(selectinload(DailyCheckin.items))
    )
    rows = build_ranking_rows(result.scalars().all())
    rows.sort(key=ranking_sort_key)
    return [PublicRankingOut(**public_ranking_payload(row)) for row in rows]


@router.get("/api/submissions/me", response_model=PublicUserStatsOut | None)
async def get_my_stats(
    student_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    if not student_id.strip():
        raise HTTPException(status_code=400, detail="学号不能为空")

    activity = await get_or_create_activity_settings(db)
    end_date = activity.start_date + timedelta(days=activity.duration_days - 1)
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.student_id == student_id.strip())
        .where(DailyCheckin.checkin_date >= activity.start_date)
        .where(DailyCheckin.checkin_date <= end_date)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    checkins = result.scalars().all()
    if not checkins:
        return None

    rows = build_ranking_rows(checkins)
    summary = public_ranking_payload(rows[0])
    latest = max(checkins, key=lambda checkin: checkin.checkin_date)
    latest.items.sort(key=lambda item: list(ITEM_LABELS).index(item.item_type))
    return PublicUserStatsOut(**summary, latest_checkin=_build_public_checkin(latest))


@router.get("/api/submissions/attachments/{attachment_id}/download")
async def download_my_attachment(
    attachment_id: str,
    student_id: str = Query(...),
    checkin_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if not student_id.strip():
        raise HTTPException(status_code=400, detail="学号不能为空")

    activity = await get_or_create_activity_settings(db)
    target_date = _resolve_checkin_date(activity, datetime.now(CHINA_TZ), checkin_date)

    result = await db.execute(
        select(CheckinAttachment)
        .join(CheckinItem, CheckinAttachment.item_id == CheckinItem.id)
        .join(DailyCheckin, CheckinItem.checkin_id == DailyCheckin.id)
        .where(CheckinAttachment.id == attachment_id)
        .where(DailyCheckin.student_id == student_id.strip())
        .where(DailyCheckin.checkin_date == target_date)
    )
    attachment = result.scalar_one_or_none()
    if not attachment:
        raise HTTPException(status_code=404, detail="附件不存在")

    return {"download_url": get_oss_client().get_presigned_url(attachment.oss_key, expire=3600)}


@router.post("/api/submissions", response_model=PublicSubmissionResult, status_code=status.HTTP_201_CREATED)
async def create_submission(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    student_id: str = Form(...),
    items_payload: str = Form(...),
    checkin_date: date | None = Form(None),
    morning_makeup: bool = Form(False),
    attachment_item_types: list[str] | None = Form(None),
    attachments: list[UploadFile] | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    if not name.strip() or not student_id.strip():
        raise HTTPException(status_code=400, detail="姓名和学号不能为空")

    item_inputs = _parse_items_payload(items_payload)
    logger.info(
        "checkin_submit_start student_id=%s item_types=%s attachment_count=%s",
        student_id.strip(),
        ",".join(item_inputs.keys()),
        len(attachments or []),
    )

    activity = await get_or_create_activity_settings(db)
    now = datetime.now(CHINA_TZ)
    is_makeup_submission = _is_makeup_date_allowed(now.date(), checkin_date)
    if not is_makeup_submission and not is_within_checkin_window(activity, now):
        raise HTTPException(
            status_code=400,
            detail=f"当前不在打卡有效时间内，本活动有效时间为 {checkin_window_label(activity)}",
        )
    target_date = _resolve_checkin_date(activity, now, checkin_date)
    if morning_makeup:
        if is_makeup_submission or target_date != MORNING_MAKEUP_DATE:
            raise HTTPException(status_code=400, detail="补早起入口仅限 2026-11-19 当天打卡使用，现已关闭")
    if is_makeup_submission:
        _validate_makeup_submission(item_inputs, attachment_item_types or [])

    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.student_id == student_id.strip())
        .where(DailyCheckin.checkin_date == target_date)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    checkin = result.scalar_one_or_none()

    if not checkin:
        checkin = DailyCheckin(
            name=name.strip(),
            student_id=student_id.strip(),
            checkin_date=target_date,
            first_submitted_at=now,
            ip_address=request.client.host if request.client else None,
        )
        db.add(checkin)
        await db.flush()
    else:
        checkin.name = name.strip()
        checkin.ip_address = request.client.host if request.client else checkin.ip_address
    was_rejected = False
    if morning_makeup:
        # 被拒绝后允许再次提交新的证明：重新进入“等待 AI”，全部证明会被重新审一遍；
        # 重新提交期间先不给早起分，等审核通过后才恢复
        was_rejected = checkin.makeup_status == "rejected"
        checkin.makeup_bonus = True
        checkin.makeup_status = "pending"

    existing_items_result = await db.execute(
        select(CheckinItem)
        .where(CheckinItem.checkin_id == checkin.id)
        .options(selectinload(CheckinItem.attachments))
    )
    existing_items = existing_items_result.scalars().all()
    item_by_type = {item.item_type: item for item in existing_items}
    existing_attachment_counts = {
        item.item_type: len(item.attachments)
        for item in existing_items
    }
    existing_item_types = set(item_by_type)
    _validate_attachments(
        item_inputs,
        attachment_item_types or [],
        attachments or [],
        required_item_types=set(item_inputs) if morning_makeup else set(item_inputs) - existing_item_types,
    )
    for item_type, raw_values in item_inputs.items():
        item = item_by_type.get(item_type)
        if not item:
            item = CheckinItem(checkin_id=checkin.id, item_type=item_type)
            db.add(item)
            await db.flush()
            item_by_type[item_type] = item
            existing_attachment_counts[item_type] = 0

        values = _evaluate_item(item_type, raw_values)
        for field_name, value in values.items():
            setattr(item, field_name, value)

    await db.flush()
    await _store_attachments(
        db,
        checkin,
        item_by_type,
        existing_attachment_counts,
        attachment_item_types or [],
        attachments or [],
    )
    await db.flush()

    all_items = list(item_by_type.values())
    base_points = sum(item.points for item in all_items)
    total_volume = round(sum(item.volume_score for item in all_items), 4)
    has_valid_item = base_points > 0
    if has_valid_item and checkin.first_valid_at is None:
        checkin.first_valid_at = now
        checkin.earned_morning_bonus = should_earn_morning_bonus(
            activity,
            target_date,
            checkin.first_valid_at,
            min(base_points, MAX_BASE_POINTS),
        )

    if morning_makeup and base_points > 0 and not was_rejected:
        first_valid = checkin.first_valid_at
        if first_valid is None or to_china_datetime(first_valid) > MORNING_MAKEUP_CLAIM_AT:
            checkin.first_valid_at = MORNING_MAKEUP_CLAIM_AT
        checkin.earned_morning_bonus = True
        logger.info("morning_makeup_claimed student_id=%s checkin_id=%s", student_id.strip(), checkin.id)

    checkin.base_points = min(base_points, MAX_BASE_POINTS)
    checkin.total_volume = total_volume
    bonus_points = MORNING_BONUS_POINTS if checkin.earned_morning_bonus and checkin.base_points > 0 else 0
    checkin.total_points = min(checkin.base_points + bonus_points, MAX_TOTAL_POINTS)
    checkin.updated_at = now

    await db.commit()
    if morning_makeup and base_points > 0:
        background_tasks.add_task(run_ai_review, checkin.id)
    saved = await _load_checkin(db, checkin.id)
    logger.info(
        "checkin_submit_success checkin_id=%s student_id=%s base_points=%s total_points=%s",
        saved.id,
        saved.student_id,
        saved.base_points,
        saved.total_points,
    )
    return {
        "checkin": _build_public_checkin(saved),
        "message": "打卡已保存",
    }


class ClientDiag(BaseModel):
    info: dict


@router.post("/api/diag")
async def client_diag(payload: ClientDiag, request: Request):
    """前端提交失败时上报文件大小/类型等信息（不含文件内容），用于排查个别手机浏览器的问题。"""
    text = json.dumps(payload.info, ensure_ascii=False)[:2000]
    logger.warning(
        "client_diag ua=%s info=%s", request.headers.get("user-agent", "-")[:120], text
    )
    return {"ok": True}


class JsonAttachment(BaseModel):
    filename: str
    data: str  # base64


class JsonSubmission(BaseModel):
    name: str
    student_id: str
    items_payload: str
    checkin_date: date | None = None
    morning_makeup: bool = False
    attachment_item_types: list[str] = []
    attachments: list[JsonAttachment] = []


@router.post("/api/submissions/json", response_model=PublicSubmissionResult, status_code=status.HTTP_201_CREATED)
async def create_submission_json(
    payload: JsonSubmission,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """兜底入口：部分手机内置浏览器（如 iPhone 的 QQ）会把 multipart 请求体丢成空，
    前端检测到后改用 JSON+base64 重试，业务逻辑与 /api/submissions 完全一致。"""
    import base64
    import io

    files: list[UploadFile] = []
    for att in payload.attachments:
        try:
            raw = base64.b64decode(att.data, validate=False)
        except Exception:
            raise HTTPException(status_code=400, detail="附件数据格式错误")
        files.append(UploadFile(file=io.BytesIO(raw), filename=att.filename))
    return await create_submission(
        request=request,
        background_tasks=background_tasks,
        name=payload.name,
        student_id=payload.student_id,
        items_payload=payload.items_payload,
        checkin_date=payload.checkin_date,
        morning_makeup=payload.morning_makeup,
        attachment_item_types=payload.attachment_item_types or None,
        attachments=files or None,
        db=db,
    )


def _build_public_checkin(checkin: DailyCheckin) -> dict[str, Any]:
    return {
        "checkin_date": checkin.checkin_date,
        "first_submitted_at": checkin.first_submitted_at,
        "first_valid_at": checkin.first_valid_at,
        "earned_morning_bonus": checkin.earned_morning_bonus,
        "base_points": checkin.base_points,
        "total_points": checkin.total_points,
        "total_volume": checkin.total_volume,
        "updated_at": checkin.updated_at,
        "items": [
            {
                "item_type": item.item_type,
                "is_valid": item.is_valid,
                "points": item.points,
                "volume_score": item.volume_score,
                "listening_questions": item.listening_questions,
                "reading_articles": item.reading_articles,
                "reading_questions": item.reading_questions,
                "writing_words": item.writing_words,
                "vocabulary_words": item.vocabulary_words,
                "speaking_minutes": item.speaking_minutes,
                "speaking_dialogue_sentences": item.speaking_dialogue_sentences,
                "running_distance_km": item.running_distance_km,
                "running_pace_min_per_km": item.running_pace_min_per_km,
                "attachments": [
                    {
                        "id": attachment.id,
                        "item_type": attachment.item_type,
                        "display_name": f"{ITEM_LABELS.get(attachment.item_type, '附件')}截图",
                        "file_size": attachment.file_size,
                        "file_type": attachment.file_type,
                        "uploaded_at": attachment.uploaded_at,
                    }
                    for attachment in item.attachments
                ],
            }
            for item in checkin.items
        ],
    }


def _parse_items_payload(items_payload: str) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(items_payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="打卡项目数据格式错误")

    if not isinstance(parsed, dict) or not parsed:
        raise HTTPException(status_code=400, detail="请至少选择一个打卡项目")

    item_inputs: dict[str, dict[str, Any]] = {}
    for item_type, raw_values in parsed.items():
        if item_type not in ITEM_LABELS:
            raise HTTPException(status_code=400, detail="包含未知打卡项目")
        if not isinstance(raw_values, dict):
            raise HTTPException(status_code=400, detail=f"{ITEM_LABELS[item_type]}数据格式错误")
        item_inputs[item_type] = raw_values

    return item_inputs


def _resolve_checkin_date(activity: Any, now: datetime, requested_date: date | None) -> date:
    current_checkin_date = get_checkin_date_at(activity, now)
    if requested_date is None or requested_date == current_checkin_date:
        return current_checkin_date

    today = now.date()
    if _is_makeup_date_allowed(today, requested_date):
        return requested_date

    raise HTTPException(status_code=400, detail="当前不支持补打卡该日期")


def _is_makeup_date_allowed(today: date, requested_date: date | None) -> bool:
    return (
        requested_date == MAKEUP_CHECKIN_DATE
        and MAKEUP_WINDOW_START <= today <= MAKEUP_WINDOW_END
    )


def _validate_makeup_submission(
    item_inputs: dict[str, dict[str, Any]],
    attachment_item_types: list[str],
) -> None:
    if set(item_inputs) != {"speaking"}:
        raise HTTPException(status_code=400, detail="补打卡仅允许提交 2026-05-15 的口语项目")
    if any(item_type != "speaking" for item_type in attachment_item_types):
        raise HTTPException(status_code=400, detail="补打卡附件仅允许对应口语项目")


def _validate_attachments(
    item_inputs: dict[str, dict[str, Any]],
    attachment_item_types: list[str],
    attachments: list[UploadFile],
    required_item_types: set[str] | None = None,
) -> None:
    if len(attachment_item_types) != len(attachments):
        raise HTTPException(status_code=400, detail="附件和项目类型数量不匹配")
    if len(attachments) > settings.upload_max_files_per_submission:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {settings.upload_max_files_per_submission} 个附件",
        )

    attached_types = set()
    for item_type, attachment in zip(attachment_item_types, attachments):
        if item_type not in ITEM_LABELS:
            raise HTTPException(status_code=400, detail="附件包含未知项目类型")
        if not attachment.filename:
            raise HTTPException(status_code=400, detail="附件文件名不能为空")
        attached_types.add(item_type)

    required_types = required_item_types if required_item_types is not None else set(item_inputs)
    missing = [ITEM_LABELS[item_type] for item_type in required_types if item_type not in attached_types]
    if missing:
        raise HTTPException(status_code=400, detail=f"请为{', '.join(missing)}上传对应截图")


def _int_value(values: dict[str, Any], key: str) -> int:
    raw_value = values.get(key, 0)
    if raw_value in (None, ""):
        return 0
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="项目完成量必须是数字")
    if value < 0:
        raise HTTPException(status_code=400, detail="项目完成量不能为负数")
    return value


def _float_value(values: dict[str, Any], key: str) -> float:
    raw_value = values.get(key, 0)
    if raw_value in (None, ""):
        return 0
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="项目完成量必须是数字")
    if value < 0:
        raise HTTPException(status_code=400, detail="项目完成量不能为负数")
    return value


def _evaluate_item(item_type: str, values: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "listening_questions": None,
        "reading_articles": None,
        "reading_questions": None,
        "writing_words": None,
        "vocabulary_words": None,
        "speaking_minutes": None,
        "speaking_dialogue_sentences": None,
        "running_distance_km": None,
        "running_pace_min_per_km": None,
    }

    if item_type == "listening":
        questions = _int_value(values, "listening_questions")
        is_valid = questions >= 10
        data.update(
            listening_questions=questions,
            volume_score=questions / 10 if questions else 0,
        )
    elif item_type == "reading":
        articles = _int_value(values, "reading_articles")
        questions = _int_value(values, "reading_questions")
        is_valid = articles >= 2 and questions >= 10
        data.update(
            reading_articles=articles,
            reading_questions=questions,
            volume_score=min(articles / 2 if articles else 0, questions / 10 if questions else 0),
        )
    elif item_type == "writing":
        words = _int_value(values, "writing_words")
        is_valid = words >= 100
        data.update(writing_words=words, volume_score=words / 100 if words else 0)
    elif item_type == "vocabulary":
        words = _int_value(values, "vocabulary_words")
        is_valid = words >= 20
        data.update(vocabulary_words=words, volume_score=words / 20 if words else 0)
    elif item_type == "speaking":
        minutes = _float_value(values, "speaking_minutes")
        sentences = _int_value(values, "speaking_dialogue_sentences")
        is_valid = minutes >= 10 or sentences >= 15
        data.update(
            speaking_minutes=minutes,
            speaking_dialogue_sentences=sentences,
            volume_score=max(minutes / 10 if minutes else 0, sentences / 15 if sentences else 0),
        )
    else:
        distance = _float_value(values, "running_distance_km")
        pace = _float_value(values, "running_pace_min_per_km")
        is_valid = distance >= 1.2 and 0 < pace <= 10
        data.update(
            running_distance_km=distance,
            running_pace_min_per_km=pace,
            volume_score=distance / 1.2 if is_valid else 0,
        )

    data["is_valid"] = is_valid
    data["points"] = 1 if is_valid else 0
    return data


async def _store_attachments(
    db: AsyncSession,
    checkin: DailyCheckin,
    item_by_type: dict[str, CheckinItem],
    existing_attachment_counts: dict[str, int],
    attachment_item_types: list[str],
    attachments: list[UploadFile],
) -> None:
    for item_type, attachment in zip(attachment_item_types, attachments):
        item = item_by_type[item_type]
        next_index = existing_attachment_counts.get(item_type, 0) + 1
        existing_attachment_counts[item_type] = next_index
        file_bytes, detected_content_type = await _read_validated_attachment(attachment)
        formatted_filename = _format_attachment_filename(
            checkin,
            item_type,
            next_index,
            attachment.filename or "attachment",
            detected_content_type,
        )
        object_key = _format_attachment_object_key(checkin, item_type, next_index, formatted_filename)
        logger.info(
            "attachment_upload_start item_type=%s filename=%s formatted_filename=%s content_type=%s",
            item_type,
            attachment.filename,
            formatted_filename,
            detected_content_type,
        )

        try:
            upload_result = get_oss_client().upload_file(
                file_bytes,
                formatted_filename,
                object_key=object_key,
            )
        except Exception:
            logger.exception(
                "attachment_upload_failed item_type=%s filename=%s size=%s",
                item_type,
                attachment.filename,
                len(file_bytes),
            )
            raise
        logger.info(
            "attachment_upload_success item_type=%s filename=%s size=%s",
            item_type,
            attachment.filename,
            len(file_bytes),
        )
        db.add(
            CheckinAttachment(
                item_id=item.id,
                item_type=item_type,
                file_name=formatted_filename,
                file_url=upload_result["file_url"],
                file_size=len(file_bytes),
                file_type=detected_content_type,
                oss_key=upload_result["oss_key"],
            )
        )


async def _read_validated_attachment(attachment: UploadFile) -> tuple[bytes, str]:
    max_size = settings.upload_max_file_size_mb * 1024 * 1024
    file_bytes = await attachment.read(max_size + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail=f"{attachment.filename} 是空文件")
    if len(file_bytes) > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"{attachment.filename} 超过 {settings.upload_max_file_size_mb} MB 限制",
        )

    if _is_heic(file_bytes):
        try:
            file_bytes = await asyncio.to_thread(_heic_to_pdf, file_bytes)
        except Exception:
            logger.exception("heic_convert_failed filename=%s", attachment.filename)
            raise HTTPException(status_code=400, detail=f"{attachment.filename} HEIC 转换失败，请换一张图片")
        logger.info("heic_converted_to_pdf filename=%s pdf_size=%s", attachment.filename, len(file_bytes))

    detected_content_type = _detect_content_type(file_bytes)
    if detected_content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail=f"{attachment.filename} 文件类型不支持")
    return file_bytes, detected_content_type


def _is_heic(file_bytes: bytes) -> bool:
    return (
        len(file_bytes) >= 12
        and file_bytes[4:8] == b"ftyp"
        and file_bytes[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"heif")
    )


def _heic_to_pdf(file_bytes: bytes) -> bytes:
    import io

    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener()
    with Image.open(io.BytesIO(file_bytes)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PDF", resolution=150.0, quality=85)
        return out.getvalue()


def _detect_content_type(file_bytes: bytes) -> str:
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "image/webp"
    if file_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


def _format_attachment_filename(
    checkin: DailyCheckin,
    item_type: str,
    sequence: int,
    original_filename: str,
    content_type: str | None,
) -> str:
    ext = _file_extension(original_filename, content_type)
    name = _sanitize_filename_part(checkin.name)
    student_id = _sanitize_filename_part(checkin.student_id)
    item_label = ITEM_LABELS[item_type] + ("补早起" if getattr(checkin, "makeup_bonus", False) else "")
    date_text = checkin.checkin_date.strftime("%Y%m%d")
    return f"{date_text}_{student_id}_{name}_{item_label}_{sequence:02d}{ext}"


def _format_attachment_object_key(
    checkin: DailyCheckin,
    item_type: str,
    sequence: int,
    formatted_filename: str,
) -> str:
    ext = _file_extension(formatted_filename, None)
    date_text = checkin.checkin_date.strftime("%Y-%m-%d")
    student_id = _sanitize_path_part(checkin.student_id)
    return f"submissions/{date_text}/{student_id}/{item_type}/{sequence:02d}_{uuid.uuid4().hex}{ext}"


def _file_extension(filename: str, content_type: str | None) -> str:
    if content_type in UPLOAD_EXTENSION_BY_TYPE:
        return UPLOAD_EXTENSION_BY_TYPE[content_type]

    if "." in filename:
        raw_ext = filename.rsplit(".", 1)[-1].lower()
        safe_ext = re.sub(r"[^a-z0-9]", "", raw_ext)
        if safe_ext:
            return f".{safe_ext[:12]}"

    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type == "application/pdf":
        return ".pdf"
    return ".bin"


def _sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", value.strip())
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned[:40] or "unknown"


def _sanitize_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    return cleaned.strip("-")[:40] or "unknown"


async def _load_checkin(db: AsyncSession, checkin_id: str) -> DailyCheckin:
    result = await db.execute(
        select(DailyCheckin)
        .where(DailyCheckin.id == checkin_id)
        .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
    )
    checkin = result.scalar_one()
    checkin.items.sort(key=lambda item: list(ITEM_LABELS).index(item.item_type))
    return checkin
