def find_user(db, name):
    return db.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchall()


def count_users(db):
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
