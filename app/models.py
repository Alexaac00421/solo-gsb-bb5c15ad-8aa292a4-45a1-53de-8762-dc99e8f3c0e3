from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base


class Archive(Base):
    """档案：登记编号 + 保留截止时间。DESTROYED 后不可再变更。"""

    __tablename__ = "archives"

    reg_no = Column(String(64), primary_key=True)
    retain_until = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(16), nullable=False, server_default="ACTIVE")  # ACTIVE | DESTROYED
    destroyed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Hold(Base):
    """封存：案件对档案的封存记录，同一案件对同一档案至多一条生效记录。"""

    __tablename__ = "holds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    archive_reg_no = Column(
        String(64), ForeignKey("archives.reg_no"), nullable=False, index=True
    )
    case_no = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, server_default="ACTIVE")  # ACTIVE | RELEASED
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    released_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 仅对生效中的封存做唯一约束，解除后的历史记录保留可复查
        Index(
            "uq_active_hold",
            "archive_reg_no",
            "case_no",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class DestructionRequest(Base):
    """销毁批次：request_no 幂等键，result 为不可变凭据（整批结果一次提交后不再变化）。"""

    __tablename__ = "destruction_requests"

    request_no = Column(String(64), primary_key=True)
    payload_hash = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, server_default="COMPLETED")
    decided_at = Column(DateTime(timezone=True), nullable=False)
    result = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DestructionItem(Base):
    """销毁批次逐件明细，与批次同事务写入，可独立复查。"""

    __tablename__ = "destruction_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_no = Column(
        String(64), ForeignKey("destruction_requests.request_no"), nullable=False, index=True
    )
    reg_no = Column(String(64), nullable=False, index=True)
    destroyed = Column(Boolean, nullable=False)
    reasons = Column(JSONB, nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=False)
