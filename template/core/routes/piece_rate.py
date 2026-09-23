import csv
import io
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from database import get_db_connection

piece_rate_bp = Blueprint("piece_rate", __name__, url_prefix="/piece-rate")

@piece_rate_bp.route("/")
def index():
    """
    Main Piece-Rate Dashboard & Quantity Tracker register.
    Displays daily KPIs, item-wise production breakdown, and work entries.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    selected_date = request.args.get("date")
    if not selected_date:
        # Default to latest entry date or today
        cursor.execute("SELECT MAX(work_date) as max_d FROM piece_rate_work_entries")
        row = cursor.fetchone()
        selected_date = (row["max_d"] if row and row["max_d"] else None) or date.today().strftime("%Y-%m-%d")

    worker_id = request.args.get("worker_id")
    item_id = request.args.get("item_id")
    status = request.args.get("status", "ALL")

    # Build work entries query
    query = """
        SELECT e.*, w.worker_code, w.name as worker_name, w.phone as worker_phone, 
               w.department_category, w.payment_mode
        FROM piece_rate_work_entries e
        JOIN piece_rate_workers w ON e.worker_id = w.id
        WHERE 1=1
    """
    params = []

    if selected_date and selected_date != "ALL":
        query += " AND e.work_date = ?"
        params.append(selected_date)

    if worker_id and worker_id.isdigit():
        query += " AND e.worker_id = ?"
        params.append(int(worker_id))

    if item_id and item_id.isdigit():
        query += " AND e.item_id = ?"
        params.append(int(item_id))

    if status and status in ("UNPAID", "PAID"):
        query += " AND e.payment_status = ?"
        params.append(status)

    query += " ORDER BY e.work_date DESC, e.id DESC"
    cursor.execute(query, params)
    entries = [dict(r) for r in cursor.fetchall()]

    # Daily KPIs for selected date
    kpi_query = """
        SELECT 
            COALESCE(SUM(quantity_completed), 0) as total_gross_pcs,
            COALESCE(SUM(rejected_quantity), 0) as total_rejected_pcs,
            COALESCE(SUM(payable_quantity), 0) as total_payable_pcs,
            COALESCE(SUM(total_amount), 0) as total_earned_amt,
            COUNT(DISTINCT worker_id) as active_workers_count,
            COUNT(*) as total_entries_count
        FROM piece_rate_work_entries
        WHERE 1=1
    """
    kpi_params = []
    if selected_date and selected_date != "ALL":
        kpi_query += " AND work_date = ?"
        kpi_params.append(selected_date)

    cursor.execute(kpi_query, kpi_params)
    kpis = dict(cursor.fetchone())

    # Overall pending unpaid wages across all dates
    cursor.execute("SELECT COALESCE(SUM(total_amount), 0) as pending_amt, COUNT(*) as pending_cnt FROM piece_rate_work_entries WHERE payment_status = 'UNPAID'")
    pending_info = dict(cursor.fetchone())

    # Item-wise production breakdown for selected date
    item_breakdown_query = """
        SELECT item_name, unit_measure,
               SUM(quantity_completed) as gross_qty,
               SUM(rejected_quantity) as rejected_qty,
               SUM(payable_quantity) as payable_qty,
               SUM(total_amount) as total_amt,
               COUNT(DISTINCT worker_id) as workers_count,
               COUNT(*) as job_count
        FROM piece_rate_work_entries
        WHERE 1=1
    """
    item_params = []
    if selected_date and selected_date != "ALL":
        item_breakdown_query += " AND work_date = ?"
        item_params.append(selected_date)
    item_breakdown_query += " GROUP BY item_name, unit_measure ORDER BY payable_qty DESC"

    cursor.execute(item_breakdown_query, item_params)
    item_summary = [dict(r) for r in cursor.fetchall()]

    # Active workers and active items for modal selectors & filters
    cursor.execute("SELECT id, worker_code, name, department_category FROM piece_rate_workers WHERE is_active = 1 ORDER BY name ASC")
    active_workers = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT id, item_code, item_name, category, unit_measure, default_rate FROM piece_rate_items WHERE is_active = 1 ORDER BY category ASC, item_name ASC")
    active_items = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return render_template(
        "piece_rate/index.html",
        entries=entries,
        kpis=kpis,
        pending_info=pending_info,
        item_summary=item_summary,
        selected_date=selected_date,
        selected_worker_id=worker_id,
        selected_item_id=item_id,
        selected_status=status,
        active_workers=active_workers,
        active_items=active_items
    )


@piece_rate_bp.route("/log-entry", methods=["POST"])
def log_entry():
    """
    Records an item-wise quantity entry for a contract worker with auto-computed total earnings.
    """
    worker_id = request.form.get("worker_id")
    work_date = request.form.get("work_date", date.today().strftime("%Y-%m-%d")).strip()
    item_id = request.form.get("item_id")
    custom_item_name = request.form.get("custom_item_name", "").strip()
    job_card_no = request.form.get("job_card_no", "").strip()
    unit_measure = request.form.get("unit_measure", "Pcs").strip()
    remarks = request.form.get("remarks", "").strip()

    try:
        qty_completed = int(request.form.get("quantity_completed", 0))
    except (ValueError, TypeError):
        qty_completed = 0

    try:
        rejected_qty = int(request.form.get("rejected_quantity", 0))
    except (ValueError, TypeError):
        rejected_qty = 0

    try:
        rate_per_unit = float(request.form.get("rate_per_unit", 0.0))
    except (ValueError, TypeError):
        rate_per_unit = 0.0

    if not worker_id or not worker_id.isdigit():
        flash("Please select a valid contract worker.", "danger")
        return redirect(request.referrer or url_for("piece_rate.index"))

    if qty_completed <= 0:
        flash("Quantity completed must be greater than 0.", "danger")
        return redirect(request.referrer or url_for("piece_rate.index"))

    if rate_per_unit < 0:
        flash("Rate per piece cannot be negative.", "danger")
        return redirect(request.referrer or url_for("piece_rate.index"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # Determine item name
    item_name = custom_item_name
    final_item_id = None
    if item_id and item_id.isdigit():
        final_item_id = int(item_id)
        cursor.execute("SELECT item_name, unit_measure, default_rate FROM piece_rate_items WHERE id = ?", (final_item_id,))
        item_row = cursor.fetchone()
        if item_row:
            if not item_name:
                item_name = item_row["item_name"]
            if not unit_measure:
                unit_measure = item_row["unit_measure"]
            if rate_per_unit == 0.0:
                rate_per_unit = float(item_row["default_rate"])

    if not item_name:
        item_name = "General Contract Work"

    payable_qty = max(0, qty_completed - rejected_qty)
    total_amount = round(payable_qty * rate_per_unit, 2)

    cursor.execute("""
        INSERT INTO piece_rate_work_entries (
            worker_id, work_date, item_id, item_name, job_card_no,
            rate_per_unit, unit_measure, quantity_completed, rejected_quantity,
            payable_quantity, total_amount, payment_status, remarks
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNPAID', ?)
    """, (
        int(worker_id), work_date, final_item_id, item_name, job_card_no,
        rate_per_unit, unit_measure, qty_completed, rejected_qty,
        payable_qty, total_amount, remarks
    ))
    conn.commit()

    # Worker info for flash
    cursor.execute("SELECT name, worker_code FROM piece_rate_workers WHERE id = ?", (worker_id,))
    w_row = cursor.fetchone()
    conn.close()

    worker_label = f"{w_row['name']} ({w_row['worker_code']})" if w_row else "Worker"
    flash(f"Successfully recorded {payable_qty} {unit_measure} of '{item_name}' for {worker_label} (Total: ₹{total_amount:,.2f}).", "success")
    return redirect(url_for("piece_rate.index", date=work_date, worker_id=worker_id))


@piece_rate_bp.route("/edit-entry/<int:entry_id>", methods=["POST"])
def edit_entry(entry_id):
    """Update quantity, rate, job card, or payment status of an existing entry."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM piece_rate_work_entries WHERE id = ?", (entry_id,))
    entry = cursor.fetchone()
    if not entry:
        conn.close()
        flash("Work entry not found.", "danger")
        return redirect(request.referrer or url_for("piece_rate.index"))

    try:
        qty_completed = int(request.form.get("quantity_completed", entry["quantity_completed"]))
        rejected_qty = int(request.form.get("rejected_quantity", entry["rejected_quantity"]))
        rate_per_unit = float(request.form.get("rate_per_unit", entry["rate_per_unit"]))
    except (ValueError, TypeError):
        flash("Invalid numerical values entered.", "danger")
        conn.close()
        return redirect(request.referrer or url_for("piece_rate.index"))

    job_card_no = request.form.get("job_card_no", entry["job_card_no"]).strip()
    payment_status = request.form.get("payment_status", entry["payment_status"]).strip()
    remarks = request.form.get("remarks", entry["remarks"] or "").strip()

    payable_qty = max(0, qty_completed - rejected_qty)
    total_amount = round(payable_qty * rate_per_unit, 2)

    cursor.execute("""
        UPDATE piece_rate_work_entries
        SET quantity_completed = ?,
            rejected_quantity = ?,
            payable_quantity = ?,
            rate_per_unit = ?,
            total_amount = ?,
            job_card_no = ?,
            payment_status = ?,
            remarks = ?
        WHERE id = ?
    """, (qty_completed, rejected_qty, payable_qty, rate_per_unit, total_amount, job_card_no, payment_status, remarks, entry_id))
    conn.commit()
    conn.close()

    flash("Work entry updated successfully.", "success")
    return redirect(request.referrer or url_for("piece_rate.index"))


