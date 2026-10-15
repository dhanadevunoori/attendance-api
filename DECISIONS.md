# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

# DECISIONS.md

1. **Indexes.** On employees: `emp_code` unique (it identifies a person and stops duplicates), plus `(department, emp_code)`, `(department, joined_on)` and `joined_on` for filtering and headcount. On attendance_logs: unique `(emp_code, date)` so there is one record per day; `(date desc, emp_code)` for the list sort and month ranges; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the record to punch out. I skipped a date-only index because `(date, emp_code)` already covers it.

2. **Punch-in race.** Both requests work out the same `emp_code` and date and try to insert. The unique index lets MongoDB accept one; the other gets a duplicate-key error and I return 409. I never read first, so there is no gap between checking and inserting.

3. **Ties.** `$rank` gives tied people the same rank across everyone first, and only then I keep `rank <= limit`. So both tied people are returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs. Someone with no logs still counts, with zeros.

5. **100x data.** I would store monthly totals per employee, updated on punch-in, punch-out and correction, so analytics stop scanning raw logs.