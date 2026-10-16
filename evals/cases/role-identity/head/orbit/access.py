def can_delete(user):
    return user.role is "admin" or user.id == user.owner_id