@piece_rate_bp.route("/delete-entry/<int:entry_id>", methods=["POST"])
def delete_entry(entry_id):
    """Delete a piece-rate work entry."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM piece_rate_work_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()

    flash("Work entry deleted successfully.", "success")
    return redirect(request.referrer or url_for("piece_rate.index"))


@piece_rate_bp.route("/workers")
def list_workers():
    """
    Contract Workers Directory & Registration Portal.
    Displays all registered piece-rate contract workers with lifetime productivity stats.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    search = request.args.get("q", "").strip()
    category = request.args.get("category", "")
    status = request.args.get("status", "")

    query = """
        SELECT w.*,
               COALESCE(SUM(e.payable_quantity), 0) as total_units_produced,
               COALESCE(SUM(e.total_amount), 0) as total_lifetime_earnings,
               COALESCE(SUM(CASE WHEN e.payment_status = 'UNPAID' THEN e.total_amount ELSE 0 END), 0) as unpaid_balance,
               COUNT(e.id) as total_jobs_count,
               MAX(e.work_date) as last_work_date
        FROM piece_rate_workers w
        LEFT JOIN piece_rate_work_entries e ON w.id = e.worker_id
        WHERE 1=1
    """
    params = []

    if search:
        query += " AND (w.name LIKE ? OR w.worker_code LIKE ? OR w.phone LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])

    if category:
        query += " AND w.department_category = ?"
        params.append(category)

    if status == "ACTIVE":
        query += " AND w.is_active = 1"
    elif status == "INACTIVE":
        query += " AND w.is_active = 0"

    query += " GROUP BY w.id ORDER BY w.is_active DESC, w.name ASC"
    cursor.execute(query, params)
    workers = [dict(r) for r in cursor.fetchall()]

    # Categories for filter
    cursor.execute("SELECT DISTINCT department_category FROM piece_rate_workers WHERE department_category IS NOT NULL AND department_category != '' ORDER BY department_category ASC")
    categories = [r["department_category"] for r in cursor.fetchall()]

    # Suggested next worker code
    cursor.execute("SELECT worker_code FROM piece_rate_workers ORDER BY id DESC LIMIT 1")
    last_code_row = cursor.fetchone()
    next_code = "CW-001"
    if last_code_row and last_code_row["worker_code"]:
        raw = last_code_row["worker_code"]
        if raw.startswith("CW-") and raw[3:].isdigit():
            num = int(raw[3:]) + 1
            next_code = f"CW-{num:03d}"

    conn.close()

    return render_template(
        "piece_rate/workers.html",
        workers=workers,
        categories=categories,
        next_code=next_code,
        search_query=search,
        selected_category=category,
        selected_status=status
    )


