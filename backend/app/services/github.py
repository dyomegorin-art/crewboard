"""GitHub OAuth (authorization code). Сетевые вызовы изолированы здесь, чтобы тесты их подменяли."""

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from app.config import get_settings

STATE_TTL = timedelta(minutes=10)
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"


class GithubError(Exception):
    pass


@dataclass
class GithubProfile:
    id: str
    email: str  # primary + verified на стороне GitHub
    name: str


def authorize_url(state: str) -> str:
    s = get_settings()
    query = urlencode(
        {
            "client_id": s.github_client_id,
            "scope": "read:user user:email",
            "state": state,
            "redirect_uri": f"{s.public_url}/api/auth/github/callback",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def fetch_profile(code: str) -> GithubProfile:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(
                TOKEN_URL,
                data={
                    "client_id": s.github_client_id,
                    "client_secret": s.github_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token = r.json().get("access_token")
            if not token:
                raise GithubError("no access token")
            auth = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            user = (await http.get(f"{API}/user", headers=auth)).json()
            emails = (await http.get(f"{API}/user/emails", headers=auth)).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GithubError(type(exc).__name__) from exc
    primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
    if primary is None or "id" not in user:
        raise GithubError("no verified primary email")
    return GithubProfile(
        id=str(user["id"]), email=primary["email"], name=user.get("name") or user["login"]
    )
