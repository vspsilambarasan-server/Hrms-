import os
import sys
from flask import Flask, render_template, request, redirect, url_for, session
from database import init_db, get_db_connection

from routes.auth import auth_bp
from routes.dashboard import dashboard_bp
from routes.employees import employees_bp
from routes.shifts import shifts_bp
from routes.attendance import attendance_bp
from routes.overtime import overtime_bp
from routes.payroll import payroll_bp
from routes.api import api_bp
from routes.piece_rate import piece_rate_bp
from routes.settings import settings_bp
from routes.settlement import settlement_bp

def create_app(test_config=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "hrms-shift-ot-secret-key-2026")
    if "unittest" in sys.modules or os.environ.get("TESTING") == "1":
        app.config["TESTING"] = True
    if test_config:
        app.config.update(test_config)

    # Initialize DB if not present
    init_db()

    # Register Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(shifts_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(overtime_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(piece_rate_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(settlement_bp)

    # Global Login Gate
    @app.before_request
    def check_login():
        # Exempt testing mode unless ENFORCE_AUTH is explicitly set
        if app.config.get("TESTING") and not app.config.get("ENFORCE_AUTH"):
            return None

        # Static assets and auth routes are always public
        if request.endpoint in ("auth.login", "auth.logout", "static") or request.path.startswith("/static/"):
            return None

        if not session.get("user_id"):
            if request.path.startswith("/api/") or request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return {"error": "Authentication required"}, 401
            return redirect(url_for("auth.login", next=request.url))

    # Global template context processor
    @app.context_processor
    def inject_global_data():
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Pending Overtime badge count
        cursor.execute("SELECT COUNT(*) as count FROM overtime_records WHERE status = 'PENDING'")
        pending_ot_count = cursor.fetchone()["count"]

        # Pending Missing Punches badge count
        try:
            cursor.execute("SELECT COUNT(*) as count FROM missing_punch_alerts WHERE status = 'PENDING'")
            pending_missing_punches = cursor.fetchone()["count"]
        except Exception:
            pending_missing_punches = 0

        # Company settings
        cursor.execute("SELECT * FROM company_settings LIMIT 1")
        settings_row = cursor.fetchone()
        company_info = dict(settings_row) if settings_row else {
            "company_name": "Vasantham Printers",
            "currency_symbol": "₹"
        }

        conn.close()

        from models.auto_sync import get_auto_sync_status

        current_user = {
            "id": session.get("user_id"),
            "username": session.get("username", "admin"),
            "full_name": session.get("full_name", "Administrator"),
            "role": session.get("role", "admin")
        } if session.get("user_id") else None

        return {
            "global_pending_ot": pending_ot_count,
            "global_pending_missing_punches": pending_missing_punches,
            "company_info": company_info,
            "auto_sync_status": get_auto_sync_status(),
            "current_user": current_user
        }

    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    # Start the 1-minute Biometric Auto-Sync background worker
    if not app.config.get("TESTING"):
        import atexit
        from models.auto_sync import start_auto_sync, stop_auto_sync
        from database import checkpoint_db
        start_auto_sync()
        atexit.register(stop_auto_sync)
        atexit.register(checkpoint_db)

    return app

if __name__ == "__main__":
    app = create_app()
    print("\n" + "="*60)
    print(" [RUNNING] Shift & OT Payroll HRMS (Production WSGI - Waitress)")
    print(" Local:   http://127.0.0.1:5000")
    print(" Network: http://0.0.0.0:5000")
    print("="*60 + "\n")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=5000, debug=False)

