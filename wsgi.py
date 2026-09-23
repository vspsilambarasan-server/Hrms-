"""
Shift & OT Payroll HRMS - Production WSGI Entrypoint
Powered by Waitress production WSGI server.
"""

from app import create_app
from waitress import serve

app = create_app()

if __name__ == "__main__":
    print("\n" + "="*60)
    print(" [RUNNING] Shift & OT Payroll HRMS (Production WSGI - Waitress)")
    print(" Local:   http://127.0.0.1:5000")
    print(" Network: http://0.0.0.0:5000")
    print(" Threads: 8 worker threads")
    print("="*60 + "\n")
    serve(app, host="0.0.0.0", port=5000, threads=8)
