"""用多模态模型审核补早起证明截图（OpenAI Responses 协议）。"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import urllib.request
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import CheckinItem, DailyCheckin, MakeupReview
from app.services.makeup import (
    CHINA_TZ,
    STATUS_NEEDS_HUMAN,
    apply_decision,
)
from app.services.oss import get_oss_client

logger = logging.getLogger(__name__)

ENV_FILE = os.environ.get("AI_REVIEW_ENV_FILE", "/home/devbox/.ai-review.env")
MAX_IMAGES = 12
_semaphore = asyncio.Semaphore(2)
_running: set[str] = set()


def load_config() -> dict:
    cfg = {
        "api_key": "",
        "base_url": "https://grok-api.chatghx.com/v1",
        "model": "grok-4.7",
        "accept_threshold": 0.80,
        "reject_threshold": 0.90,
    }
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k == "AI_REVIEW_API_KEY":
                    cfg["api_key"] = v
                elif k == "AI_REVIEW_BASE_URL" and v:
                    cfg["base_url"] = v.rstrip("/")
                elif k == "AI_REVIEW_MODEL" and v:
                    cfg["model"] = v
                elif k == "AI_REVIEW_THRESHOLD" and v:  # 兼容旧配置：同时设置通过/拒绝阈值
                    cfg["accept_threshold"] = cfg["reject_threshold"] = float(v)
                elif k == "AI_REVIEW_ACCEPT_THRESHOLD" and v:
                    cfg["accept_threshold"] = float(v)
                elif k == "AI_REVIEW_REJECT_THRESHOLD" and v:
                    cfg["reject_threshold"] = float(v)
    except FileNotFoundError:
        pass
    return cfg


def _image_to_data_url(raw: bytes, content_type: str, edge: int = 1400, quality: int = 78) -> list[str]:
    from PIL import Image

    pages: list[bytes] = []
    if content_type == "application/pdf" or raw[:5] == b"%PDF-":
        import fitz  # pymupdf

        doc = fitz.open(stream=raw, filetype="pdf")
        for i, page in enumerate(doc):
            if i >= 2:
                break
            pages.append(page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).tobytes("png"))
    else:
        pages.append(raw)

    urls = []
    for data in pages:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((edge, edge))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=quality)
        urls.append("data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode())
    return urls


def _fetch_object(key: str) -> bytes:
    client = get_oss_client()
    resp = client.client.get_object(Bucket=client.bucket_name, Key=key)
    return resp["Body"].read()


def _extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in data.get("output", []) or []:
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    if not parts and data.get("choices"):  # 兼容 chat completions 形态
        parts.append(data["choices"][0].get("message", {}).get("content", ""))
    return "\n".join(parts)


def _post_responses(cfg: dict, content: list[dict]) -> dict:
    body = json.dumps(
        {
            "model": cfg["model"],
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 1200,
        }
    ).encode()
    req = urllib.request.Request(
        f"{cfg['base_url']}/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.5.0",  # 网关会拦截默认的 Python-urllib UA（403）
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


PROMPT = """你是打卡活动的审核员。同学声称在北京时间 {date} 上午 06:00 至 08:00 之间完成了打卡，因系统故障当时未能提交，现在补交证明截图。
请逐张查看下面的截图，判断截图中能否看到**这个时段内的时间显示证明**。
时间的主要依据是手机屏幕**最上方状态栏左上角的系统时间**（例如 07:21）。同学是 {date} 当天提交的，所以只要状态栏时间落在 06:00-08:00 内，就视为有效的时间证明，**不要求画面里再出现日期**。
其他可用的证据：相机水印时间、学习/运动 App 里的完成时间、开始/结束时间、记录时间。
注意：
- 状态栏时间在 08:00 之后（如 08:25）说明截图是 08:00 之后才截的，不能证明 06:00-08:00 内完成。
- 只有当画面里明确出现的日期不是 {date}（例如显示 11-18）时，才因日期不符而拒绝。画面里没有日期不是拒绝理由。
- 截图内容要与所补的项目相符（比如补背单词就应是背单词界面或其完成记录）。

判断规则：
- accept：至少一张截图的状态栏（或水印、App 内）时间清楚落在 06:00-08:00 内，没有出现与 {date} 矛盾的日期，且内容与所补项目相符。
- reject：时间明确在范围之外（例如状态栏 09:xx、08:25），或日期明确是别的天，或所有截图都完全没有任何时间显示，且你有把握。
- uncertain：图片模糊、时间看不清、只有部分证据、截图与项目不符、你不确定。
宁可 uncertain 也不要乱判；confidence 是你对 decision 这个结论的把握，0 到 1。

