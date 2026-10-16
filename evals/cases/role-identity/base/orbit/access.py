def can_delete(user):
    return user.role == "admin" or user.id == user.owner_id
