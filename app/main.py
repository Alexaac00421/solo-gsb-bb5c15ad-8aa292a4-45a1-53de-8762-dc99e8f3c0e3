import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import Archive, DestructionItem, Hold
from .schemas import ArchiveCreate, DestructionBatchCreate, HoldCreate
from .services import (
    ApiError,
    active_holds,
    archive_destruction_history,
    create_archive,
    execute_destruction_batch,
    get_archive,
    get_destruction_batch,
    place_hold,
    release_hold,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 等待数据库就绪并初始化表结构
    last_err = None
    for _ in range(30):
        try:
            Base.metadata.create_all(engine)
            last_err = None
            break
        except OperationalError as e:
            last_err = e
            time.sleep(1)
    if last_err is not None:
        raise last_err
    yield


app = FastAPI(title="电子档案销毁裁决服务", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ApiError)
def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message, "details": exc.details}
        },
    )


def _archive_json(archive: Archive, holds: list[Hold] | None = None) -> dict:
    data = {
        "reg_no": archive.reg_no,
        "retain_until": archive.retain_until.isoformat(),
        "status": archive.status,
        "destroyed_at": archive.destroyed_at.isoformat() if archive.destroyed_at else None,
        "created_at": archive.created_at.isoformat() if archive.created_at else None,
    }
    if holds is not None:
        data["active_holds"] = [
            {"case_no": h.case_no, "created_at": h.created_at.isoformat()} for h in holds
        ]
    return data


def _hold_json(hold: Hold) -> dict:
    return {
        "archive_reg_no": hold.archive_reg_no,
        "case_no": hold.case_no,
        "status": hold.status,
        "created_at": hold.created_at.isoformat() if hold.created_at else None,
        "released_at": hold.released_at.isoformat() if hold.released_at else None,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- archives


@app.post("/archives", status_code=201)
def create_archive_api(body: ArchiveCreate, db: Session = Depends(get_db)):
    archive = create_archive(db, body.reg_no, body.retain_until)
    return _archive_json(archive)


@app.get("/archives/{reg_no}")
def get_archive_api(reg_no: str, db: Session = Depends(get_db)):
    archive = get_archive(db, reg_no)
    return _archive_json(archive, active_holds(db, reg_no))


@app.get("/archives/{reg_no}/destruction-items")
def archive_destruction_history_api(reg_no: str, db: Session = Depends(get_db)):
    items = archive_destruction_history(db, reg_no)
    return {
        "reg_no": reg_no,
        "items": [
            {
                "request_no": it.request_no,
                "destroyed": it.destroyed,
                "reasons": it.reasons,
                "decided_at": it.decided_at.isoformat(),
            }
            for it in items
        ],
    }


# ---------------------------------------------------------------- holds


@app.post("/archives/{reg_no}/holds")
def place_hold_api(reg_no: str, body: HoldCreate, response: Response, db: Session = Depends(get_db)):
    hold, created = place_hold(db, reg_no, body.case_no)
    response.status_code = 201 if created else 200
    return _hold_json(hold)


@app.delete("/archives/{reg_no}/holds/{case_no}")
def release_hold_api(reg_no: str, case_no: str, db: Session = Depends(get_db)):
    return _hold_json(release_hold(db, reg_no, case_no))


# ---------------------------------------------------------------- destruction


@app.post("/destruction-batches")
def execute_destruction_batch_api(
    body: DestructionBatchCreate, response: Response, db: Session = Depends(get_db)
):
    result, created = execute_destruction_batch(db, body.request_no, body.reg_nos)
    response.status_code = 201 if created else 200
    return result


@app.get("/destruction-batches/{request_no}")
def get_destruction_batch_api(request_no: str, db: Session = Depends(get_db)):
    return get_destruction_batch(db, request_no)
