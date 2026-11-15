"""Sessions (JWT in the Authorization header), single-use stream tickets, and the login_required decorator."""
import secrets
import threading
import time
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from .config import settings

ISSUER = "warren"
_tickets: dict[str, tuple[int, float]] = {}
_ticket_lock = threading.Lock()


def issue_token(user_id: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "iss": ISSUER, "iat": now, "exp": now + settings.session_hours * 3600}, settings.secret_key, "HS256")


def issue_state(nonce: str) -> str:
    """OAuth `state`: short-lived and bound to the browser through an HttpOnly cookie holding the same nonce (anti-CSRF)."""
    return jwt.encode({"purpose": "oauth", "nonce": nonce, "iss": ISSUER, "exp": int(time.time()) + 600}, settings.secret_key, "HS256")


def verify_state(state: str) -> str | None:
    try:
        claims = jwt.decode(state, settings.secret_key, ["HS256"], issuer=ISSUER)
    except jwt.PyJWTError:
        return None
    return claims.get("nonce") if claims.get("purpose") == "oauth" else None


def issue_ticket(user_id: int) -> str:
    """60-second, single-use credential for EventSource (which cannot send headers) so the session token never appears in a URL."""
    ticket = secrets.token_urlsafe(24)
    with _ticket_lock:
        for key in [k for k, (_, exp) in _tickets.items() if exp < time.time()]:
            del _tickets[key]
        _tickets[ticket] = (user_id, time.time() + 60)
    return ticket


def consume_ticket(ticket: str) -> int | None:
    with _ticket_lock:
        user_id, exp = _tickets.pop(ticket, (None, 0))
    return user_id if user_id and exp >= time.time() else None


def _authenticate(user_id: int | None):
    user = current_app.db.get("users", user_id) if user_id else None
    if not user:
        return jsonify(error="unauthorized"), 401
    g.user = user
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        try:
            claims = jwt.decode(header[7:] if header.startswith("Bearer ") else "", settings.secret_key, ["HS256"], issuer=ISSUER)
            user_id = int(claims["sub"])
        except (jwt.PyJWTError, KeyError, ValueError):
            return jsonify(error="unauthorized"), 401
        if (denied := _authenticate(user_id)):
            return denied
        return fn(*args, **kwargs)

    return wrapper


def ticket_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if (denied := _authenticate(consume_ticket(request.args.get("ticket", "")))):
            return denied
        return fn(*args, **kwargs)

    return wrapper
