def claim(path, owner):
    """Write `owner` to `path` unless somebody already claimed it."""
    try:
        with open(path, "x") as f:
            f.write(owner)
    except FileExistsError:
        return False
    return True