补早起项目：{items}
截图列表（按顺序对应后面的图片）：
{files}

只输出一个 JSON，不要任何其他文字：
{{"decision":"accept|reject|uncertain","confidence":0.0,"observed_times":["在截图中看到的所有时间/日期，注明来自第几张图"],"reason":"一两句中文说明依据"}}"""


async def _review_once(checkin_id: str) -> None:
    cfg = load_config()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DailyCheckin)
            .where(DailyCheckin.id == checkin_id)
            .options(selectinload(DailyCheckin.items).selectinload(CheckinItem.attachments))
        )
        checkin = result.scalar_one_or_none()
        if not checkin or not checkin.makeup_bonus:
            return

        review = MakeupReview(
            checkin_id=checkin.id,
            reviewer_type="ai",
            reviewer_name=cfg["model"],
            model=cfg["model"],
            decision="error",
            created_at=datetime.now(CHINA_TZ),
        )
        try:
            if not cfg["api_key"]:
                raise RuntimeError("未配置 AI 审核密钥")

            atts = [
                (a, it.item_type)
                for it in checkin.items
                for a in it.attachments
                if "补早起" in (a.file_name or "")
            ]
            if not atts:
                raise RuntimeError("没有找到补早起的附件")

            content: list[dict] = []
            file_lines: list[str] = []
            raws = []
            for a, item_type in atts[:MAX_IMAGES]:
                raw = await asyncio.to_thread(_fetch_object, a.oss_key)
                raws.append((a, item_type, raw))
            # 总负载预算：网关对请求体大小有限制，超了就逐级降分辨率重试
            for edge, quality in ((1400, 78), (1100, 70), (850, 62), (700, 55)):
                content, file_lines, n = [], [], 0
                for a, item_type, raw in raws:
                    for url in await asyncio.to_thread(_image_to_data_url, raw, a.file_type, edge, quality):
                        if n >= MAX_IMAGES:
                            break
                        n += 1
                        file_lines.append(f"第{n}张：{a.file_name}（{item_type}）")
                        content.append({"type": "input_image", "image_url": url})
                if sum(len(c["image_url"]) for c in content) <= 950_000:  # 网关实测上限在 0.83~1.4MB 之间
                    break
            review.attachments_checked = n

            items = "、".join(sorted({t for _, t in atts}))
            prompt = PROMPT.format(date="2026-11-19", items=items, files="\n".join(file_lines))
            content.insert(0, {"type": "input_text", "text": prompt})

            data = await asyncio.to_thread(_post_responses, cfg, content)
            text = _extract_text(data)
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise RuntimeError(f"模型返回无法解析：{text[:200]}")
            parsed = json.loads(m.group(0))
            decision = str(parsed.get("decision", "")).lower()
            if decision not in {"accept", "reject", "uncertain"}:
                raise RuntimeError(f"模型返回了未知结论：{decision}")
            review.decision = decision
            review.confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0))))
            review.observed_times = json.dumps(parsed.get("observed_times", []), ensure_ascii=False)
            review.reason = str(parsed.get("reason", ""))[:1000]
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai_review_failed checkin_id=%s", checkin_id)
            review.decision = "error"
            review.reason = f"AI 审核出错：{exc}"[:1000]

        # 高置信度才自动生效，其余转人工
        need = cfg["accept_threshold"] if review.decision == "accept" else cfg["reject_threshold"]
        if review.decision in {"accept", "reject"} and (review.confidence or 0) >= need:
            apply_decision(checkin, review.decision == "accept")
            review.auto_applied = True
        else:
            checkin.makeup_status = STATUS_NEEDS_HUMAN
            review.auto_applied = False
        db.add(review)
        await db.commit()
        logger.info(
            "ai_review_done checkin_id=%s decision=%s confidence=%s applied=%s",
            checkin_id, review.decision, review.confidence, review.auto_applied,
        )


async def run_ai_review(checkin_id: str) -> None:
    if checkin_id in _running:
        return
    _running.add(checkin_id)
    try:
        async with _semaphore:
            await _review_once(checkin_id)
    finally:
        _running.discard(checkin_id)
