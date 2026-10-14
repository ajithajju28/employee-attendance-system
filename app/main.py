"""
Employee Attendance & Analytics API - Production Implementation
FastAPI + MongoDB (PyMongo)
Implements all specifications in openapi.yaml and DATA_MODEL.md (R1 - R10).
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes exist at startup
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Precision and Rounding Helpers (R8)
# --------------------------------------------------------------------------- #
def round_half_up_2(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Time & Business Logic Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Truncates sub-seconds and returns UTC datetime."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_current_epoch_ms() -> int:
    now = datetime.now(timezone.utc)
    return int(now.timestamp()) * 1000


def get_attendance_date_and_shift(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> tuple[str, datetime, datetime]:
    """
    R1: Shifts & dates are in IST (UTC+05:30).
    Overnight shift: shift_end <= shift_start.
    If punch-in IST time < shift_end, it belongs to the previous calendar day's shift.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    eh_h, eh_m = map(int, shift_end_str.split(":"))

    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    if is_overnight and punch_in_ist.time() < time(eh_h, eh_m):
        base_date = punch_in_ist.date() - timedelta(days=1)
    else:
        base_date = punch_in_ist.date()

    shift_start_dt = datetime(base_date.year, base_date.month, base_date.day, sh_h, sh_m, 0, tzinfo=IST)
    if is_overnight:
        shift_end_date = base_date + timedelta(days=1)
        shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh_h, eh_m, 0, tzinfo=IST)
    else:
        shift_end_dt = datetime(base_date.year, base_date.month, base_date.day, eh_h, eh_m, 0, tzinfo=IST)

    return base_date.isoformat(), shift_start_dt.astimezone(timezone.utc), shift_end_dt.astimezone(timezone.utc)


