from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, Field, field_validator


def _aware_utc(v: datetime) -> datetime:
    """无时区的输入按 UTC 处理，统一服务端裁决时刻的时区基准。"""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class ArchiveCreate(BaseModel):
    reg_no: str = Field(min_length=1, max_length=64)
    retain_until: datetime

    _normalize = field_validator("retain_until")(_aware_utc)


class HoldCreate(BaseModel):
    case_no: str = Field(min_length=1, max_length=64)


class DestructionBatchCreate(BaseModel):
    request_no: str = Field(min_length=1, max_length=64)
    reg_nos: List[str] = Field(min_length=1, max_length=1000)

    @field_validator("reg_nos")
    @classmethod
    def _non_empty(cls, v):
        for n in v:
            if not n or len(n) > 64:
                raise ValueError("reg_no 不能为空且长度不超过 64")
        return v
