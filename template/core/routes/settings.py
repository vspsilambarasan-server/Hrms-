import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from database import get_db_connection
from routes.auth import login_required

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

@settings_bp.route("/company", methods=["GET", "POST"])
@login_required
def company_profile():
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip() or "Vasantham Printers"
        address = request.form.get("address", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        gstin = request.form.get("gstin", "").strip()
        factory_license_no = request.form.get("factory_license_no", "").strip()
        supervisor_name = request.form.get("supervisor_name", "").strip()

        # SMTP & Alert settings
        smtp_host = request.form.get("smtp_host", "").strip()
        smtp_port = int(request.form.get("smtp_port") or 587)
        smtp_user = request.form.get("smtp_user", "").strip()
        smtp_password = request.form.get("smtp_password", "").strip()
        smtp_use_tls = 1 if request.form.get("smtp_use_tls") == "1" else 0
        alert_recipient_email = request.form.get("alert_recipient_email", "").strip()

        if not alert_recipient_email and email:
            alert_recipient_email = email

        cursor.execute("SELECT id FROM company_settings ORDER BY id ASC LIMIT 1")
        existing_row = cursor.fetchone()
        if existing_row:
            cursor.execute("""
                UPDATE company_settings
                SET company_name = ?,
                    address = ?,
                    phone = ?,
                    email = ?,
                    gstin = ?,
                    factory_license_no = ?,
                    supervisor_name = ?,
                    smtp_host = ?,
                    smtp_port = ?,
                    smtp_user = ?,
                    smtp_password = ?,
                    smtp_use_tls = ?,
                    alert_recipient_email = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                company_name, address, phone, email, gstin, factory_license_no,
                supervisor_name, smtp_host, smtp_port, smtp_user, smtp_password,
                smtp_use_tls, alert_recipient_email, existing_row["id"]
            ))
        else:
            cursor.execute("""
                INSERT INTO company_settings (
                    company_name, address, phone, email, gstin, factory_license_no,
                    supervisor_name, smtp_host, smtp_port, smtp_user, smtp_password,
                    smtp_use_tls, alert_recipient_email
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                company_name, address, phone, email, gstin, factory_license_no,
                supervisor_name, smtp_host, smtp_port, smtp_user, smtp_password,
                smtp_use_tls, alert_recipient_email
            ))
        conn.commit()
        conn.close()

        flash("Company Profile, Address & Alert Settings updated successfully!", "success")
        return redirect(url_for("settings.company_profile"))

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = dict(cursor.fetchone() or {})
    conn.close()

    return render_template("settings/company.html", settings=settings)

@settings_bp.route("/test-email", methods=["POST"])
@login_required
def test_email():
    """Test SMTP email configuration by sending a verification message."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    s = dict(cursor.fetchone() or {})
    conn.close()

    recipient = request.form.get("test_recipient") or s.get("alert_recipient_email") or s.get("email")
    if not recipient:
        return jsonify({"success": False, "error": "Recipient email address is required."})

    smtp_host = s.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(s.get("smtp_port", 587))
    smtp_user = s.get("smtp_user", "")
    smtp_password = s.get("smtp_password", "")

    if not smtp_user or not smtp_password:
        return jsonify({
            "success": False,
            "error": "SMTP Username and App Password are not configured. Please save credentials first."
        })

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[TEST ALERT] Vasantham Printers HRMS Email Dispatch Verification"
        msg["From"] = f"Vasantham Printers HRMS <{smtp_user}>"
        msg["To"] = recipient

        body_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px;">
            <h2 style="color: #1e3a8a; margin-top: 0;">Vasantham Printers • Email Alert Verification</h2>
            <p style="color: #475569; font-size: 14px;">This is a test notification confirming that your SMTP email alert system is functioning properly.</p>
            <div style="background: #f8fafc; padding: 12px; border-radius: 8px; font-size: 12px; color: #334155;">
                <p><strong>SMTP Server:</strong> {smtp_host}:{smtp_port}</p>
                <p><strong>Alert Recipient:</strong> {recipient}</p>
                <p><strong>Status:</strong> Ready for Morning Missed Punch Alerts</p>
            </div>
            <p style="font-size: 11px; color: #94a3b8; margin-top: 15px;">Automated notification from Shift & Overtime Payroll HRMS.</p>
        </div>
        """
        msg.attach(MIMEText(body_html, "html"))

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
        if s.get("smtp_use_tls", 1):
            server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], msg.as_string())
        server.quit()

        return jsonify({"success": True, "message": f"Test email sent successfully to {recipient}!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
