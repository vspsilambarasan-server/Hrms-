import os
from datetime import datetime, date, timedelta
from database import get_db_connection

def analyze_employee_shift_gaps(employee_id, target_date, conn=None):
    """
    Analyzes an employee's actual biometric scans on target_date against
    their expected shift pattern to identify missing punch gaps:
      - MISSING_CLOCK_IN: Missed morning shift start swipe.
      - MISSING_CLOCK_OUT: Worked day but missed evening logout swipe.
      - MISSING_LUNCH_OUT: Worked midday without lunch departure punch.
      - MISSING_LUNCH_RETURN: Swiped out for lunch but missed return swipe.
    Returns a list of dicts describing the identified gaps with suggested corrections.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cursor = conn.cursor()

    # 1. Fetch employee details
    cursor.execute("""
        SELECT e.id, e.emp_no, e.first_name, e.last_name, e.default_shift_id,
               e.weekly_off_day, e.is_admin, e.is_active, d.name as dept_name
        FROM employees e
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE e.id = ?
    """, (employee_id,))
    emp = cursor.fetchone()

    if not emp or not emp["is_active"] or emp["is_admin"]:
        if close_conn:
            conn.close()
        return []

    # 2. Resolve expected shift & off-day status
    cursor.execute("""
        SELECT shift_id, is_off_day
        FROM roster_schedules
        WHERE employee_id = ? AND date = ?
    """, (employee_id, target_date))
    roster = cursor.fetchone()

    shift_id = None
    is_off = False
    if roster and roster["shift_id"]:
        shift_id = roster["shift_id"]
        is_off = bool(roster["is_off_day"])
    else:
        shift_id = emp["default_shift_id"]
        try:
            d_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
            if emp["weekly_off_day"] is not None and d_obj.weekday() == emp["weekly_off_day"]:
                is_off = True
        except Exception:
            pass

    # 3. Fetch punches for target_date
    cursor.execute("""
        SELECT id, punch_time, punch_type, device_id
        FROM attendance_punches
        WHERE employee_id = ? AND punch_time LIKE ?
        ORDER BY punch_time ASC
    """, (employee_id, f"{target_date}%"))
    raw_punches = [dict(r) for r in cursor.fetchall()]

    # If scheduled off-day and no punches, no gap
    if is_off and not raw_punches:
        if close_conn:
            conn.close()
        return []

    # 4. Resolve shift timing rules
    cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    shift_row = cursor.fetchone()
    shift = dict(shift_row) if shift_row else {
        "name": "General Shift", "start_time": "09:00", "end_time": "18:00",
        "grace_late_mins": 15, "grace_early_mins": 15, "break_mins": 60, "is_overnight": 0
    }

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    is_today = (target_date == today_str)

    start_time_str = shift.get("start_time", "09:00")
    if len(start_time_str) == 5:
        start_time_str += ":00"
    end_time_str = shift.get("end_time", "18:00")
    if len(end_time_str) == 5:
        end_time_str += ":00"

    sched_start_dt = datetime.strptime(f"{target_date} {start_time_str}", "%Y-%m-%d %H:%M:%S")
    sched_end_dt = datetime.strptime(f"{target_date} {end_time_str}", "%Y-%m-%d %H:%M:%S")
    if shift.get("is_overnight") or (end_time_str < start_time_str):
        sched_end_dt += timedelta(days=1)

    punches = []
    for i, p in enumerate(raw_punches):
        p_time = p["punch_time"][11:] if len(p["punch_time"]) >= 19 else p["punch_time"]
        pt = (p.get("punch_type") or "").upper()
        if not pt:
            pt = "IN" if (i % 2 == 0) else "OUT"
        punches.append({
            "id": p["id"],
            "full_time": p["punch_time"],
            "time": p_time,
            "type": pt
        })

    gaps = []
    first_punch = punches[0] if punches else None
    last_punch = punches[-1] if punches else None

    # Check 1: MISSING_CLOCK_IN
    # Occurs if:
    # A) Employee has punches later in the day (e.g. out at 13:00 or 18:00), but first punch is > 11:30
    # B) Or employee has 0 punches, and current time > sched_start + 1 hour (or historical day)
    has_clock_in = False
    if punches:
        if first_punch["time"] <= "11:30:00":
            has_clock_in = True
        else:
            # First punch was late morning / afternoon; missed shift entry
            gaps.append({
                "employee_id": employee_id,
                "date": target_date,
                "gap_type": "MISSING_CLOCK_IN",
                "expected_time": start_time_str,
                "suggested_punch_type": "IN",
                "suggested_timestamp": f"{target_date} {start_time_str}",
                "description": f"First scan recorded at {first_punch['time']}. Expected shift clock-in at {start_time_str} missing."
            })
            has_clock_in = True # Not absent, just missed in-punch
    else:
        # Zero punches recorded
        if not is_off:
            grace_dt = sched_start_dt + timedelta(minutes=int(shift.get("grace_late_mins", 15)) + 45)
            if (is_today and now >= grace_dt) or (not is_today and target_date < today_str):
                gaps.append({
                    "employee_id": employee_id,
                    "date": target_date,
                    "gap_type": "MISSING_CLOCK_IN",
                    "expected_time": start_time_str,
                    "suggested_punch_type": "IN",
                    "suggested_timestamp": f"{target_date} {start_time_str}",
                    "description": f"No punches recorded for scheduled working shift ({start_time_str} - {end_time_str})."
                })

    # Check 2 & 3: LUNCH BREAK GAPS (Only for regular day shifts spanning >= 6 hrs)
    shift_span_hrs = (sched_end_dt - sched_start_dt).total_seconds() / 3600.0
    if shift_span_hrs >= 6.0 and punches and start_time_str < "12:00:00" and end_time_str > "15:00:00":
        lunch_out_punch = next((p for p in punches if "12:30:00" <= p["time"] <= "13:45:00" and p["type"] == "OUT"), None)
        if not lunch_out_punch:
            lunch_out_punch = next((p for p in punches if "12:40:00" <= p["time"] <= "13:30:00"), None)

        lunch_in_punch = None
        if lunch_out_punch:
            lunch_in_punch = next((p for p in punches if p["time"] > lunch_out_punch["time"] and p["time"] <= "14:45:00" and p["type"] == "IN"), None)
            if not lunch_in_punch:
                lunch_in_punch = next((p for p in punches if p["time"] > lunch_out_punch["time"] and p["time"] <= "14:45:00"), None)
        else:
            lunch_in_punch = next((p for p in punches if "13:35:00" <= p["time"] <= "14:45:00" and p["type"] == "IN"), None)

        # Check MISSING_LUNCH_OUT: Has midday return or evening logout, but no lunch out punch
        if not lunch_out_punch and lunch_in_punch:
            can_check_lunch = True
            if is_today and now.strftime("%H:%M:%S") < "14:00:00":
                can_check_lunch = False
            if can_check_lunch:
                gaps.append({
                    "employee_id": employee_id,
                    "date": target_date,
                    "gap_type": "MISSING_LUNCH_OUT",
                    "expected_time": "13:00:00",
                    "suggested_punch_type": "OUT",
                    "suggested_timestamp": f"{target_date} 13:00:00",
                    "description": f"Lunch return scanned at {lunch_in_punch['time']}, but departure punch (~13:00) missing."
                })

        # Check MISSING_LUNCH_RETURN: Swiped out for lunch, but missed return punch
        if lunch_out_punch and not lunch_in_punch:
            can_check_return = True
            if is_today and now.strftime("%H:%M:%S") < "14:30:00":
                can_check_return = False
            if can_check_return:
                gaps.append({
                    "employee_id": employee_id,
                    "date": target_date,
                    "gap_type": "MISSING_LUNCH_RETURN",
                    "expected_time": "14:00:00",
                    "suggested_punch_type": "IN",
                    "suggested_timestamp": f"{target_date} 14:00:00",
                    "description": f"Swiped out for lunch at {lunch_out_punch['time']}, but return punch (~14:00) missing."
                })

    # Check 4: MISSING_CLOCK_OUT
    # Per factory floor policy: on 6:00 PM shift ends with NO punch required.
    # Shifts ending at 18:00 do NOT flag MISSING_CLOCK_OUT.
    if not end_time_str.startswith("18:00"):
        if punches and first_punch["time"] <= "12:00:00":
            end_grace_dt = sched_end_dt + timedelta(minutes=30)
            has_ended = (not is_today and target_date < today_str) or (is_today and now >= end_grace_dt)
            if has_ended:
                has_evening_out = any(p for p in punches if p["type"] == "OUT" and p["time"] >= "16:00:00")
                if not has_evening_out or last_punch["type"] == "IN":
                    gaps.append({
                        "employee_id": employee_id,
                        "date": target_date,
                        "gap_type": "MISSING_CLOCK_OUT",
                        "expected_time": end_time_str,
                        "suggested_punch_type": "OUT",
                        "suggested_timestamp": f"{target_date} {end_time_str}",
                        "description": f"Clocked in at {first_punch['time']}, but shift clock-out swipe at {end_time_str} missing."
                    })

    if close_conn:
        conn.close()

    return gaps


def scan_and_record_missing_punches(target_date=None, days_back=7):
    """
    Scans active workforce over recent dates to identify and persist missing punch gaps
    in missing_punch_alerts table. Returns count of new alerts and total pending.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    now = datetime.now()
    if target_date:
        dates_to_scan = [target_date]
    else:
        # Scan from today - days_back up to today
        dates_to_scan = [
            (now - timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(days_back, -1, -1)
        ]

    cursor.execute("SELECT id FROM employees WHERE is_active = 1 AND (is_admin IS NULL OR is_admin = 0)")
    employees = cursor.fetchall()

    new_alerts_count = 0

    for d_str in dates_to_scan:
        for emp in employees:
            eid = emp["id"]
            detected = analyze_employee_shift_gaps(eid, d_str, conn=conn)
            for g in detected:
                # Check if already exists in missing_punch_alerts
                cursor.execute("""
                    SELECT id, status FROM missing_punch_alerts
                    WHERE employee_id = ? AND date = ? AND gap_type = ? AND suggested_punch_type = ?
                """, (g["employee_id"], g["date"], g["gap_type"], g["suggested_punch_type"]))
                existing = cursor.fetchone()

                if not existing:
                    cursor.execute("""
                        INSERT INTO missing_punch_alerts (
                            employee_id, date, gap_type, expected_time,
                            suggested_punch_type, suggested_timestamp,
                            description, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
                    """, (
                        g["employee_id"], g["date"], g["gap_type"], g["expected_time"],
                        g["suggested_punch_type"], g["suggested_timestamp"],
                        g["description"]
                    ))
                    new_alerts_count += 1

    # Auto-resolve any pending alerts if an actual punch exists now
    cursor.execute("""
        SELECT id, employee_id, date, gap_type, suggested_punch_type, suggested_timestamp
        FROM missing_punch_alerts
        WHERE status = 'PENDING'
    """)
    pending_alerts = cursor.fetchall()
    for a in pending_alerts:
        cursor.execute("""
            SELECT id FROM attendance_punches
            WHERE employee_id = ? AND punch_time LIKE ?
        """, (a["employee_id"], f"{a['date']}%"))
        punches = cursor.fetchall()
        # If punches exist and no longer triggers this gap, resolve it
        current_gaps = analyze_employee_shift_gaps(a["employee_id"], a["date"], conn=conn)
        has_this_gap = any(cg["gap_type"] == a["gap_type"] for cg in current_gaps)
        if not has_this_gap:
            cursor.execute("""
                UPDATE missing_punch_alerts
                SET status = 'APPROVED',
                    approved_by = 'Auto-Resolved (Punch Detected)',
                    approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (a["id"],))

    conn.commit()

    cursor.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts WHERE status = 'PENDING'")
    total_pending = cursor.fetchone()["cnt"]
    conn.close()

    return {
        "new_alerts_count": new_alerts_count,
        "total_pending": total_pending
    }


def auto_resolve_alerts_for_punch(employee_id, punch_date, conn=None):
    """
    Hook called whenever a punch is manually inserted or synced for an employee.
    Re-checks remaining gaps for employee on that date and resolves any cleared alerts.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, gap_type FROM missing_punch_alerts
        WHERE employee_id = ? AND date = ? AND status = 'PENDING'
    """, (employee_id, punch_date))
    pending = cursor.fetchall()

    if pending:
        remaining_gaps = analyze_employee_shift_gaps(employee_id, punch_date, conn=conn)
        rem_types = {g["gap_type"] for g in remaining_gaps}
        for a in pending:
            if a["gap_type"] not in rem_types:
                cursor.execute("""
                    UPDATE missing_punch_alerts
                    SET status = 'APPROVED',
                        approved_by = 'Auto-Resolved (Punch Added)',
                        approved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (a["id"],))
        conn.commit()

    if close_conn:
        conn.close()
