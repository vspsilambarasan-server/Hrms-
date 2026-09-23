from datetime import datetime, timedelta

def parse_time_str(time_str):
    """Parse 'HH:MM' string to (hour, minute)."""
    parts = [int(p) for p in time_str.split(":")]
    return parts[0], parts[1]

def get_shift_datetimes(shift_date_str, shift):
    """
    Given a shift date string 'YYYY-MM-DD' and shift dict/row,
    returns (scheduled_start_dt, scheduled_end_dt).
    Handles cross-midnight shifts (e.g., 22:00 to 06:00).
    """
    base_date = datetime.strptime(shift_date_str, "%Y-%m-%d")
    s_h, s_m = parse_time_str(shift["start_time"])
    e_h, e_m = parse_time_str(shift["end_time"])

    start_dt = base_date.replace(hour=s_h, minute=s_m, second=0)
    
    if shift["is_overnight"] or (e_h < s_h) or (e_h == s_h and e_m < s_m):
        # Crosses midnight into the next calendar day
        end_dt = (base_date + timedelta(days=1)).replace(hour=e_h, minute=e_m, second=0)
    else:
        end_dt = base_date.replace(hour=e_h, minute=e_m, second=0)

    return start_dt, end_dt

def compute_daily_lateness(shift_date_str, sched_start_dt, all_punches, grace_late_mins=15):
    """
    Computes total lateness across the day:
    1. Morning arrival (past scheduled start, e.g. 09:00 + grace_late_mins).
    2. Morning tea break return (11:00 - 11:15, max 15 mins).
    3. Lunch break return (13:00 - 14:00, max 60 mins).
    4. Evening tea break return (16:00 - 16:15, max 15 mins).
    5. Overtime tea break return (19:00 - 19:15, max 15 mins).
    
    Returns:
      total_late_mins (int), breakdown (dict)
    """
    if not all_punches:
        return 0, {}

    base_date = datetime.strptime(shift_date_str, "%Y-%m-%d")

    # Normalize punch entries to (datetime, punch_type)
    normalized = []
    for p in all_punches:
        if isinstance(p, str):
            t_str = p.strip()
            pt = None
        elif isinstance(p, dict) or hasattr(p, "keys"):
            p_dict = dict(p)
            t_str = (p_dict.get("punch_time") or p_dict.get("full_time") or "").strip()
            pt = p_dict.get("punch_type") or p_dict.get("type")
        elif isinstance(p, (tuple, list)):
            t_str = str(p[0]).strip()
            pt = p[1] if len(p) > 1 else None
        else:
            continue

        if not t_str:
            continue

        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
            try:
                if len(t_str) <= 8:
                    dt_time = datetime.strptime(t_str, fmt).time()
                    dt = datetime.combine(base_date.date(), dt_time)
                else:
                    dt = datetime.strptime(t_str, fmt)
                break
            except ValueError:
                pass

        if dt:
            pt_clean = str(pt).upper() if pt else None
            normalized.append({"dt": dt, "type": pt_clean})

    if not normalized:
        return 0, {}

    normalized.sort(key=lambda x: x["dt"])

    # Infer IN/OUT types if not explicitly specified
    for idx, item in enumerate(normalized):
        if not item["type"] or item["type"] not in ("IN", "OUT"):
            item["inferred_type"] = "IN" if idx % 2 == 0 else "OUT"
        else:
            item["inferred_type"] = item["type"]

    breakdown = {
        "morning_late_mins": 0,
        "morning_tea_late_mins": 0,
        "lunch_late_mins": 0,
        "evening_tea_late_mins": 0,
        "ot_break_late_mins": 0
    }

    # 1. Morning Clock-In Lateness
    first_punch = normalized[0]
    first_dt = first_punch["dt"]
    grace_dt = sched_start_dt + timedelta(minutes=grace_late_mins)
    if first_dt > grace_dt:
        diff_mins = int((first_dt - sched_start_dt).total_seconds() // 60)
        breakdown["morning_late_mins"] = max(0, diff_mins)

    # Scheduled break configurations:
    break_configs = [
        ("Morning Tea",
         base_date.replace(hour=10, minute=30, second=0),
         base_date.replace(hour=12, minute=0, second=0),
         base_date.replace(hour=11, minute=15, second=0),
         15,
         "morning_tea_late_mins"),
        ("Lunch Break",
         base_date.replace(hour=12, minute=15, second=0),
         base_date.replace(hour=14, minute=45, second=0),
         base_date.replace(hour=14, minute=0, second=0),
         60,
         "lunch_late_mins"),
        ("Evening Tea",
         base_date.replace(hour=15, minute=30, second=0),
         base_date.replace(hour=17, minute=0, second=0),
         base_date.replace(hour=16, minute=15, second=0),
         15,
         "evening_tea_late_mins"),
        ("OT Break",
         base_date.replace(hour=18, minute=45, second=0),
         base_date.replace(hour=19, minute=45, second=0),
         base_date.replace(hour=19, minute=15, second=0),
         15,
         "ot_break_late_mins")
    ]

    used_indices = set()
    for b_name, w_start, w_end, target_return, max_dur, b_key in break_configs:
        # Match break pair (OUT, IN)
        for i in range(len(normalized) - 1):
            if i in used_indices:
                continue
            p_out = normalized[i]
            p_in = normalized[i + 1]
            if p_out["inferred_type"] == "OUT" and p_in["inferred_type"] == "IN":
                if (w_start <= p_out["dt"] <= w_end) or (w_start <= p_in["dt"] <= w_end):
                    used_indices.add(i)
                    used_indices.add(i + 1)
                    duration_mins = int((p_in["dt"] - p_out["dt"]).total_seconds() // 60)
                    excess_dur = max(0, duration_mins - max_dur)
                    return_late = max(0, int((p_in["dt"] - target_return).total_seconds() // 60))
                    breakdown[b_key] = max(excess_dur, return_late)
                    break

        # Fallback: check isolated IN return punch in window
        if breakdown[b_key] == 0:
            for j, p in enumerate(normalized):
                if j in used_indices or j == 0:
                    continue
                if p["inferred_type"] == "IN" and w_start <= p["dt"] <= w_end:
                    if p["dt"] > target_return:
                        return_late = int((p["dt"] - target_return).total_seconds() // 60)
                        breakdown[b_key] = max(0, return_late)
                        used_indices.add(j)
                        break

    total_late_mins = sum(breakdown.values())
    return total_late_mins, breakdown

def evaluate_attendance(shift_date_str, shift, punch_in_str, punch_out_str, is_off_day=False, is_holiday=False, all_punches=None):
    """
    Evaluates attendance punch in & out against scheduled shift.
    
    Supports:
      - 6:00 PM shift end with no clock-out swipe required
      - Overtime break from 7:00 PM to 7:15 PM (15 mins deducted)
      - Lateness offset: morning arrival and break lateness deducted from OT
    
    Returns a dict with:
      - work_hours (float)
      - late_mins (int)
      - late_deduction_mins (int)
      - early_leave_mins (int)
      - status (PRESENT, LATE, HALF_DAY, ABSENT, WEEKLY_OFF, HOLIDAY)
      - shift_allowance (float)
      - pre_shift_ot_hours (float)
      - post_shift_ot_hours (float)
      - gross_ot_hours (float)
      - total_ot_hours (float)
      - ot_type (NORMAL, WEEKEND, HOLIDAY)
    """
    if is_holiday and not punch_in_str:
        return {
            "work_hours": 0.0,
            "late_mins": 0,
            "late_deduction_mins": 0,
            "early_leave_mins": 0,
            "status": "HOLIDAY",
            "shift_allowance": 0.0,
            "pre_shift_ot_hours": 0.0,
            "post_shift_ot_hours": 0.0,
            "gross_ot_hours": 0.0,
            "total_ot_hours": 0.0,
            "ot_type": "HOLIDAY"
        }

    if is_off_day and not punch_in_str:
        return {
            "work_hours": 0.0,
            "late_mins": 0,
            "late_deduction_mins": 0,
            "early_leave_mins": 0,
            "status": "WEEKLY_OFF",
            "shift_allowance": 0.0,
            "pre_shift_ot_hours": 0.0,
            "post_shift_ot_hours": 0.0,
            "gross_ot_hours": 0.0,
            "total_ot_hours": 0.0,
            "ot_type": "WEEKEND"
        }

    if not punch_in_str:
        return {
            "work_hours": 0.0,
            "late_mins": 0,
            "late_deduction_mins": 0,
            "early_leave_mins": 0,
            "status": "ABSENT",
            "shift_allowance": 0.0,
            "pre_shift_ot_hours": 0.0,
            "post_shift_ot_hours": 0.0,
            "gross_ot_hours": 0.0,
            "total_ot_hours": 0.0,
            "ot_type": "NORMAL"
        }

    sched_start, sched_end = get_shift_datetimes(shift_date_str, shift)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    is_shift_ended = (shift_date_str < today_str) or (shift_date_str == today_str and now >= sched_end)

    # If employee has punched in but has not yet punched out
    if not punch_out_str:
        if is_shift_ended:
            # Per factory policy: shift ends at 18:00 with NO punch required.
            punch_out_str = sched_end.strftime("%Y-%m-%d %H:%M:%S")
        else:
            # Shift still actively in progress
            grace_late = timedelta(minutes=shift.get("grace_late_mins", 15))
            try:
                p_in = datetime.strptime(punch_in_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                p_in = datetime.strptime(punch_in_str, "%Y-%m-%d %H:%M")
            
            late_mins = 0
            if p_in > (sched_start + grace_late):
                diff = p_in - sched_start
                late_mins = int(diff.total_seconds() // 60)
            
            status = "LATE" if late_mins > 0 else "PRESENT"
            allowance_rate = shift.get("allowance_rate", 0.0)
            return {
                "work_hours": 0.0,
                "late_mins": late_mins,
                "late_deduction_mins": 0,
                "early_leave_mins": 0,
                "status": status,
                "shift_allowance": float(allowance_rate),
                "pre_shift_ot_hours": 0.0,
                "post_shift_ot_hours": 0.0,
                "gross_ot_hours": 0.0,
                "total_ot_hours": 0.0,
                "ot_type": "NORMAL"
            }

    # Parse timestamps
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        p_in = datetime.strptime(punch_in_str, fmt)
    except ValueError:
        p_in = datetime.strptime(punch_in_str, "%Y-%m-%d %H:%M")

    try:
        p_out = datetime.strptime(punch_out_str, fmt)
    except ValueError:
        p_out = datetime.strptime(punch_out_str, "%Y-%m-%d %H:%M")

    if p_out < p_in:
        p_out += timedelta(days=1)

    min_full = float(shift.get("min_hours_full_day", 8.0))
    min_half = float(shift.get("min_hours_half_day", 4.5))

    # Calculate Lateness
    if all_punches:
        late_mins, breakdown = compute_daily_lateness(
            shift_date_str, sched_start, all_punches, grace_late_mins=shift.get("grace_late_mins", 15)
        )
    else:
        grace_late = timedelta(minutes=shift.get("grace_late_mins", 15))
        late_mins = 0
        if p_in > (sched_start + grace_late):
            diff = p_in - sched_start
            late_mins = int(diff.total_seconds() // 60)

    # Early Departure
    grace_early = timedelta(minutes=shift.get("grace_early_mins", 15))
    early_leave_mins = 0
    if p_out < (sched_end - grace_early):
        diff = sched_end - p_out
        early_leave_mins = int(diff.total_seconds() // 60)

    # Work Hours calculation
    elapsed_seconds = (p_out - p_in).total_seconds()
    break_seconds = shift.get("break_mins", 0) * 60
    net_work_seconds = max(0, elapsed_seconds - break_seconds)
    work_hours = round(net_work_seconds / 3600.0, 2)

    # Overtime Calculation (Strictly after scheduled shift timing ends)
    ot_threshold_seconds = 15 * 60
    gross_ot_hours = 0.0

    if p_out > sched_end:
        ot_start = sched_end
        ot_end = p_out

        # Check for 19:00 - 19:15 OT break (applicable to shifts ending at 18:00)
        base_date = datetime.strptime(shift_date_str, "%Y-%m-%d")
        ot_break_start = base_date.replace(hour=19, minute=0, second=0)
        ot_break_end = base_date.replace(hour=19, minute=15, second=0)

        if ot_start <= ot_break_start and ot_end > ot_break_start:
            if ot_end <= ot_break_end:
                gross_ot_seconds = (ot_break_start - ot_start).total_seconds()
            else:
                gross_ot_seconds = (ot_end - ot_start).total_seconds() - 900.0
        else:
            gross_ot_seconds = (ot_end - ot_start).total_seconds()

        if gross_ot_seconds >= ot_threshold_seconds:
            gross_ot_hours = round(gross_ot_seconds / 3600.0, 2)
        else:
            gross_ot_hours = 0.0

    ot_type = "HOLIDAY" if is_holiday else ("WEEKEND" if is_off_day else "NORMAL")
    if is_off_day or is_holiday:
        gross_ot_hours = work_hours
        total_ot_hours = work_hours
        post_shift_ot_hours = work_hours
        late_deduction_mins = 0
    else:
        late_deduction_mins = late_mins
        net_ot_hours = max(0.0, round(gross_ot_hours - (late_mins / 60.0), 2))
        total_ot_hours = net_ot_hours
        post_shift_ot_hours = net_ot_hours

    pre_shift_ot_hours = 0.0

    # Status Determination
    if work_hours >= min_full:
        status = "LATE" if late_mins > 0 else "PRESENT"
    elif work_hours >= min_half:
        status = "HALF_DAY"
    else:
        status = "ABSENT"

    # Shift Allowance
    allowance_rate = shift.get("allowance_rate", 0.0)
    if status in ("PRESENT", "LATE"):
        shift_allowance = float(allowance_rate)
    elif status == "HALF_DAY":
        shift_allowance = round(float(allowance_rate) * 0.5, 2)
    else:
        shift_allowance = 0.0

    return {
        "work_hours": work_hours,
        "late_mins": late_mins,
        "late_deduction_mins": late_deduction_mins,
        "early_leave_mins": early_leave_mins,
        "status": status,
        "shift_allowance": shift_allowance,
        "pre_shift_ot_hours": pre_shift_ot_hours,
        "post_shift_ot_hours": post_shift_ot_hours,
        "gross_ot_hours": gross_ot_hours,
        "total_ot_hours": total_ot_hours,
        "ot_type": ot_type
    }

def parse_time_12_or_24(t_str):
    """Parses '09:00 AM' or '09:00' to (hour, minute)."""
    t_str = t_str.strip().upper()
    try:
        dt = datetime.strptime(t_str, "%I:%M %p")
        return dt.hour, dt.minute
    except ValueError:
        try:
            dt = datetime.strptime(t_str, "%H:%M")
            return dt.hour, dt.minute
        except ValueError:
            parts = [int(p) for p in t_str.split(":")[:2]]
            return parts[0], parts[1]

def calculate_duty_segments_breakdown(shift_date_str, punch_in_str, punch_out_str, intervals):
    """
    Given daily punch in/out timestamps and duty_intervals rows,
    computes exact minutes and hours spent in each segment:
      1. Morning Shift (Start to Tea)
      2. Morning Shift (Tea to Lunch)
      3. Afternoon Shift (Lunch to Break)
      4. Afternoon Shift (Break to Shift End)
      5. Overtime Session 1
      6. Overtime Session 2
      7. Night Duty Session
    """
    if not punch_in_str or not punch_out_str:
        return []

    try:
        p_in = datetime.strptime(punch_in_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        p_in = datetime.strptime(punch_in_str, "%Y-%m-%d %H:%M")

    try:
        p_out = datetime.strptime(punch_out_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        p_out = datetime.strptime(punch_out_str, "%Y-%m-%d %H:%M")

    if p_out < p_in:
        p_out += timedelta(days=1)

    base_date = datetime.strptime(shift_date_str, "%Y-%m-%d")
    results = []

    for seg in intervals:
        s_h, s_m = parse_time_12_or_24(seg["time_in"])
        e_h, e_m = parse_time_12_or_24(seg["time_out"])

        # Determine segment datetime boundaries
        # If night duty (e.g. 21:30 to 05:30)
        is_night = seg.get("is_night", 0) or (e_h < s_h)
        seg_start = base_date.replace(hour=s_h, minute=s_m, second=0)
        seg_end = (base_date + timedelta(days=1)).replace(hour=e_h, minute=e_m, second=0) if is_night else base_date.replace(hour=e_h, minute=e_m, second=0)

        # Intersection with punch [p_in, p_out]
        overlap_start = max(p_in, seg_start)
        overlap_end = min(p_out, seg_end)

        if overlap_end > overlap_start:
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            hours_worked = round(overlap_seconds / 3600.0, 2)
        else:
            hours_worked = 0.0

        max_capacity_hours = round((seg_end - seg_start).total_seconds() / 3600.0, 2)

        results.append({
            "segment_number": seg["segment_number"],
            "segment_name": seg["segment_name"],
            "time_in": seg["time_in"],
            "time_out": seg["time_out"],
            "is_overtime": seg["is_overtime"],
            "is_night": seg["is_night"],
            "hours_worked": hours_worked,
            "max_hours": max_capacity_hours
        })

    return results

