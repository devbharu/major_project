"""GitHub OAuth login (plus a personal-access-token fallback and a demo login)."""
import secrets

import requests
from flask import Blueprint, current_app, g, jsonify, make_response, redirect, request

from .. import crypto
from ..auth import issue_state, issue_token, login_required, verify_state
from ..config import settings
from ..db import now
from ..ratelimit import limited
from ..services.github import GitHubClient, GitHubError

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _login(gh_user: dict, token: str) -> str:
    db = current_app.db
    user_id = db.upsert("users", {"github_id": gh_user["id"]}, {
        "login": gh_user["login"], "name": gh_user.get("name") or gh_user["login"],
        "avatar_url": gh_user.get("avatar_url", ""), "token": crypto.encrypt(token), "created_at": now()})
    return issue_token(user_id)


@bp.get("/config")
def config():
    return jsonify(github_oauth=settings.github_oauth_enabled, demo=settings.demo_seed, bot_mention=settings.bot_mention)


@bp.get("/github/login")
def github_login():
    if not settings.github_oauth_enabled:
        return redirect(f"{settings.frontend_url}/login?error=GitHub+OAuth+is+not+configured")
    nonce = secrets.token_urlsafe(16)
    callback = f"{settings.backend_url}/api/auth/github/callback"
    resp = make_response(redirect("https://github.com/login/oauth/authorize?" + requests.compat.urlencode({
        "client_id": settings.github_client_id, "redirect_uri": callback, "scope": "repo workflow read:user", "state": issue_state(nonce)})))
    resp.set_cookie("warren_oauth", nonce, max_age=600, httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


@bp.get("/github/callback")
def github_callback():
    nonce = verify_state(request.args.get("state", ""))
    if not nonce or not secrets.compare_digest(nonce, request.cookies.get("warren_oauth", "")):
        return redirect(f"{settings.frontend_url}/login?error=Invalid+OAuth+state")
    resp = requests.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, timeout=20, data={
        "client_id": settings.github_client_id, "client_secret": settings.github_client_secret, "code": request.args.get("code", "")})
    token = resp.json().get("access_token")
    if not token:
        return redirect(f"{settings.frontend_url}/login?error=GitHub+rejected+the+login")
    session = _login(GitHubClient(token).user(), token)
    out = make_response(redirect(f"{settings.frontend_url}/auth/callback#token={session}"))
    out.delete_cookie("warren_oauth")
    return out


@bp.post("/token-login")
@limited("token-login", 10)
def token_login():
    token = (request.get_json(silent=True) or {}).get("token", "").strip()
    try:
        gh_user = GitHubClient(token).user()
    except GitHubError as exc:
        return jsonify(error=f"GitHub rejected that token ({exc.status})"), 401
    return jsonify(token=_login(gh_user, token))


@bp.post("/demo")
@limited("demo", 20)
def demo_login():
    if not settings.demo_seed:
        return jsonify(error="demo mode is off (set DEMO_SEED=true)"), 404
    user = current_app.db.one("SELECT id FROM users WHERE login='demo'")
    return jsonify(token=issue_token(user["id"]))


@bp.get("/me")
@login_required
def me():
    u = g.user
    return jsonify(id=u["id"], login=u["login"], name=u["name"], avatar_url=u["avatar_url"], demo=u["login"] == "demo")
