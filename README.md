# Employee Attendance & Analytics API

FastAPI + MongoDB attendance service. All code is in app/main.py.

## Run
    python -m venv .venv
    .venv\Scripts\activate        (Mac/Linux: source .venv/bin/activate)
    pip install -r requirements.txt
    set MONGO_URI and MONGO_DB in the environment (or in a local .env)
    uvicorn app.main:app --port 8000

Indexes are created automatically at startup. GET /health pings MongoDB.

## Notes
- All endpoints in the contract are implemented, including /admin/explain/{endpoint}.
- moving_avg_7d averages the already-rounded (4 dp) attendance_rate values.
- Tested locally against MongoDB Atlas with the sample data plus my own cases.
- Missing / known gaps: none intentionally.