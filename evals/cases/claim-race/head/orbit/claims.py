import os


def claim(path, owner):
    """Write `owner` to `path` unless somebody already claimed it."""
    if os.path.exists(path):
        return False
    with open(path, "w") as f:
        f.write(owner)
    return True
