# REVIEW.md

# REVIEW.md

| # | Where | What is wrong | How I'd notice | How I fixed it |
|---|---|---|---|---|
| 1 | `punch_in`, `datetime.now()` / `fromtimestamp()` | Uses the server's local time zone and naive datetimes, so the date depends on the server. | Server in UTC, punch at 23:00 UTC (04:30 IST next day): wrong `date`. | Aware UTC instants; date is worked out in IST (`attendance_date`). |
| 2 | `punch_in` (date) | No overnight rule (R1): 01:00 on a 22:00-06:00 shift lands on the wrong day. | Overnight employee punching in after midnight. | Subtract a day when the IST time is before `shift_end` on an overnight shift. |
| 3 | `compute_late_minutes` | Builds the shift start on the punch-in's own day, ignoring time zone and overnight shifts. | Overnight shift, or server not in IST. | Shift start is built from the attendance date in IST. |
| 4 | `compute_late_minutes` | `minutes > 10` on whole minutes, so 09:40:01 gives 0 instead of 10 (R2 says strictly more than 10:00). | Shift 09:30, punch 09:40:01. | Compare in seconds (`> 600`), then floor to minutes. |
| 5 | `compute_work_hours` | `round()` is banker's rounding; R4 needs half-up. | 4.505 h gives 4.5, should be 4.51. | `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3); end time ignores overnight shifts. | Punch-out 15 minutes after shift end returns 15. | Threshold added; shift end moves to next day for overnight shifts. |
| 7 | `punch_in` (seconds, 0) | Not truncated to whole seconds; `if body.punched_at` treats 0 as missing. | A punch with milliseconds. | Truncate to seconds; check `is not None`. |
| 8 | `punch_in` (unknown employee) | `emp` is None, then `emp["shift_start"]` raises, giving a 500 instead of 404. | Punch in with an unknown `emp_code`. | `get_employee` raises 404. |
| 9 | `PunchInIn` | `status` is any string and `punched_at` accepts seconds or floats. | `status: "FOO"`, `punched_at: 1783312500`. | `Literal` status; strict int with a range, giving 422. |
| 10 | `punch_in` (race) | Check-then-insert: parallel requests can both pass the check and both insert. | 10 parallel punch-ins. | Unique `(emp_code, date)` index; duplicate key becomes 409. |
| 11 | `punch_in` / `list_attendance` responses | Returns an `id` and raw datetimes instead of epoch ms. | Look at the response. | `record_out` serializer: no id, epoch ms. |
| 12 | `list_employees` | `skip = page * page_size` skips a page too many. | Page 1 misses the first employees. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts everyone, not the filtered set; no sort; page values not validated. | `?department=X` returns the full `total`. | Count with the same filter, sort by `emp_code`, validate with `Query(ge, le)`. |
| 14 | `list_attendance` | Loads everything and sorts in Python; no `emp_code` tiebreak; no date/status validation. | Slow and heavy at 100k records. | Sort, skip and limit in MongoDB with indexes; validation gives 422. |
| 15 | `create_employee` | Check-then-insert race; no input validation; naive `created_at`. | Parallel creates; `joined_on: "2026-02-30"`. | Unique index and duplicate key to 409; pydantic constraints; aware UTC `created_at`. |
| 16 | `health` | Always returns ok without touching MongoDB. | Stop MongoDB: still 200. | Pings MongoDB, 503 on failure. |
| 17 | `MongoClient` | Not `tz_aware`, so reads give naive datetimes. | Subtracting stored and aware times raises. | `tz_aware=True`. |
| 18 | Startup | No indexes except `_id`. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup. |

## Looked at and fine
- The `{"_id": 0}` projection in `list_employees` correctly hides the internal id.
- Plain `def` endpoints run in FastAPI's thread pool and PyMongo is thread safe, so I left them.
- `load_dotenv()` does not override variables that are already set, so real environment variables win.
