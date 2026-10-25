import sqlite3
from backend.config import DB_PATH
from werkzeug.security import generate_password_hash

def seed_admin():
    name = input("Admin Name: ")
    phone = input("Phone (11 digits): ")
    password = input("Password: ")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    password_hash = generate_password_hash(password)

    cursor.execute("""
        INSERT INTO users (full_name, phone, password_hash, login_key, status, created_at)
        VALUES (?, ?, ?, ?, 'active', datetime('now'))
    """, (name, phone, password_hash, phone))

    user_id = cursor.lastrowid

    cursor.execute("INSERT INTO user_roles (user_id, role) VALUES (?, 'admin')", (user_id,))

    conn.commit()
    conn.close()
    print("Admin created.")

if __name__ == "__main__":
    seed_admin()
