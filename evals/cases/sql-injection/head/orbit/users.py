def find_user(db, name):
    query = f"SELECT id, name FROM users WHERE name = '{name}'"
    return db.execute(query).fetchall()


def count_users(db):
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
