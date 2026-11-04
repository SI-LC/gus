"""Settings and secrets.

config.json      defaults, part of the code (tracked)
settings.json    your overrides, set from the cockpit (git-ignored)
.secrets.json    keys and tokens, readable only by you (git-ignored, never shown back in full)

SCHEMA describes every setting so the cockpit can draw the form.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "config.json"
USER = ROOT / "settings.json"
SECRETS = ROOT / ".secrets.json"

PROVIDERS = {
    "claude_subscription": "Claude subscription (Claude Code login)",
    "anthropic_api": "Anthropic API key (via Claude Code)",
    "openai_compatible": "OpenAI-compatible API (scoring only)",
}
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]

# key, label, type, group, help. Types: text, regex, int, bool, select:<options>, times
SCHEMA = [
    ("ai_provider", "Provider", "select:provider", "AI",
     "Claude providers can do everything. OpenAI-compatible can score roles, but can't search the web for people or run the agent."),
    ("model_scoring", "Model for scoring roles", "select:claude_model", "AI", "Runs on every new role. Sonnet is a good balance."),
    ("model_people", "Model for finding people", "select:claude_model", "AI", "Uses web search, for roles above the people threshold."),
    ("model_agent", "Model for agent runs", "select:claude_model", "AI", "Discovery and watchlist loops. The most judgement-heavy work."),
    ("openai_base_url", "OpenAI-compatible base URL", "text", "AI",
     "e.g. https://api.openai.com/v1, https://openrouter.ai/api/v1, or http://localhost:11434/v1 for Ollama."),
    ("openai_model", "OpenAI-compatible model", "text", "AI", "Only used when that provider is selected."),

    ("ai_daily_budget_usd", "Daily AI limit (USD)", "money", "AI",
     "Runs pause for the day at this much, in the equivalent API prices shown on Costs. 0 turns AI runs off: scans still find roles, but nothing is scored."),

    ("title_include", "Titles to include", "regex", "Matching", "Regular expression, case-insensitive."),
    ("title_exclude", "Titles to exclude", "regex", "Matching", "Regular expression, case-insensitive."),
    ("location_include", "Locations to include", "regex", "Matching", "Regular expression, case-insensitive."),
    ("notify_min_score", "Notify at score", "int:1-10", "Matching", "Push a notification for roles at or above this."),
    ("people_min_score", "Find people at score", "int:1-10", "Matching", "Search for the people behind roles at or above this."),

    ("scheduler_enabled", "Run automatically each day", "bool", "Schedule",
     "Starts the full run at your daily time. Off means it only runs from the button."),
    ("run_time", "Daily run time", "times", "Schedule", "When the full run starts each day, 24h, e.g. 08:00. Leave empty to run it only from the button."),
    ("watchlist_every_days", "Refresh accounts every (days)", "int:1-30", "Schedule", "The heaviest step of the run, so it's skipped until this many days have passed."),
    ("agent_max_searches", "Web searches per agent run", "int:10-200", "Schedule", "A budget, so runs don't use too much of your allowance."),

    ("source_hn", "HN Who is hiring", "bool", "Feeds", "Monthly thread, scanned with the job boards."),

    ("source_x", "X", "bool", "Social", "Needs an X API bearer token (Keys below). X bills per post."),
    ("x_daily_post_cap", "X posts per day", "int:0-2000", "Social", "Hard cap on posts fetched, and paid for, each day."),
    ("x_cost_per_post", "X price per post (USD)", "money", "Social", "Used to estimate X costs. Check your X developer console for the real rate."),
    ("source_bluesky", "Bluesky", "bool", "Social", "Free, open search. Also used when finding people."),
    ("social_queries", "Bluesky searches", "lines", "Social", "One search per line. Posts must also mention a product role and one of your locations."),
    ("source_mastodon", "Mastodon", "bool", "Social", "Free, open hashtag timelines."),
    ("social_mastodon_tags", "Mastodon hashtags", "lines", "Social", "Comma or line separated, without #."),
    ("mastodon_instance", "Mastodon server", "text", "Social", "e.g. mastodon.social or hachyderm.io"),
    ("social_include_bots", "Keep job-bot posts", "bool", "Social", "Bots repost listings. Off keeps only posts from people; on also uses bots to discover companies."),
    ("source_reddit", "Reddit", "bool", "Social", "Needs a free Reddit 'script' app: client id and secret in Keys."),
    ("social_reddit_subs", "Subreddits", "lines", "Social", "Without r/."),

    ("feeds_disabled", "Feeds turned off", "lines", "Feeds", "Names of built-in feeds to skip, one per line."),
    ("feeds_extra", "Your feeds", "lines", "Feeds",
     "One per line: Name | https://feed.url | kind | minutes. kind is signals (keyword-filtered), newsletter, jobs or community."),

    ("source_gmail", "Read this Gmail account", "bool", "Email", "Checked on every scan. Read-only: nothing is sent, deleted or marked as read."),
    ("gmail_address", "Gmail address", "text", "Email", "The account that receives your recruiter and LinkedIn emails."),
    ("inbox_days", "Look back (days)", "int:1-30", "Email", "How far back each check goes."),
    ("inbox_senders", "Senders to read", "lines", "Email", "Any part of an email address, one per line. Other emails stay unopened."),
    ("inbox_subjects", "Subjects to read", "regex", "Email", "Emails with a subject matching this are read too. Regular expression, case-insensitive."),

    ("my_employers", "Your employers", "lines", "Network",
     "One per line. Used to spot people who worked where you did."),

    ("notify_ntfy_server", "ntfy server", "text", "Notifications", "https://ntfy.sh, or your own server."),
    ("quiet_hours", "Quiet hours", "text", "Notifications", "e.g. 22:00-07:00. Notifications are held until it ends. Empty for none."),
    ("cockpit_url", "Cockpit address", "text", "Notifications", "Where notifications link to, e.g. http://raspberrypi:8484 or your Tailscale name."),
]

SECRET_KEYS = {
    "anthropic_api_key": ("Anthropic API key", "Only for the Anthropic API provider."),
    "openai_api_key": ("OpenAI-compatible API key", "Only for the OpenAI-compatible provider. Leave empty for local Ollama."),
    "gmail_app_password": ("Gmail app password", "A 16-letter password from your Google account, not your normal one."),
    "x_bearer_token": ("X API bearer token", "From developer.x.com."),
    "ntfy_topic": ("ntfy topic", "A long random name. Anyone who knows it can read your notifications."),
    "cockpit_token": ("Cockpit access token", "Required before opening the cockpit to other devices. Add ?t=<token> to the URL once."),
    "companies_house_key": ("Companies House API key", "Free, from the Companies House developer hub. Spots new UK branches."),
    "reddit_client_id": ("Reddit client id", "From reddit.com/prefs/apps, a 'script' app."),
    "reddit_client_secret": ("Reddit client secret", ""),
}
# older single-file secrets, still honoured
LEGACY = {"x_bearer_token": ".x_token", "ntfy_topic": ".ntfy_topic", "cockpit_token": ".cockpit_token",
          "companies_house_key": ".companies_house_key"}
ENV = {"anthropic_api_key": "ANTHROPIC_API_KEY", "x_bearer_token": "X_BEARER_TOKEN", "ntfy_topic": "NTFY_TOPIC"}


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def load() -> dict:
    return {**_read(DEFAULTS), **_read(USER)}


def validate(key: str, value):
    row = next((r for r in SCHEMA if r[0] == key), None)
    if not row:
        raise ValueError(f"Unknown setting: {key}")
    kind = row[2]
    if kind == "bool":
        return bool(value)
    if kind.startswith("int"):
        lo, hi = map(int, kind.split(":")[1].split("-"))
        v = int(value)
        if not lo <= v <= hi:
            raise ValueError(f"{row[1]} must be between {lo} and {hi}")
        return v
    if kind == "money":
        v = float(value)
        if v < 0:
            raise ValueError(f"{row[1]} can't be negative")
        return v
    if kind == "regex":
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"{row[1]}: not a valid pattern ({e})")
        return str(value)
    if kind == "lines":
        return "\n".join(x.strip() for x in re.split(r"[\n,]", str(value)) if x.strip()) if "tags" in key or "subs" in key or "senders" in key \
            else "\n".join(x.strip() for x in str(value).splitlines() if x.strip())
    if kind == "times":
        times = [t.strip() for t in str(value).split(",") if t.strip()]
        for t in times:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"{row[1]}: use 24h times like 08:00")
        return ", ".join(times)
    if kind == "select:provider" and value not in PROVIDERS:
        raise ValueError("Unknown provider")
    if kind == "select:claude_model" and value not in CLAUDE_MODELS:
        raise ValueError("Unknown model")
    return str(value).strip()


def save(changes: dict) -> dict:
    user = _read(USER)
    defaults = _read(DEFAULTS)
    for k, v in changes.items():
        v = validate(k, v)
        if defaults.get(k) == v:
            user.pop(k, None)  # back to default
        else:
            user[k] = v
    USER.write_text(json.dumps(user, indent=2, ensure_ascii=False))
    return load()


def secret(key: str) -> str:
    if ENV.get(key) and os.environ.get(ENV[key]):
        return os.environ[ENV[key]]
    v = _read(SECRETS).get(key)
    if v:
        return v
    legacy = LEGACY.get(key)
    if legacy and (ROOT / legacy).exists():
        return (ROOT / legacy).read_text().strip()
    return ""


def set_secret(key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        raise ValueError("Unknown secret")
    s = _read(SECRETS)
    if value:
        s[key] = value.strip()
    else:
        s.pop(key, None)
    tmp = SECRETS.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the first byte
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2)
    tmp.replace(SECRETS)
    os.chmod(SECRETS, stat.S_IRUSR | stat.S_IWUSR)


def secrets_masked() -> dict:
    out = {}
    for k in SECRET_KEYS:
        v = secret(k)
        out[k] = f"set (…{v[-4:]})" if len(v) > 8 else ("set" if v else "")
    return out
