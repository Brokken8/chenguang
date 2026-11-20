"""补早起：审核结果生效逻辑（AI 与人工共用）。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import DailyCheckin
from app.services.activity import MAX_TOTAL_POINTS, MORNING_BONUS_POINTS

CHINA_TZ = ZoneInfo("Asia/Shanghai")
MORNING_MAKEUP_CLAIM_AT = datetime(2026, 11, 19, 7, 59, tzinfo=CHINA_TZ)

STATUS_PENDING = "pending"          # 等待 AI 审核
STATUS_NEEDS_HUMAN = "needs_human"  # AI 拿不准，等人工
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def apply_decision(checkin: DailyCheckin, accept: bool) -> None:
    """通过：保留/恢复早起分；拒绝：取消早起分并禁止再次申请。"""
    if accept:
        checkin.makeup_status = STATUS_APPROVED
        checkin.makeup_revoked = False
        if checkin.base_points > 0:
            checkin.earned_morning_bonus = True
            checkin.first_valid_at = MORNING_MAKEUP_CLAIM_AT
            checkin.total_points = min(checkin.base_points + MORNING_BONUS_POINTS, MAX_TOTAL_POINTS)
    else:
        checkin.makeup_status = STATUS_REJECTED
        checkin.makeup_revoked = True
        checkin.earned_morning_bonus = False
        # 不能留在 07:59，否则“重算早起”会把分加回去
        checkin.first_valid_at = datetime.now(CHINA_TZ)
        checkin.total_points = min(checkin.base_points, MAX_TOTAL_POINTS)
