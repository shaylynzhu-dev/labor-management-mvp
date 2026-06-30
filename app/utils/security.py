from functools import wraps

from flask import session

from .responses import api_response


def roles_required(*roles):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                return api_response(403, "forbidden", None, 403)
            return function(*args, **kwargs)
        return wrapped
    return decorator