@piece_rate_bp.route("/workers/create", methods=["POST"])
def create_worker():
    """Register a new piece-rate contract worker."""
    worker_code = request.form.get("worker_code", "").strip().upper()
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    id_proof = request.form.get("id_proof_number", "").strip()
    department_category = request.form.get("department_category", "Binding").strip()
    payment_mode = request.form.get("payment_mode", "CASH").strip()
    upi_or_bank = request.form.get("upi_or_bank_details", "").strip()
    daily_target_qty = request.form.get("daily_target_qty", "0").strip()
    notes = request.form.get("notes", "").strip()

    if not worker_code or not name:
        flash("Worker Code and Full Name are mandatory.", "danger")
        return redirect(url_for("piece_rate.list_workers"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # Check for duplicate code
    cursor.execute("SELECT id FROM piece_rate_workers WHERE worker_code = ?", (worker_code,))
    if cursor.fetchone():
        conn.close()
        flash(f"Worker code '{worker_code}' already exists. Please choose a unique code.", "danger")
        return redirect(url_for("piece_rate.list_workers"))

    try:
        target = int(daily_target_qty)
    except ValueError:
        target = 0

    cursor.execute("""
        INSERT INTO piece_rate_workers (
            worker_code, name, phone, id_proof_number, department_category,
            payment_mode, upi_or_bank_details, daily_target_qty, is_active, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (worker_code, name, phone, id_proof, department_category, payment_mode, upi_or_bank, target, notes))
    conn.commit()
    conn.close()

    flash(f"Contract Worker '{name}' ({worker_code}) registered successfully.", "success")
    return redirect(url_for("piece_rate.list_workers"))


@piece_rate_bp.route("/workers/edit/<int:worker_id>", methods=["POST"])
def edit_worker(worker_id):
    """Update contract worker registration details."""
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    id_proof = request.form.get("id_proof_number", "").strip()
    department_category = request.form.get("department_category", "").strip()
    payment_mode = request.form.get("payment_mode", "CASH").strip()
    upi_or_bank = request.form.get("upi_or_bank_details", "").strip()
    notes = request.form.get("notes", "").strip()
    is_active = 1 if request.form.get("is_active") == "1" else 0

    try:
        daily_target = int(request.form.get("daily_target_qty", 0))
    except (ValueError, TypeError):
        daily_target = 0

    if not name:
        flash("Worker name is required.", "danger")
        return redirect(url_for("piece_rate.list_workers"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE piece_rate_workers
        SET name = ?, phone = ?, id_proof_number = ?, department_category = ?,
            payment_mode = ?, upi_or_bank_details = ?, daily_target_qty = ?,
            is_active = ?, notes = ?
        WHERE id = ?
    """, (name, phone, id_proof, department_category, payment_mode, upi_or_bank, daily_target, is_active, notes, worker_id))
    conn.commit()
    conn.close()

    flash("Worker details updated successfully.", "success")
    return redirect(url_for("piece_rate.list_workers"))


@piece_rate_bp.route("/items")
def list_items():
    """
    Piece-Rate Items & Rates Master.
    Lists all printing items, units of measurement, and default piece rates.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT i.*,
               COALESCE(SUM(e.payable_quantity), 0) as total_produced_qty,
               COALESCE(SUM(e.total_amount), 0) as total_payout_amount,
               COUNT(e.id) as jobs_logged_count
        FROM piece_rate_items i
        LEFT JOIN piece_rate_work_entries e ON i.id = e.item_id
        GROUP BY i.id
        ORDER BY i.category ASC, i.item_name ASC
    """)
    items = [dict(r) for r in cursor.fetchall()]

    # Suggested next item code
    cursor.execute("SELECT item_code FROM piece_rate_items ORDER BY id DESC LIMIT 1")
    last_row = cursor.fetchone()
    next_code = "ITEM-001"
    if last_row and last_row["item_code"] and last_row["item_code"].startswith("ITEM-"):
        raw_num = last_row["item_code"][5:]
        if raw_num.isdigit():
            next_code = f"ITEM-{int(raw_num) + 1:03d}"

    conn.close()

    return render_template(
        "piece_rate/items.html",
        items=items,
        next_code=next_code
    )


@piece_rate_bp.route("/items/create", methods=["POST"])
def create_item():
    """Add a new piece-rate item to the master catalog."""
    item_code = request.form.get("item_code", "").strip().upper()
    item_name = request.form.get("item_name", "").strip()
    category = request.form.get("category", "General").strip()
    unit_measure = request.form.get("unit_measure", "Pcs").strip()

    try:
        default_rate = float(request.form.get("default_rate", 0.0))
    except (ValueError, TypeError):
        default_rate = 0.0

    if not item_code or not item_name:
        flash("Item Code and Item Name are mandatory.", "danger")
        return redirect(url_for("piece_rate.list_items"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM piece_rate_items WHERE item_code = ?", (item_code,))
    if cursor.fetchone():
        conn.close()
        flash(f"Item code '{item_code}' already exists.", "danger")
        return redirect(url_for("piece_rate.list_items"))

    cursor.execute("""
        INSERT INTO piece_rate_items (item_code, item_name, category, unit_measure, default_rate, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (item_code, item_name, category, unit_measure, default_rate))
    conn.commit()
    conn.close()

    flash(f"Piece-Rate Item '{item_name}' (₹{default_rate:.2f} / {unit_measure}) added successfully.", "success")
    return redirect(url_for("piece_rate.list_items"))


@piece_rate_bp.route("/items/edit/<int:item_id>", methods=["POST"])
def edit_item(item_id):
    """Edit piece-rate item details and default rate."""
    item_name = request.form.get("item_name", "").strip()
    category = request.form.get("category", "").strip()
    unit_measure = request.form.get("unit_measure", "Pcs").strip()
    is_active = 1 if request.form.get("is_active") == "1" else 0

    try:
        default_rate = float(request.form.get("default_rate", 0.0))
    except (ValueError, TypeError):
        default_rate = 0.0

    if not item_name:
        flash("Item name cannot be empty.", "danger")
        return redirect(url_for("piece_rate.list_items"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE piece_rate_items
        SET item_name = ?, category = ?, unit_measure = ?, default_rate = ?, is_active = ?
        WHERE id = ?
    """, (item_name, category, unit_measure, default_rate, is_active, item_id))
    conn.commit()
    conn.close()

    flash("Item updated successfully.", "success")
    return redirect(url_for("piece_rate.list_items"))


@piece_rate_bp.route("/worker/<int:worker_id>/statement")
def worker_statement(worker_id):
    """
    Item-Wise Production Statement & Printable Wage Slip / Voucher.
    Generates an itemized breakdown with printable layout for cash/UPI settlement.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM piece_rate_workers WHERE id = ?", (worker_id,))
    worker = cursor.fetchone()
    if not worker:
        conn.close()
        flash("Worker not found.", "danger")
        return redirect(url_for("piece_rate.list_workers"))

    worker = dict(worker)

    # Date range filters (default to current month or past 30 days)
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    status = request.args.get("status", "ALL")

    if not start_date or not end_date:
        today = date.today()
        # Default to 1st of current month to today
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

    # Query entries for this worker
    query = """
        SELECT e.*
        FROM piece_rate_work_entries e
        WHERE e.worker_id = ?
    """
    params = [worker_id]

    if start_date:
        query += " AND e.work_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND e.work_date <= ?"
        params.append(end_date)
    if status and status in ("UNPAID", "PAID"):
        query += " AND e.payment_status = ?"
        params.append(status)

    query += " ORDER BY e.work_date ASC, e.id ASC"
    cursor.execute(query, params)
    entries = [dict(r) for r in cursor.fetchall()]

    # Item-wise grouped summary
    item_summary = {}
    total_gross_qty = 0
    total_rejected_qty = 0
    total_payable_qty = 0
    total_amount = 0.0
    unpaid_amount = 0.0

    for e in entries:
        k = (e["item_name"], e["unit_measure"])
        if k not in item_summary:
            item_summary[k] = {
                "item_name": e["item_name"],
                "unit_measure": e["unit_measure"],
                "rates": set(),
                "gross_qty": 0,
                "rejected_qty": 0,
                "payable_qty": 0,
                "total_amount": 0.0,
                "jobs_count": 0
            }
        item_summary[k]["rates"].add(f"₹{e['rate_per_unit']:.2f}")
        item_summary[k]["gross_qty"] += e["quantity_completed"]
        item_summary[k]["rejected_qty"] += e["rejected_quantity"]
        item_summary[k]["payable_qty"] += e["payable_quantity"]
        item_summary[k]["total_amount"] += e["total_amount"]
        item_summary[k]["jobs_count"] += 1

        total_gross_qty += e["quantity_completed"]
        total_rejected_qty += e["rejected_quantity"]
        total_payable_qty += e["payable_quantity"]
        total_amount += e["total_amount"]
        if e["payment_status"] == "UNPAID":
            unpaid_amount += e["total_amount"]

    for item in item_summary.values():
        item["rates_display"] = ", ".join(sorted(item["rates"]))

    # Company settings for statement header
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company_row = cursor.fetchone()
    company_info = dict(company_row) if company_row else {"company_name": "Vasantham Printers", "currency_symbol": "₹"}

    conn.close()

    return render_template(
        "piece_rate/statement.html",
        worker=worker,
        entries=entries,
        item_summary=list(item_summary.values()),
        total_gross_qty=total_gross_qty,
        total_rejected_qty=total_rejected_qty,
        total_payable_qty=total_payable_qty,
        total_amount=round(total_amount, 2),
        unpaid_amount=round(unpaid_amount, 2),
        start_date=start_date,
        end_date=end_date,
        status=status,
        company_info=company_info
    )


@piece_rate_bp.route("/worker/<int:worker_id>/settle-payment", methods=["POST"])
def settle_payment(worker_id):
    """
    Marks unpaid piece-rate work entries within a date range as PAID.
    Generates a settlement payout reference (voucher ID).
    """
    start_date = request.form.get("start_date")
    end_date = request.form.get("end_date")
    payout_ref = request.form.get("payout_reference", "").strip()
    payment_mode = request.form.get("payment_mode", "CASH").strip()

    if not payout_ref:
        payout_ref = f"VOUCHER-{datetime.now().strftime('%y%m%d')}-{worker_id}"

    payout_date = date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Find unpaid entries
    query = """
        SELECT id, total_amount FROM piece_rate_work_entries 
        WHERE worker_id = ? AND payment_status = 'UNPAID'
    """
    params = [worker_id]
    if start_date:
        query += " AND work_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND work_date <= ?"
        params.append(end_date)

    cursor.execute(query, params)
    unpaid_rows = cursor.fetchall()

    if not unpaid_rows:
        conn.close()
        flash("No unpaid entries found in the selected range to settle.", "info")
        return redirect(url_for("piece_rate.worker_statement", worker_id=worker_id, start_date=start_date, end_date=end_date))

    settled_sum = sum(r["total_amount"] for r in unpaid_rows)
    entry_ids = [r["id"] for r in unpaid_rows]

    cursor.execute(f"""
        UPDATE piece_rate_work_entries
        SET payment_status = 'PAID',
            payout_date = ?,
            payout_reference = ?
        WHERE id IN ({','.join(['?'] * len(entry_ids))})
    """, [payout_date, f"{payout_ref} ({payment_mode})"] + entry_ids)
    conn.commit()
    conn.close()

    flash(f"Payment settled! ₹{settled_sum:,.2f} marked as PAID for {len(entry_ids)} work entries (Ref: {payout_ref}).", "success")
    return redirect(url_for("piece_rate.worker_statement", worker_id=worker_id, start_date=start_date, end_date=end_date))


@piece_rate_bp.route("/export-csv")
def export_csv():
    """Export piece-rate work entries to CSV."""
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT e.work_date, w.worker_code, w.name as worker_name, w.department_category,
               e.job_card_no, e.item_name, e.unit_measure, e.rate_per_unit,
               e.quantity_completed, e.rejected_quantity, e.payable_quantity,
               e.total_amount, e.payment_status, e.payout_date, e.payout_reference, e.remarks
        FROM piece_rate_work_entries e
        JOIN piece_rate_workers w ON e.worker_id = w.id
        ORDER BY e.work_date DESC, e.id DESC
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Work Date", "Worker Code", "Worker Name", "Category", "Job Card No",
        "Item Name", "Unit", "Rate (Rs)", "Qty Completed", "Rejected Qty",
        "Payable Qty", "Total Amount (Rs)", "Payment Status", "Payout Date",
        "Payout Reference", "Remarks"
    ])

    for r in rows:
        writer.writerow([
            r["work_date"], r["worker_code"], r["worker_name"], r["department_category"],
            r["job_card_no"] or "", r["item_name"], r["unit_measure"], f"{r['rate_per_unit']:.2f}",
            r["quantity_completed"], r["rejected_quantity"], r["payable_quantity"],
            f"{r['total_amount']:.2f}", r["payment_status"], r["payout_date"] or "",
            r["payout_reference"] or "", r["remarks"] or ""
        ])

    csv_data = output.getvalue()
    filename = f"Vasantham_Printers_Piece_Rate_Production_{date.today().strftime('%Y%m%d')}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def calculate_weekly_contractor_payroll(cursor, week_start, week_end, worker_id=None):
    """
    Aggregates piece-rate work entries between week_start and week_end.
    Returns per-worker weekly salary summaries and overall summary.
    """
    query = """
        SELECT e.*, w.worker_code, w.name as worker_name, w.phone as worker_phone,
               w.department_category, w.payment_mode, w.upi_or_bank_details
        FROM piece_rate_work_entries e
        JOIN piece_rate_workers w ON e.worker_id = w.id
        WHERE e.work_date >= ? AND e.work_date <= ?
    """
    params = [week_start, week_end]
    if worker_id:
        query += " AND e.worker_id = ?"
        params.append(worker_id)
    query += " ORDER BY w.name ASC, e.work_date ASC, e.id ASC"

    cursor.execute(query, params)
    entries = [dict(r) for r in cursor.fetchall()]

    workers_map = {}
    for e in entries:
        wid = e["worker_id"]
        if wid not in workers_map:
            workers_map[wid] = {
                "worker_id": wid,
                "worker_code": e["worker_code"],
                "worker_name": e["worker_name"],
                "worker_phone": e["worker_phone"],
                "department_category": e["department_category"],
                "payment_mode": e["payment_mode"],
                "upi_or_bank_details": e["upi_or_bank_details"],
                "work_dates": set(),
                "total_gross_qty": 0,
                "total_rejected_qty": 0,
                "total_payable_qty": 0,
                "total_wages": 0.0,
                "paid_wages": 0.0,
                "unpaid_wages": 0.0,
                "items_map": {},
                "daily_map": {},
                "entries": []
            }
        
        w_data = workers_map[wid]
        w_data["work_dates"].add(e["work_date"])
        w_data["total_gross_qty"] += e["quantity_completed"]
        w_data["total_rejected_qty"] += e["rejected_quantity"]
        w_data["total_payable_qty"] += e["payable_quantity"]
        w_data["total_wages"] += e["total_amount"]
        w_data["entries"].append(e)

        if e["payment_status"] == "PAID":
            w_data["paid_wages"] += e["total_amount"]
        else:
            w_data["unpaid_wages"] += e["total_amount"]

        # Item-wise aggregation
        item_key = (e["item_name"], e["unit_measure"], e["rate_per_unit"])
        if item_key not in w_data["items_map"]:
            w_data["items_map"][item_key] = {
                "item_name": e["item_name"],
                "unit_measure": e["unit_measure"],
                "rate": e["rate_per_unit"],
                "gross_qty": 0,
                "rejected_qty": 0,
                "payable_qty": 0,
                "total_amount": 0.0
            }
        w_data["items_map"][item_key]["gross_qty"] += e["quantity_completed"]
        w_data["items_map"][item_key]["rejected_qty"] += e["rejected_quantity"]
        w_data["items_map"][item_key]["payable_qty"] += e["payable_quantity"]
        w_data["items_map"][item_key]["total_amount"] += e["total_amount"]

        # Daily aggregation
        d = e["work_date"]
        if d not in w_data["daily_map"]:
            w_data["daily_map"][d] = {"date": d, "payable_qty": 0, "amount": 0.0}
        w_data["daily_map"][d]["payable_qty"] += e["payable_quantity"]
        w_data["daily_map"][d]["amount"] += e["total_amount"]

    # Finalize worker records
    worker_summaries = []
    grand_total_wages = 0.0
    grand_total_pcs = 0
    grand_unpaid = 0.0
    grand_paid = 0.0

    for wid, w_data in workers_map.items():
        w_data["days_worked"] = len(w_data["work_dates"])
        w_data["sorted_dates"] = sorted(list(w_data["work_dates"]))
        w_data["items_breakdown"] = list(w_data["items_map"].values())
        w_data["daily_breakdown"] = sorted(list(w_data["daily_map"].values()), key=lambda x: x["date"])
        w_data["total_wages"] = round(w_data["total_wages"], 2)
        w_data["paid_wages"] = round(w_data["paid_wages"], 2)
        w_data["unpaid_wages"] = round(w_data["unpaid_wages"], 2)
        
        if w_data["unpaid_wages"] == 0:
            w_data["status"] = "PAID"
        elif w_data["paid_wages"] == 0:
            w_data["status"] = "UNPAID"
        else:
            w_data["status"] = "PARTIAL"

        grand_total_wages += w_data["total_wages"]
        grand_total_pcs += w_data["total_payable_qty"]
        grand_unpaid += w_data["unpaid_wages"]
        grand_paid += w_data["paid_wages"]

        worker_summaries.append(w_data)

    totals = {
        "grand_total_wages": round(grand_total_wages, 2),
        "grand_total_pcs": grand_total_pcs,
        "grand_unpaid": round(grand_unpaid, 2),
        "grand_paid": round(grand_paid, 2),
        "active_contractors_count": len(worker_summaries),
        "total_entries_count": len(entries)
    }

    return worker_summaries, totals


@piece_rate_bp.route("/weekly-salary")
def weekly_salary():
    """
    Weekly Contractor Salary Calculation Dashboard.
    Aggregates piece-rate wages per contractor per week.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Determine default week range (Monday to Sunday)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    # Check if there is data in previous weeks if today's week is sparse
    week_start = request.args.get("start_date")
    week_end = request.args.get("end_date")

    if not week_start or not week_end:
        # Check latest entry date in DB
        cursor.execute("SELECT MAX(work_date) as max_d FROM piece_rate_work_entries")
        row = cursor.fetchone()
        if row and row["max_d"]:
            try:
                latest_d = datetime.strptime(row["max_d"], "%Y-%m-%d").date()
                start_of_week = latest_d - timedelta(days=latest_d.weekday())
                end_of_week = start_of_week + timedelta(days=6)
            except Exception:
                pass
        week_start = start_of_week.strftime("%Y-%m-%d")
        week_end = end_of_week.strftime("%Y-%m-%d")

    worker_summaries, totals = calculate_weekly_contractor_payroll(cursor, week_start, week_end)

    # Calculate ISO week number and readable label
    try:
        dt_start = datetime.strptime(week_start, "%Y-%m-%d")
        week_num = dt_start.isocalendar()[1]
        week_label = f"Week {week_num} ({dt_start.strftime('%d %b')} - {datetime.strptime(week_end, '%Y-%m-%d').strftime('%d %b %Y')})"
    except Exception:
        week_num = 1
        week_label = f"{week_start} to {week_end}"

    # Company settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = cursor.fetchone()
    company_info = dict(settings) if settings else {"company_name": "Vasantham Printers", "currency_symbol": "₹"}

    conn.close()

    return render_template(
        "piece_rate/weekly_salary.html",
        workers=worker_summaries,
        totals=totals,
        week_start=week_start,
        week_end=week_end,
        week_label=week_label,
        week_num=week_num,
        company_info=company_info
    )


@piece_rate_bp.route("/weekly-salary/settle-all", methods=["POST"])
def settle_all_week():
    """
    Batch settles all unpaid piece-rate work entries within the selected week.
    """
    week_start = request.form.get("week_start") or request.form.get("start_date") or request.form.get("start")
    week_end = request.form.get("week_end") or request.form.get("end_date") or request.form.get("end")
    payout_ref = request.form.get("payout_reference", "").strip()
    payment_mode = request.form.get("payment_mode", "CASH").strip()

    if not week_start or not week_end:
        flash("Week date range is missing.", "danger")
        return redirect(url_for("piece_rate.weekly_salary"))

    if not payout_ref:
        payout_ref = f"WEEKLY-PAYOUT-{datetime.now().strftime('%y%m%d-%H%M')}"

    payout_date = date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, total_amount FROM piece_rate_work_entries
        WHERE work_date >= ? AND work_date <= ? AND payment_status = 'UNPAID'
    """, (week_start, week_end))
    unpaid_entries = cursor.fetchall()

    if not unpaid_entries:
        conn.close()
        flash("No unpaid entries found for this week to settle.", "info")
        return redirect(url_for("piece_rate.weekly_salary", start_date=week_start, end_date=week_end))

    settled_sum = sum(r["total_amount"] for r in unpaid_entries)
    entry_ids = [r["id"] for r in unpaid_entries]

    cursor.execute(f"""
        UPDATE piece_rate_work_entries
        SET payment_status = 'PAID',
            payout_date = ?,
            payout_reference = ?
        WHERE id IN ({','.join(['?'] * len(entry_ids))})
    """, [payout_date, f"{payout_ref} ({payment_mode})"] + entry_ids)
    conn.commit()
    conn.close()

    flash(f"Weekly settlement completed! ₹{settled_sum:,.2f} marked as PAID across {len(entry_ids)} work entries (Ref: {payout_ref}).", "success")
    return redirect(url_for("piece_rate.weekly_salary", start_date=week_start, end_date=week_end))


@piece_rate_bp.route("/worker/<int:worker_id>/weekly-slip")
def weekly_slip(worker_id):
    """
    Dedicated printable weekly wage slip for a specific contractor worker.
    """
    week_start = request.args.get("start")
    week_end = request.args.get("end")

    if not week_start or not week_end:
        today = date.today()
        start_of_week = today - timedelta(days=today.weekday())
        week_start = start_of_week.strftime("%Y-%m-%d")
        week_end = (start_of_week + timedelta(days=6)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    worker_summaries, totals = calculate_weekly_contractor_payroll(cursor, week_start, week_end, worker_id=worker_id)
    if not worker_summaries:
        # Worker might have 0 entries for this week, fetch worker profile directly
        cursor.execute("SELECT * FROM piece_rate_workers WHERE id = ?", (worker_id,))
        w_profile = cursor.fetchone()
        if not w_profile:
            conn.close()
            flash("Worker not found.", "danger")
            return redirect(url_for("piece_rate.weekly_salary"))
        worker_data = {
            "worker_id": worker_id,
            "worker_code": w_profile["worker_code"],
            "worker_name": w_profile["name"],
            "worker_phone": w_profile["phone"],
            "department_category": w_profile["department_category"],
            "payment_mode": w_profile["payment_mode"],
            "upi_or_bank_details": w_profile["upi_or_bank_details"],
            "days_worked": 0,
            "sorted_dates": [],
            "total_gross_qty": 0,
            "total_rejected_qty": 0,
            "total_payable_qty": 0,
            "total_wages": 0.0,
            "paid_wages": 0.0,
            "unpaid_wages": 0.0,
            "status": "NO_WORK",
            "items_breakdown": [],
            "daily_breakdown": [],
            "entries": []
        }
    else:
        worker_data = worker_summaries[0]

    # Week label
    try:
        dt_start = datetime.strptime(week_start, "%Y-%m-%d")
        week_num = dt_start.isocalendar()[1]
        week_label = f"Week {week_num} ({dt_start.strftime('%d %b')} - {datetime.strptime(week_end, '%Y-%m-%d').strftime('%d %b %Y')})"
    except Exception:
        week_num = 1
        week_label = f"{week_start} to {week_end}"

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company_info = dict(cursor.fetchone() or {"company_name": "Vasantham Printers", "currency_symbol": "₹"})
    conn.close()

    return render_template(
        "piece_rate/weekly_slip.html",
        worker=worker_data,
        week_start=week_start,
        week_end=week_end,
        week_label=week_label,
        week_num=week_num,
        company_info=company_info
    )


@piece_rate_bp.route("/weekly-slips-batch")
def weekly_slips_batch():
    """
    Batch printable wage slips for all contractor workers who worked during the week.
    """
    week_start = request.args.get("start")
    week_end = request.args.get("end")

    if not week_start or not week_end:
        today = date.today()
        start_of_week = today - timedelta(days=today.weekday())
        week_start = start_of_week.strftime("%Y-%m-%d")
        week_end = (start_of_week + timedelta(days=6)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    workers, totals = calculate_weekly_contractor_payroll(cursor, week_start, week_end)

    try:
        dt_start = datetime.strptime(week_start, "%Y-%m-%d")
        week_num = dt_start.isocalendar()[1]
        week_label = f"Week {week_num} ({dt_start.strftime('%d %b')} - {datetime.strptime(week_end, '%Y-%m-%d').strftime('%d %b %Y')})"
    except Exception:
        week_num = 1
        week_label = f"{week_start} to {week_end}"

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company_info = dict(cursor.fetchone() or {"company_name": "Vasantham Printers", "currency_symbol": "₹"})
    conn.close()

    return render_template(
        "piece_rate/weekly_slips_batch.html",
        workers=workers,
        totals=totals,
        week_start=week_start,
        week_end=week_end,
        week_label=week_label,
        week_num=week_num,
        company_info=company_info
    )


@piece_rate_bp.route("/weekly-salary/export-csv")
def export_weekly_salary_csv():
    """Export weekly contractor salary calculation report to CSV."""
    week_start = request.args.get("start")
    week_end = request.args.get("end")

    if not week_start or not week_end:
        today = date.today()
        start_of_week = today - timedelta(days=today.weekday())
        week_start = start_of_week.strftime("%Y-%m-%d")
        week_end = (start_of_week + timedelta(days=6)).strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    workers, totals = calculate_weekly_contractor_payroll(cursor, week_start, week_end)
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Worker Code", "Worker Name", "Trade Category", "Week Start", "Week End",
        "Days Worked", "Total Pieces Completed", "Rejected Pieces", "Payable Pieces",
        "Total Weekly Wages (Rs)", "Paid Wages (Rs)", "Unpaid Balance (Rs)",
        "Payment Mode", "Bank/UPI Account Details", "Status"
    ])

    for w in workers:
        writer.writerow([
            w["worker_code"], w["worker_name"], w["department_category"],
            week_start, week_end, w["days_worked"], w["total_gross_qty"],
            w["total_rejected_qty"], w["total_payable_qty"],
            f"{w['total_wages']:.2f}", f"{w['paid_wages']:.2f}", f"{w['unpaid_wages']:.2f}",
            w["payment_mode"], w["upi_or_bank_details"] or "", w["status"]
        ])

    csv_data = output.getvalue()
    filename = f"Vasantham_Printers_Contractor_Weekly_Payroll_{week_start}_to_{week_end}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

