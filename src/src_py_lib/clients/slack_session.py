"""Browser sign-in to capture a Slack web session token and cookie.

The Slack web client authenticates with an `xoxc-...` session token (stored
in the browser's localStorage) plus a `d` session cookie. Together they call
the Web API as the signed-in user, including user-only methods such as
`search.messages` and undocumented web-client endpoints such as
`emoji.adminList`. `browser_signin` opens a real browser via Playwright so a
human can sign in (SSO included), then extracts both values. Save them to a
gitignored, permission-restricted file: they act with the user's full access,
so guard the file like a password.

Playwright is an optional dependency; only `browser_signin` needs it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from src_py_lib.clients.slack import SESSION_COOKIE_NAME, SlackClient, SlackError, SlackPacer
from src_py_lib.utils.config import ConfigError
from src_py_lib.utils.http import HTTPClient
from src_py_lib.utils.json_types import json_dict, json_str

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

SESSION_FILE_MODE: Final[int] = 0o600
SIGNIN_TIMEOUT_SECONDS: Final[int] = 300
SIGNIN_POLL_SECONDS: Final[float] = 1.0
WEB_CLIENT_URL_PREFIX: Final[str] = "https://app.slack.com/client"
SESSION_TOKEN_PREFIX: Final[str] = "xoxc-"
LOCAL_STORAGE_CONFIG_KEY: Final[str] = "localConfig_v2"


@dataclass(frozen=True)
class SlackSession:
    """Slack web client credentials captured from a browser sign-in."""

    workspace_url: str  # e.g. https://example.slack.com
    session_token: str  # xoxc-...
    session_cookie: str  # value of the `d` cookie


def read_session(path: Path) -> SlackSession | None:
    """Return the saved session from `path`, or None when absent/incomplete."""
    if not path.exists():
        return None
    data = json_dict(json.loads(path.read_text()))
    session = SlackSession(
        workspace_url=json_str(data, "workspace_url").rstrip("/"),
        session_token=json_str(data, "session_token"),
        session_cookie=json_str(data, "session_cookie"),
    )
    if not (session.workspace_url and session.session_token and session.session_cookie):
        logger.warning("Session file %s is incomplete; ignoring it.", path)
        return None
    return session


def write_session(path: Path, session: SlackSession) -> None:
    """Write `session` to `path`, readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2) + "\n")
    path.chmod(SESSION_FILE_MODE)


def slack_client_from_session(
    session: SlackSession,
    *,
    http: HTTPClient | None = None,
    pacer: SlackPacer | None = None,
) -> SlackClient:
    """Return a Slack API client that acts as the signed-in browser user."""
    return SlackClient(
        session.session_token,
        http=http or HTTPClient(max_attempts=1),
        pacer=pacer or SlackPacer(),
        session_cookie=session.session_cookie,
    )


def browser_signin(workspace_url: str, profile_directory: Path) -> SlackSession:
    """Open a browser for the human to sign in; return the captured session.

    Uses a persistent browser profile so a subsequent sign-in (after the
    session expires) usually just needs a click, not the full SSO dance.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exception:
        raise ConfigError(
            "Playwright is required for browser sign-in. Install it with:\n"
            "  uv add 'src-py-lib[slack-session]'\n"
            "  uv run playwright install chromium"
        ) from exception

    logger.info("Opening a browser; sign in to %s...", workspace_url)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_directory), headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(workspace_url)
        session_token = _wait_for_session_token(context)
        session_cookie = _session_cookie(context)
        context.close()
    if not session_cookie:
        raise SlackError(f"Signed in, but no `{SESSION_COOKIE_NAME}` session cookie was found.")
    logger.info("Captured a Slack web session token and cookie.")
    return SlackSession(workspace_url.rstrip("/"), session_token, session_cookie)


def _wait_for_session_token(context: BrowserContext) -> str:
    """Poll open tabs until one is the signed-in web client with a token."""
    deadline = time.monotonic() + SIGNIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for open_page in context.pages:
            if open_page.url.startswith(WEB_CLIENT_URL_PREFIX):
                token = _session_token_from_page(open_page)
                if token:
                    return token
        time.sleep(SIGNIN_POLL_SECONDS)
    raise SlackError(
        f"Timed out after {SIGNIN_TIMEOUT_SECONDS}s waiting for Slack sign-in. "
        "If Slack offered to open the desktop app, choose 'use Slack in your browser'."
    )


def _session_token_from_page(page: Page) -> str:
    """Extract the xoxc session token from the web client's localStorage."""
    try:
        local_config = page.evaluate(
            f"() => window.localStorage.getItem('{LOCAL_STORAGE_CONFIG_KEY}')"
        )
    except Exception:  # page may be mid-navigation; retried by the caller
        return ""
    if not isinstance(local_config, str):
        return ""
    teams = json_dict(json_dict(json.loads(local_config)).get("teams"))
    for team in teams.values():
        token = json_str(json_dict(team), "token")
        if token.startswith(SESSION_TOKEN_PREFIX):
            return token
    return ""


def _session_cookie(context: BrowserContext) -> str:
    """Return the Slack `d` session cookie value from the browser context."""
    for cookie in context.cookies():
        if cookie.get("name") == SESSION_COOKIE_NAME and "slack.com" in cookie.get("domain", ""):
            return cookie.get("value", "")
    return ""
