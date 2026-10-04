from fastapi import Request


class LoginRequired(Exception):
    pass


def is_admin(request: Request) -> bool:
    return request.session.get("admin") is True


def require_admin(request: Request):
    if not is_admin(request):
        raise LoginRequired()
