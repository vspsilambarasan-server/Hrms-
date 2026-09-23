from database import get_db_connection

conn = get_db_connection()
conn.execute("UPDATE company_settings SET company_name = 'Vasantham Printers' WHERE id = 1")
conn.commit()
row = conn.execute("SELECT company_name FROM company_settings WHERE id = 1").fetchone()
print("Successfully updated company name to:", row["company_name"])
conn.close()
