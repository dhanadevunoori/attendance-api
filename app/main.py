"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
EMP = db.employees
LOGS = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUS = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PRESENCE_STATUS = Literal["PRESENT", "WFH", "ON_DUTY"]


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    EMP.create_index([("emp_code", ASCENDING)], unique=True)            # identity + race-safe create + sort
    EMP.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])  # filtered list, trend root
    EMP.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])  # dept summary headcount
    EMP.create_index([("joined_on", ASCENDING)])                        # summary without department
    LOGS.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)  # one record per day
    LOGS.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])  # list sort, leaderboard range
    LOGS.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    LOGS.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])  # punch-out lookup


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up; /health will report 503
        log.error("index creation failed: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_req: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
def _valid_date(v: str) -> str:
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return v


DateStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_valid_date)]
MonthStr = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict=True rejects floats, strings and bools; the range rejects values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def bad_param(loc: str, msg: str):
    return RequestValidationError([{"loc": ("query", loc), "msg": msg, "type": "value_error"}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PRESENCE_STATUS = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[ALL_STATUS] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=ms - ms % 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_ms_precision() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)  # BSON dates are millisecond precision


def hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of punch-in; overnight shifts belong to the previous day before shift_end."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = hm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = hm(shift_start)
    return datetime.combine(date.fromisoformat(date_str), time(h, m), tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = hm(shift_end)
    d = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime.combine(d, time(h, m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    diff = (punch_in - shift_start_dt(date_str, shift_start)).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    diff = (punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds()
    mins = int(diff // 60)
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours to 2 decimals, half-up (Decimal, not float round())."""
    micros = (punch_out - punch_in) // timedelta(microseconds=1)
    hours = Decimal(micros) / Decimal(3_600_000_000)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float) -> bool:
    return Decimal(str(work_hours)) < Decimal("4.50")  # R5, on the rounded value


def round_half_up(x, places: int) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def derive(status: str, pin: Optional[datetime], pout: Optional[datetime], date_str: str, emp: dict) -> dict:
    """All derived fields for a record in its final state (DATA_MODEL.md table)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or pin is None:
        return out
    out["late_minutes"] = compute_late_minutes(pin, date_str, emp["shift_start"])
    if pout is not None:
        out["work_hours"] = compute_work_hours(pin, pout)
        out["overtime_minutes"] = compute_overtime(pout, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = is_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def _cv(v):
    return to_ms(v) if isinstance(v, datetime) else v


def history_out(h: dict) -> dict:
    return {
        "at": to_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": {k: {"from": _cv(v.get("from")), "to": _cv(v.get("to"))} for k, v in (h.get("changes") or {}).items()},
    }


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def get_employee(emp_code: str) -> dict:
    emp = EMP.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def page_skip(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, 2**62)


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        EMP.insert_one(doc)  # the unique index on emp_code decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    total = EMP.count_documents(q)
    cur = EMP.find(q).sort("emp_code", ASCENDING).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        LOGS.insert_one(doc)  # unique (emp_code, date) index: exactly one of N parallel requests wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[ALL_STATUS] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if date_from and date_to and date_from > date_to:
        raise bad_param("date_from", "date_from must not be after date_to")
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = LOGS.count_documents(q) if q else LOGS.estimated_document_count()
    cur = LOGS.find(q).sort(ATT_SORT).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = LOGS.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise RequestValidationError(
            [{"loc": ("body", "punched_at"), "msg": "must be after punch_in and within 24 hours", "type": "value_error"}]
        )
    fields = derive(rec["status"], pin, ts, rec["date"], emp)
    fields["punch_out"] = ts
    # Atomic compare-and-set: only the first of N parallel punch-outs still sees punch_out == null.
    upd = LOGS.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if upd is None:
        raise HTTPException(409, "already punched out")
    return record_out(upd)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee(emp_code)
    rec = LOGS.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    def err(field, msg):
        return RequestValidationError([{"loc": ("body", field), "msg": msg, "type": "value_error"}])

    status = body.status or rec["status"]
    pin = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    pout = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise err("status", "ABSENT/LEAVE clear punch times; do not supply punch_in/punch_out")
        pin = pout = None
    else:
        if pin is None:
            raise err("punch_in", "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(pin, emp["shift_start"], emp["shift_end"]) != date:
            raise err("punch_in", "punch_in must stay on the record's attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise err("punch_out", "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derive(status, pin, pout, date, emp)}
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    if all(new[k] == old[k] for k in ("status", "punch_in", "punch_out")):
        raise err("body", "request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if the record is exactly as we read it (same primary
    # fields and same history length). The loser gets 409 instead of silently losing history.
    n = len(rec.get("history") or [])
    flt = {
        "_id": rec["_id"],
        "status": old["status"],
        "punch_in": old["punch_in"],
        "punch_out": old["punch_out"],
        f"history.{n}": {"$exists": False},
    }
    if n:
        flt[f"history.{n - 1}"] = {"$exists": True}
    upd = LOGS.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if upd is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(upd)


# --------------------------------------------------------------------------- #
# Aggregation builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def working_days(first: date, last: date, joined_on: str) -> int:
    start = max(first, date.fromisoformat(joined_on))
    n, d = 0, start
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


D = Decimal128
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
DAY_VALUE = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
PRESENT_DAYS = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_VALUE, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def half_up_dec(expr, places: int):
    """Half-up rounding in the database. $round is banker's rounding, so use floor(x*10^p + 0.5)/10^p
    on exact Decimal128 values. Null in -> null out."""
    f = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, f]}, D("0.5")]}}, f]}


def employee_monthly_pipeline(emp_code: str, month: str):
    first, last = month_bounds(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ], {}


def department_summary_pipeline(month: str, department: Optional[str]):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    hint = "department_1_joined_on_1" if department is not None else "joined_on_1"
    pipeline = [
        {"$match": match},
        # one indexed lookup per employee (emp_code, date); employees with no logs get m = []
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_DAYS},
                    "wh_sum": {"$sum": {"$cond": [HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_cnt": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": LATE_MIN},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "m",
        }},
        {"$set": {"m": {"$first": "$m"}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$m.present_days"},
            "wh_sum": {"$sum": "$m.wh_sum"},
            "wh_cnt": {"$sum": "$m.wh_cnt"},
            "late_count": {"$sum": "$m.late_count"},
            "total_late_minutes": {"$sum": "$m.total_late_minutes"},
            "leave_count": {"$sum": "$m.leave_count"},
            "on_duty_count": {"$sum": "$m.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$toDouble": "$present_days"},
            "avg_work_hours": {"$cond": [
                {"$eq": ["$wh_cnt", 0]}, None,
                {"$toDouble": half_up_dec({"$divide": ["$wh_sum", "$wh_cnt"]}, 2)},
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
    ]
    return "employees", pipeline, {"hint": hint}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # ranking happens inside the department
    pipeline += [
        # $rank = standard competition ranking (1,2,2,4), computed on the whole set BEFORE limit
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return "attendance_logs", pipeline, {}


def department_trend_pipeline(department: str, d_from: str, d_to: str):
    zero = {"h": 0, "p": 0, "l": 0}
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                {"$project": {
                    "_id": 0, "d": "$date", "h": {"$literal": 0},
                    "p": {"$cond": [IS_PRESENT, DAY_VALUE, 0]},  # presence on any day incl. weekends
                    "l": IS_LATE,
                }},
            ],
            "as": "logs",
        }},
        # Turn each employee into events: a join (headcount +1, clamped to `from`), their logs, and
        # sentinels on `from`/`to` so the densified range always covers the whole window.
        {"$project": {"_id": 0, "ev": {"$concatArrays": [
            [{"d": d_from, **zero}, {"d": d_to, **zero}],
            [{"d": {"$cond": [{"$lt": ["$joined_on", d_from]}, d_from, "$joined_on"]}, "h": 1, "p": 0, "l": 0}],
            "$logs",
        ]}}},
        {"$unwind": "$ev"},
        {"$match": {"ev.d": {"$lte": d_to}}},  # employees who join after `to` drop out
        {"$group": {"_id": "$ev.d", "h": {"$sum": "$ev.h"}, "p": {"$sum": "$ev.p"}, "l": {"$sum": "$ev.l"}}},
        {"$set": {"date": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": "full"}}},  # gap filling
        {"$set": {k: {"$ifNull": [f"${k}", 0]} for k in ("h", "p", "l")}},
        {"$setWindowFields": {  # running headcount
            "sortBy": {"date": 1},
            "output": {"headcount": {"$sum": "$h", "window": {"documents": ["unbounded", "current"]}}},
        }},
        {"$set": {"wd": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]}}},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$wd", {"$gt": ["$headcount", 0]}]},
            half_up_dec({"$divide": [{"$toDecimal": "$p"}, "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {  # 7-day moving average inside the range ($avg skips nulls)
            "sortBy": {"date": 1},
            "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": "$wd",
            "headcount": 1,
            "present_count": {"$toDouble": "$p"},
            "late_count": "$l",
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": half_up_dec("$ma", 4)},
        }},
    ]
    return "employees", pipeline, {}


def run(spec):
    coll, pipeline, kw = spec
    return list(db[coll].aggregate(pipeline, allowDiskUse=True, **kw))


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    rows = run(employee_monthly_pipeline(emp_code, month))
    r = rows[0] if rows else {}
    wd = working_days(first, last, emp["joined_on"])
    present = r.get("present_days", 0)
    pct = round_half_up(Decimal(str(present)) * 100 / wd, 2) if wd else None
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": float(present),
        "leave_days": r.get("leave_days", 0), "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    return {"month": month, "items": run(department_summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    return {"month": month, "items": run(late_leaderboard_pipeline(month, limit, department))}


def check_range(d_from: str, d_to: str):
    if d_to < d_from:
        raise bad_param("to", "'to' must not be before 'from'")
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1 > 92:
        raise bad_param("to", "range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: DateStr,
):
    check_range(from_, to)
    if EMP.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": run(department_trend_pipeline(department, from_, to))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[ALL_STATUS] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    def need(**kw):
        for k, v in kw.items():
            if v is None:
                raise bad_param(k, "field required")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise bad_param("date_from", "date_from must not be after date_to")
        cmd = {
            "find": "attendance_logs",
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATT_SORT),
            "skip": page_skip(page, page_size),
            "limit": page_size,
        }
        coll = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            spec = employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month=month)
            spec = department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            spec = late_leaderboard_pipeline(month, limit, department)
        else:
            need(department=department, **{"from": from_, "to": to})
            check_range(from_, to)
            spec = department_trend_pipeline(department, from_, to)
        coll, pipeline, kw = spec
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True, **kw}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }