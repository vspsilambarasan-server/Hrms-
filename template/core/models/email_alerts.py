import smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from database import get_db_connection

def get_missed_morning_punch_employees(target_date=None):
    """
    Finds active employees who have not clocked in for their morning shift.
    """
    if not target_date:
        target_date = date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Query active employees whose shift starts in morning (e.g. before 12:00 PM)
    # and who have no punch_in recorded for target_date
    cursor.execute("""
        SELECT e.id, e.emp_no, e.first_name, e.last_name, e.phone, e.designation,
               s.name as shift_name, s.start_time, s.end_time,
               a.punch_in, a.status
        FROM employees e
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        LEFT JOIN attendance_records a ON e.id = a.employee_id AND a.date = ?
        WHERE e.is_active = 1
          AND (e.is_admin IS NULL OR e.is_admin = 0)
          AND (a.punch_in IS NULL OR a.punch_in = '')
          AND (a.status IS NULL OR a.status = 'ABSENT')
        ORDER BY s.start_time ASC, e.emp_no ASC
    """, (target_date,))

    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def send_missed_morning_punch_alert(target_date=None):
    """
    Sends an email alert to management with the list of employees who missed morning punch.
    """
    if not target_date:
        target_date = date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    s = dict(cursor.fetchone() or {})
    conn.close()

    recipient = s.get("alert_recipient_email") or s.get("email")
    if not recipient:
        return {"success": False, "error": "No alert recipient email configured in Company Settings."}

    missed = get_missed_morning_punch_employees(target_date)
    if not missed:
        return {"success": True, "message": f"All employees have punched in for {target_date}. No alert needed.", "missed_count": 0}

    smtp_host = s.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(s.get("smtp_port", 587))
    smtp_user = s.get("smtp_user", "")
    smtp_password = s.get("smtp_password", "")

    if not smtp_user or not smtp_password:
        return {
            "success": False,
            "error": f"{len(missed)} employees missed morning punch, but SMTP credentials are not configured in Company Settings.",
            "missed_count": len(missed),
            "missed_employees": missed
        }

    company_name = s.get("company_name", "Vasantham Printers")

    # Construct HTML table
    rows_html = ""
    for idx, emp in enumerate(missed, 1):
        rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 8px 12px; font-size: 12px; text-align: center;">{idx}</td>
            <td style="padding: 8px 12px; font-size: 12px; font-family: monospace; font-weight: bold; color: #1d4ed8;">{emp['emp_no']}</td>
            <td style="padding: 8px 12px; font-size: 12px; font-weight: bold; color: #0f172a;">{emp['first_name']} {emp['last_name']}</td>
            <td style="padding: 8px 12px; font-size: 12px; color: #475569;">{emp.get('designation', 'Staff')}</td>
            <td style="padding: 8px 12px; font-size: 12px; color: #475569;">{emp.get('shift_name', 'Morning Shift')} ({emp.get('start_time', '09:00')})</td>
            <td style="padding: 8px 12px; font-size: 12px; font-family: monospace; color: #e11d48; font-weight: bold;">NO PUNCH</td>
        </tr>
        """

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px; color: #1e293b;">
        <div style="max-width: 650px; margin: 0 auto; background: white; border-radius: 12px; border: 1px solid #cbd5e1; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
            <div style="background: #0f172a; color: white; padding: 18px 24px; border-bottom: 3px solid #e11d48;">
                <h2 style="margin: 0; font-size: 18px; text-transform: uppercase;">{company_name}</h2>
                <p style="margin: 4px 0 0 0; font-size: 12px; color: #94a3b8;">Daily Attendance Alert • Morning Shift Clock-In Missed</p>
            </div>
            
            <div style="padding: 24px;">
                <div style="background: #fff1f2; border: 1px solid #fecdd3; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;">
                    <span style="font-size: 13px; font-weight: bold; color: #be123c;">
                        Attention: {len(missed)} staff member(s) have NOT punched in for today ({target_date}).
                    </span>
                    <p style="margin: 4px 0 0 0; font-size: 11px; color: #9f1239;">
                        The morning grace period has expired without biometric terminal scan.
                    </p>
                </div>

                <table style="width: 100%; border-collapse: collapse; text-align: left;">
                    <thead>
                        <tr style="background: #f1f5f9; border-bottom: 2px solid #cbd5e1; font-size: 11px; text-transform: uppercase; color: #475569;">
                            <th style="padding: 8px 12px; text-align: center;">#</th>
                            <th style="padding: 8px 12px;">Emp No</th>
                            <th style="padding: 8px 12px;">Staff Name</th>
                            <th style="padding: 8px 12px;">Designation</th>
                            <th style="padding: 8px 12px;">Shift (Start)</th>
                            <th style="padding: 8px 12px;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>

                <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #64748b; text-align: center;">
                    <p style="margin: 0;">Automated alert generated by {company_name} HRMS Biometric Engine.</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[{company_name}] ALERT: {len(missed)} Staff Missed Morning Clock-IN ({target_date})"
        msg["From"] = f"{company_name} HRMS <{smtp_user}>"
        msg["To"] = recipient

        msg.attach(MIMEText(body_html, "html"))

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
        if s.get("smtp_use_tls", 1):
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], msg.as_string())
        server.quit()

        return {
            "success": True,
            "message": f"Alert successfully sent to {recipient} for {len(missed)} staff members.",
            "missed_count": len(missed)
        }
    except Exception as e:
        return {"success": False, "error": str(e), "missed_count": len(missed)}


def send_missing_punch_manager_notification(target_date=None, server_base_url="http://127.0.0.1:5000"):
    """
    Sends an automated email notification to managers summarizing identified
    missing punch gaps with suggested corrections and direct 1-click review link.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    s = dict(cursor.fetchone() or {})

    # Fetch pending missing punch alerts
    query = """
        SELECT m.*, e.emp_no, e.first_name, e.last_name, e.designation, d.name as dept_name
        FROM missing_punch_alerts m
        JOIN employees e ON m.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE m.status = 'PENDING'
    """
    params = []
    if target_date:
        query += " AND m.date = ?"
        params.append(target_date)

    query += " ORDER BY m.date DESC, m.gap_type ASC, e.emp_no ASC"
    cursor.execute(query, params)
    alerts = [dict(r) for r in cursor.fetchall()]
    conn.close()

    recipient = s.get("alert_recipient_email") or s.get("email")
    if not recipient:
        return {"success": False, "error": "No alert recipient email configured in Company Settings."}

    if not alerts:
        return {"success": True, "message": "No pending missing punch gaps detected. All shift patterns satisfied!", "count": 0}

    smtp_host = s.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(s.get("smtp_port", 587))
    smtp_user = s.get("smtp_user", "")
    smtp_password = s.get("smtp_password", "")

    if not smtp_user or not smtp_password:
        return {
            "success": False,
            "error": f"{len(alerts)} missing punch gap(s) identified, but SMTP credentials are not configured in Company Settings.",
            "count": len(alerts),
            "alerts": alerts
        }

    company_name = s.get("company_name", "Vasantham Printers")
    approval_url = f"{server_base_url}/attendance/missing-punches"

    # Build HTML rows
    rows_html = ""
    for idx, a in enumerate(alerts, 1):
        gap_label = a["gap_type"].replace("_", " ").title()
        badge_bg = "#fef2f2" if "CLOCK" in a["gap_type"] else "#fefce8"
        badge_color = "#b91c1c" if "CLOCK" in a["gap_type"] else "#854d0e"
        badge_border = "#fecaca" if "CLOCK" in a["gap_type"] else "#fef08a"

        correction_badge = f"""
        <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-family: monospace; font-size: 11px; background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0;">
            {a['suggested_punch_type']} @ {a['suggested_timestamp'][11:]}
        </span>
        """

        rows_html += f"""
        <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 12px; font-size: 12px; text-align: center; color: #64748b;">{idx}</td>
            <td style="padding: 10px 12px; font-size: 12px; font-weight: bold; font-family: monospace; color: #334155;">{a['date']}</td>
            <td style="padding: 10px 12px; font-size: 12px; font-family: monospace; font-weight: bold; color: #1d4ed8;">{a['emp_no']}</td>
            <td style="padding: 10px 12px; font-size: 12px; font-weight: bold; color: #0f172a;">{a['first_name']} {a['last_name']}</td>
            <td style="padding: 10px 12px; font-size: 12px; color: #475569;">{a.get('dept_name', 'Plant')}</td>
            <td style="padding: 10px 12px; font-size: 11px;">
                <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; background: {badge_bg}; color: {badge_color}; border: 1px solid {badge_border};">
                    {gap_label}
                </span>
            </td>
            <td style="padding: 10px 12px;">{correction_badge}</td>
            <td style="padding: 10px 12px; font-size: 11px; color: #64748b;">{a.get('description', '')}</td>
        </tr>
        """

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; padding: 24px; color: #1e293b; margin: 0;">
        <div style="max-width: 820px; margin: 0 auto; background: white; border-radius: 12px; border: 1px solid #cbd5e1; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
            <div style="background: #0f172a; color: white; padding: 20px 28px; border-bottom: 4px solid #f59e0b;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <h1 style="margin: 0; font-size: 20px; text-transform: uppercase; letter-spacing: 0.5px;">{company_name}</h1>
                        <p style="margin: 4px 0 0 0; font-size: 13px; color: #94a3b8;">Automated Biometric Shift Gap Analysis & Correction Alert</p>
                    </div>
                </div>
            </div>
            
            <div style="padding: 24px 28px;">
                <div style="background: #fffbeb; border: 1px solid #fef3c7; border-left: 4px solid #f59e0b; border-radius: 8px; padding: 14px 18px; margin-bottom: 22px;">
                    <strong style="font-size: 14px; color: #92400e; display: block;">
                        Manager Action Required: {len(alerts)} Missing Punch Gap(s) Detected
                    </strong>
                    <p style="margin: 4px 0 0 0; font-size: 12px; color: #b45309;">
                        The system compared individual expected shift schedules against biometric logs and flagged the following gaps. You can review and approve timestamped corrections with a single click.
                    </p>
                </div>

                <table style="width: 100%; border-collapse: collapse; text-align: left;">
                    <thead>
                        <tr style="background: #f1f5f9; border-bottom: 2px solid #cbd5e1; font-size: 11px; text-transform: uppercase; color: #475569; letter-spacing: 0.5px;">
                            <th style="padding: 10px 12px; text-align: center;">#</th>
                            <th style="padding: 10px 12px;">Date</th>
                            <th style="padding: 10px 12px;">Emp No</th>
                            <th style="padding: 10px 12px;">Staff Name</th>
                            <th style="padding: 10px 12px;">Dept</th>
                            <th style="padding: 10px 12px;">Gap Type</th>
                            <th style="padding: 10px 12px;">Suggested Correction</th>
                            <th style="padding: 10px 12px;">Notes</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>

                <div style="margin-top: 26px; text-align: center; padding: 18px 0; background: #f8fafc; border-radius: 8px; border: 1px dashed #cbd5e1;">
                    <a href="{approval_url}" style="display: inline-block; background: #16a34a; color: white; padding: 12px 28px; border-radius: 8px; font-weight: bold; font-size: 14px; text-decoration: none; box-shadow: 0 2px 4px rgba(22, 163, 74, 0.3);">
                        ✓ Review & 1-Click Approve Corrections in HRMS &raquo;
                    </a>
                    <p style="margin: 8px 0 0 0; font-size: 11px; color: #64748b;">
                        Or open directly in browser: <span style="font-family: monospace; color: #2563eb;">{approval_url}</span>
                    </p>
                </div>

                <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #94a3b8; text-align: center;">
                    <p style="margin: 0;">Automated alert generated by {company_name} HRMS Biometric Engine.</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[{company_name}] ATTENDANCE ALERT: {len(alerts)} Missing Punch Gap(s) Need Approval"
        msg["From"] = f"{company_name} HRMS <{smtp_user}>"
        msg["To"] = recipient

        msg.attach(MIMEText(body_html, "html"))

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
        if s.get("smtp_use_tls", 1):
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], msg.as_string())
        server.quit()

        return {
            "success": True,
            "message": f"Notification successfully sent to {recipient} for {len(alerts)} missing punch gap(s).",
            "count": len(alerts)
        }
    except Exception as e:
        return {"success": False, "error": str(e), "count": len(alerts)}