def compute_late_minutes(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    """R2: Grace 10 min. Late if > 10:00 elapsed. Minutes = floor(since shift_start)."""
    diff_sec = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """R3: Floor minutes between shift_end and punch_out. Counted only if >= 30."""
    diff_sec = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = diff_sec / 3600.0
    return round_half_up_2(raw_hours) or 0.0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats MongoDB document to exact OpenAPI spec."""
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for h in doc.get("history", []):
        serialized_changes = {}
        for k, v in h.get("changes", {}).items():
            c_from = v.get("from")
            c_to = v.get("to")
            if k in ("punch_in", "punch_out"):
                c_from = datetime_to_epoch_ms(c_from) if isinstance(c_from, datetime) else c_from
                c_to = datetime_to_epoch_ms(c_to) if isinstance(c_to, datetime) else c_to
            serialized_changes[k] = {"from": c_from, "to": c_to}

        res["history"].append({
            "at": datetime_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": serialized_changes,
        })
    return res


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Core Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database ping failed")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(created_at_dt)
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        emp["created_at"] = datetime_to_epoch_ms(emp["created_at"])
        items.append(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    p_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    p_dt = epoch_ms_to_datetime_utc(p_ms)

    att_date, shift_start_utc, _ = get_attendance_date_and_shift(p_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(p_dt, shift_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    p_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    out_dt = epoch_ms_to_datetime_utc(p_ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record or not record.get("punch_in"):
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    in_dt = record["punch_in"].replace(tzinfo=timezone.utc) if record["punch_in"].tzinfo is None else record["punch_in"]

    diff_seconds = (out_dt - in_dt).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    _, _, shift_end_utc = get_attendance_date_and_shift(in_dt, emp["shift_start"], emp["shift_end"])
    wh = compute_work_hours(in_dt, out_dt)
    ot = compute_overtime_minutes(out_dt, shift_end_utc)
    hd = compute_half_day(wh)

    # Concurrency safe: only one simultaneous punch_out succeeds
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": out_dt,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd,
        }},
        return_document=True,
    )
    if not res:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_doc(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Record not found for given date")

    cur_status = record["status"]
    cur_in = record.get("punch_in")
    cur_out = record.get("punch_out")

    new_status = body.status if body.status is not None else cur_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot provide punch times with ABSENT or LEAVE")
        final_in = None
        final_out = None
    else:
        final_in = epoch_ms_to_datetime_utc(body.punch_in) if body.punch_in is not None else cur_in
        final_out = epoch_ms_to_datetime_utc(body.punch_out) if body.punch_out is not None else cur_out

        if not final_in:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        att_date, _, _ = get_attendance_date_and_shift(final_in, emp["shift_start"], emp["shift_end"])
        if att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if final_out:
            diff_s = (final_out - final_in).total_seconds()
            if diff_s <= 0 or diff_s > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if final_in:
        _, shift_start_utc, shift_end_utc = get_attendance_date_and_shift(final_in, emp["shift_start"], emp["shift_end"])
        new_late = compute_late_minutes(final_in, shift_start_utc)
        if final_out:
            new_wh = compute_work_hours(final_in, final_out)
            new_ot = compute_overtime_minutes(final_out, shift_end_utc)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False
    else:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False

    # Track actual changes
    changes = {}
    if cur_status != new_status:
        changes["status"] = {"from": cur_status, "to": new_status}
    if cur_in != final_in:
        changes["punch_in"] = {"from": cur_in, "to": final_in}
    if cur_out != final_out:
        changes["punch_out"] = {"from": cur_out, "to": final_out}
    if record.get("late_minutes", 0) != new_late:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if record.get("work_hours") != new_wh:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
    if record.get("overtime_minutes", 0) != new_ot:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if record.get("half_day", False) != new_hd:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in regularization request")

    hist_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_in,
                "punch_out": final_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": hist_entry},
        },
        return_document=True,
    )
    return serialize_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines (Cross-collection, Aggregations, Gap Filling)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start_date = dt_date(year, m, 1)
    month_end_date = dt_date(year, m, num_days)

    joined_on_date = dt_date.fromisoformat(emp["joined_on"])
    calc_start = max(month_start_date, joined_on_date)

    working_days = 0
    if calc_start <= month_end_date:
        cur = calc_start
        while cur <= month_end_date:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}"},
        }},
        {"$project": {
            "date": 1,
            "status": 1,
            "work_hours": 1,
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            "half_day": {"$ifNull": ["$half_day", False]},
            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},  # Mon-Fri
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        p_days = round_half_up_2(data["present_days"]) or 0.0
        leave_days = int(data["leave_days"])
        late_count = int(data["late_count"])
        tot_late = int(data["total_late_minutes"])
        tot_ot = int(data["total_overtime_minutes"])
    else:
        p_days = 0.0
        leave_days = 0
        late_count = 0
        tot_late = 0
        tot_ot = 0

    att_pct = None
    if working_days > 0:
        att_pct = round_half_up_2((p_days / working_days) * 100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": p_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": tot_late,
        "total_overtime_minutes": tot_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_end_str = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ecode": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$ecode"]},
                            {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "all_logs": {"$push": "$logs"},
        }},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "flat_logs": {
                "$reduce": {
                    "input": "$all_logs",
                    "initialValue": [],
                    "in": {"$concatArrays": ["$$value", "$$this"]},
                }
            },
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": {
                "$sum": {
                    "$map": {
                        "input": "$flat_logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]},
                                    ]
                                },
                                {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        },
                    }
                }
            },
            "work_hours_list": {
                "$filter": {
                    "input": "$flat_logs",
                    "as": "l",
                    "cond": {
                        "$and": [
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$$l.work_hours", None]},
                        ]
                    },
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$flat_logs",
                        "as": "l",
                        "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
                    }
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$map": {
                        "input": "$flat_logs",
                        "as": "l",
                        "in": {"$ifNull": ["$$l.late_minutes", 0]},
                    }
                }
            },
            "leave_count": {
                "$sum": {
                    "$map": {
                        "input": "$flat_logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                    }
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$map": {
                        "input": "$flat_logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                    }
                }
            },
        }},
        {"$sort": {"department": 1}},
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []
    for d in res:
        if d["headcount"] == 0:
            continue
        wh_records = d.get("work_hours_list", [])
        if wh_records:
            total_wh = sum(item["work_hours"] for item in wh_records)
            avg_wh = round_half_up_2(total_wh / len(wh_records))
        else:
            avg_wh = None

        items.append({
            "department": d["department"],
            "headcount": int(d["headcount"]),
            "present_days": round_half_up_2(d["present_days"]) or 0.0,
            "avg_work_hours": avg_wh,
            "late_count": int(d["late_count"]),
            "total_late_minutes": int(d["total_late_minutes"]),
            "leave_count": int(d["leave_count"]),
            "on_duty_count": int(d["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    emp_match = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": {
            "date": {"$regex": f"^{month}"},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    dt_f = dt_date.fromisoformat(date_from)
    dt_t = dt_date.fromisoformat(date_to)

    if dt_t < dt_f:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
    if (dt_t - dt_f).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days limit")

    pipeline = [
        {"$match": {
            "department": department,
            "joined_on": {"$lte": date_to},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ecode": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$ecode"]},
                            {"$gte": ["$date", date_from]},
                            {"$lte": ["$date", date_to]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
    ]

    log_stats = {d["date"]: d for d in db.employees.aggregate(pipeline)}
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    items = []
    curr = dt_f
    while curr <= dt_t:
        d_str = curr.isoformat()
        is_wd = curr.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= d_str)
        stats = log_stats.get(d_str, {"present_count": 0.0, "late_count": 0})

        rate = None
        if is_wd and hc > 0:
            rate = round_half_up_4(stats["present_count"] / hc)

        items.append({
            "date": d_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": round_half_up_2(stats["present_count"]) or 0.0,
            "late_count": stats["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": None,
        })
        curr += timedelta(days=1)

    for i in range(len(items)):
        start_idx = max(0, i - 6)
        valid_rates = [
            items[j]["attendance_rate"]
            for j in range(start_idx, i + 1)
            if items[j]["attendance_rate"] is not None
        ]
        if valid_rates:
            items[i]["moving_avg_7d"] = round_half_up_4(sum(valid_rates) / len(valid_rates))

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        plan = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {"$group": {"_id": None, "cnt": {"$sum": 1}}},
        ]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipe, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipe = [{"$match": {"joined_on": {"$lte": f"{month}-31"}}}]
        plan = db.command("aggregate", "employees", pipeline=pipe, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipe = [{"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}}]
        plan = db.command("aggregate", "attendance_logs", pipeline=pipe, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required")
        pipe = [{"$match": {"department": department}}]
        plan = db.command("aggregate", "employees", pipeline=pipe, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}
