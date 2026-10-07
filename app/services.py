import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Archive, DestructionItem, DestructionRequest, Hold


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payload_hash(reg_nos: list[str]) -> str:
    """批次内容指纹：档案编号视为集合，排序去重后哈希，与请求顺序无关。"""
    canon = json.dumps(sorted(set(reg_nos)), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- archives


def create_archive(db: Session, reg_no: str, retain_until: datetime) -> Archive:
    if db.get(Archive, reg_no) is not None:
        raise ApiError(409, "ARCHIVE_EXISTS", f"档案 {reg_no} 已存在")
    archive = Archive(reg_no=reg_no, retain_until=retain_until)
    db.add(archive)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ARCHIVE_EXISTS", f"档案 {reg_no} 已存在")
    db.refresh(archive)
    return archive


def get_archive(db: Session, reg_no: str) -> Archive:
    archive = db.get(Archive, reg_no)
    if archive is None:
        raise ApiError(404, "ARCHIVE_NOT_FOUND", f"档案 {reg_no} 不存在")
    return archive


def active_holds(db: Session, reg_no: str) -> list[Hold]:
    return db.execute(
        select(Hold)
        .where(Hold.archive_reg_no == reg_no, Hold.status == "ACTIVE")
        .order_by(Hold.case_no)
    ).scalars().all()


# ---------------------------------------------------------------- holds


def place_hold(db: Session, reg_no: str, case_no: str) -> tuple[Hold, bool]:
    """封存。与销毁共用档案行锁串行化：销毁先提交则此处看到 DESTROYED 报冲突。"""
    archive = db.execute(
        select(Archive).where(Archive.reg_no == reg_no).with_for_update()
    ).scalar_one_or_none()
    if archive is None:
        raise ApiError(404, "ARCHIVE_NOT_FOUND", f"档案 {reg_no} 不存在")
    if archive.status == "DESTROYED":
        raise ApiError(409, "ARCHIVE_DESTROYED", f"档案 {reg_no} 已销毁，禁止变更")

    hold = db.execute(
        select(Hold).where(
            Hold.archive_reg_no == reg_no,
            Hold.case_no == case_no,
            Hold.status == "ACTIVE",
        )
    ).scalar_one_or_none()
    if hold is not None:
        return hold, False  # 同案件重复封存：幂等返回原记录

    hold = Hold(archive_reg_no=reg_no, case_no=case_no, status="ACTIVE")
    db.add(hold)
    db.commit()
    db.refresh(hold)
    return hold, True


def release_hold(db: Session, reg_no: str, case_no: str) -> Hold:
    """解除封存：仅解除指定案件，不影响同一档案上的其他案件。"""
    archive = db.execute(
        select(Archive).where(Archive.reg_no == reg_no).with_for_update()
    ).scalar_one_or_none()
    if archive is None:
        raise ApiError(404, "ARCHIVE_NOT_FOUND", f"档案 {reg_no} 不存在")
    if archive.status == "DESTROYED":
        raise ApiError(409, "ARCHIVE_DESTROYED", f"档案 {reg_no} 已销毁，禁止变更")

    hold = db.execute(
        select(Hold).where(
            Hold.archive_reg_no == reg_no,
            Hold.case_no == case_no,
            Hold.status == "ACTIVE",
        )
    ).scalar_one_or_none()
    if hold is None:
        raise ApiError(404, "HOLD_NOT_FOUND", f"档案 {reg_no} 上案件 {case_no} 无生效封存")

    hold.status = "RELEASED"
    hold.released_at = _now()
    db.commit()
    db.refresh(hold)
    return hold


# ---------------------------------------------------------------- destruction


def execute_destruction_batch(
    db: Session, request_no: str, reg_nos: list[str]
) -> tuple[dict, bool]:
    """批量销毁裁决。

    - 整批一个事务：锁定 -> 逐件裁决 -> 写凭据 -> 改状态，一次提交；
    - 任一档案不存在则整批报错回滚，不留部分销毁；
    - request_no 幂等：同内容重试返回原结果，不同内容报冲突。
    """
    unique_nos = sorted(set(reg_nos))
    payload_hash = _payload_hash(reg_nos)

    existing = db.get(DestructionRequest, request_no)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ApiError(
                409,
                "REQUEST_CONFLICT",
                f"请求号 {request_no} 已存在且内容不一致",
                {"request_no": request_no},
            )
        return existing.result, False

    # 按编号排序加行锁，避免批次间死锁；并发的封存/解除因竞争同一行锁而按提交顺序串行
    archives = db.execute(
        select(Archive)
        .where(Archive.reg_no.in_(unique_nos))
        .order_by(Archive.reg_no)
        .with_for_update()
    ).scalars().all()
    by_reg = {a.reg_no: a for a in archives}
    missing = [n for n in unique_nos if n not in by_reg]
    if missing:
        raise ApiError(
            404,
            "ARCHIVE_NOT_FOUND",
            "批次中存在不存在的档案，整批拒绝，未产生任何销毁",
            {"missing": missing},
        )

    decided_at = _now()  # 服务端裁决时刻，整批统一
    items = []
    for reg_no in unique_nos:
        archive = by_reg[reg_no]
        reasons = []
        if archive.status == "DESTROYED":
            reasons.append(
                {
                    "code": "ALREADY_DESTROYED",
                    "destroyed_at": archive.destroyed_at.isoformat()
                    if archive.destroyed_at
                    else None,
                }
            )
        else:
            if archive.retain_until > decided_at:
                reasons.append(
                    {"code": "NOT_EXPIRED", "retain_until": archive.retain_until.isoformat()}
                )
            case_nos = db.execute(
                select(Hold.case_no)
                .where(Hold.archive_reg_no == reg_no, Hold.status == "ACTIVE")
                .order_by(Hold.case_no)
            ).scalars().all()
            if case_nos:
                reasons.append({"code": "ACTIVE_HOLD", "case_nos": list(case_nos)})

        destroyed = not reasons
        if destroyed:
            archive.status = "DESTROYED"
            archive.destroyed_at = decided_at
        items.append({"reg_no": reg_no, "destroyed": destroyed, "reasons": reasons})

    result = {
        "request_no": request_no,
        "status": "COMPLETED",
        "decided_at": decided_at.isoformat(),
        "items": items,
    }
    db.add(
        DestructionRequest(
            request_no=request_no,
            payload_hash=payload_hash,
            status="COMPLETED",
            decided_at=decided_at,
            result=result,
        )
    )
    try:
        db.flush()  # 先落主表，保证明细外键可解析
        for item in items:
            db.add(
                DestructionItem(
                    request_no=request_no,
                    reg_no=item["reg_no"],
                    destroyed=item["destroyed"],
                    reasons=item["reasons"],
                    decided_at=decided_at,
                )
            )
        db.commit()
    except IntegrityError:
        # 并发下同一 request_no 只有一个事务能提交成功
        db.rollback()
        winner = db.get(DestructionRequest, request_no)
        if winner is not None and winner.payload_hash == payload_hash:
            return winner.result, False
        raise ApiError(
            409,
            "REQUEST_CONFLICT",
            f"请求号 {request_no} 已存在且内容不一致",
            {"request_no": request_no},
        )
    return result, True


def get_destruction_batch(db: Session, request_no: str) -> dict:
    req = db.get(DestructionRequest, request_no)
    if req is None:
        raise ApiError(404, "REQUEST_NOT_FOUND", f"销毁批次 {request_no} 不存在")
    return req.result


def archive_destruction_history(db: Session, reg_no: str) -> list[DestructionItem]:
    get_archive(db, reg_no)  # 档案不存在则 404
    return db.execute(
        select(DestructionItem)
        .where(DestructionItem.reg_no == reg_no)
        .order_by(DestructionItem.id)
    ).scalars().all()
