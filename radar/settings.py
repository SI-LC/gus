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

from radar.paths import ROOT
from radar.store import read
DEFAULTS = ROOT / "config.json"
USER = ROOT / "settings.json"
SECRETS = ROOT / ".secrets.json"

PROVIDERS = {
    "claude_subscription": "My Claude plan (sign in, no API key)",
    "anthropic_api": "Anthropic API key (pay per use)",
    "openai_compatible": "OpenAI-compatible API (scoring only)",
}
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]

# key, label, type, group, help. Types: text, lines, int:lo-hi, money, bool, select:<options>, times
SCHEMA = [
    ("ai_provider", "How Gus connects to AI", "select:provider", "AI",
     "A Claude plan or an Anthropic API key can do everything. An OpenAI-compatible API can score roles, but can't search the web for people or run the agent."),
    ("model_scoring", "Model for scoring roles", "select:claude_model", "AI", "Runs on every new role. Sonnet is a good balance."),
    ("model_people", "Model for finding people", "select:claude_model", "AI", "Uses web search, for roles above the people threshold."),
    ("model_agent", "Model for agent runs", "select:claude_model", "AI", "Discovery and watchlist loops. The most judgement-heavy work."),
    ("model_quick", "Model for your first look", "select:claude_model", "AI", "Only for the first set of roles, where speed matters most."),
    ("openai_base_url", "OpenAI-compatible base URL", "text", "AI",
     "e.g. https://api.openai.com/v1, https://openrouter.ai/api/v1, or http://localhost:11434/v1 for Ollama."),
    ("openai_model", "OpenAI-compatible model", "text", "AI", "Only used when that provider is selected."),

    ("ai_daily_budget_usd", "Daily AI limit (USD)", "money", "AI",
     "Runs pause for the day at this much, in the equivalent API prices shown on Usage. 0 turns AI runs off: scans still find roles, but nothing is scored."),

    ("title_include", "Titles to include", "lines", "Matching", "Keep a role if its title contains any of these. One per line. Capitals don't matter."),
    ("title_exclude", "Titles to leave out", "lines", "Matching", "Drop a role if its title contains any of these, even if it's also in the list above."),
    ("title_exclude_except", "Exceptions to leave out", "lines", "Matching", "Phrases the list above ignores, so \"engineer\" there doesn't drop a product engineer."),
    ("location_include", "Locations to include", "lines", "Matching", "One per line. Add (exactly) to match only that word, so \"remote (exactly)\" won't match \"Remote, US\"."),
    ("where_groups", "Places in Role Inbox", "lines", "Matching", "How Role Inbox groups roles. One per line, Label: words to look for. The first that fits wins, and * means anything else."),
    ("role_kinds", "Role types in Role Inbox", "lines", "Matching", "Optional, in the same form, like Engineering: engineer, developer. Leave empty for no role types."),
    ("notify_min_score", "Notify at score", "int:1-10", "Matching", "Push a notification for roles at or above this."),
    ("salary_floor", "Lowest salary you want", "int:0-1000000", "Matching", "A yearly figure. Roles that state less lose a point per 10% under it, up to 5. Roles with no stated salary aren't changed. 0 turns this off."),
    ("salary_symbol", "Salary currency symbol", "text", "Matching", "Used to read salaries from job ads, e.g. £."),
    ("people_min_score", "Find people at score", "int:1-10", "Matching", "Search for the people behind roles at or above this."),

    ("scheduler_enabled", "Run automatically each day", "bool", "Schedule",
     "Starts the full run at your daily time. Off means it only runs from the button."),
    ("run_time", "Daily run time", "times", "Schedule", "When the full run starts each day, 24h, e.g. 08:00. Leave empty to run it only from the button."),
    ("catch_up_missed_run", "Catch up a missed daily run", "bool", "Schedule",
     "If Gus wasn't running at the daily time, because this computer was off, it runs when it next starts. Off means it waits for tomorrow."),
    ("watchlist_every_days", "Refresh accounts every (days)", "int:1-30", "Schedule", "The heaviest step of the run, so it's skipped until this many days have passed."),
    ("agent_max_searches", "Web searches per agent run", "int:10-200", "Schedule", "A budget, so runs don't use too much of your allowance."),
    ("prune_after_days", "Tidy old records after (days)", "int:14-365", "Schedule",
     "A closed or skipped role you never touched loses its saved advert text after this long, and ignored signals and closed events move to data/archive/. Roles you prepared or applied for are never touched."),

    ("source_hn", "HN Who is hiring", "bool", "Feeds", "Monthly thread, scanned with the job boards."),
    ("hn_title_include", "HN: words to look for", "lines", "Feeds", "A post must mention one of these. Leave empty to use your titles to include."),

    ("source_adzuna", "Adzuna", "bool", "Job sources", "Job search across many sites, with short extracts of each ad. Needs the Adzuna keys (Keys tab)."),
    ("source_jsearch", "JSearch", "bool", "Job sources", "Google for Jobs results with full descriptions, including some LinkedIn roles. Needs the JSearch key (Keys tab)."),
    ("source_mindtheproduct", "Mind the Product jobs", "bool", "Job sources",
     "Product roles from many companies on mindtheproduct.com/jobs, read with the job boards. Up to 100 new ads are read per scan, so the first few scans work through its backlog."),
    ("getro_boards", "VC job boards", "lines", "Job sources",
     "One address per line for a VC's portfolio job board run on Getro, e.g. careers.atomico.com/jobs. Each role arrives under its own company. Up to 100 new ads per board are read per scan."),
    ("google_careers_search", "Google Careers search", "text", "Job sources",
     "Paste the address of a search on Google's careers site. Gus reads its first page once a day, newest first, as that's all the site's rules allow. Leave empty to turn this off."),
    ("amazon_jobs_search", "Amazon Jobs search", "text", "Job sources",
     "Paste the address of a search on amazon.jobs, with the location set there: Amazon's roles, AWS included, aren't on a job board Gus can read. Read once a day, newest first, up to 500 roles. Leave empty to turn this off."),
    ("microsoft_careers_search", "Microsoft Careers search", "text", "Job sources",
     "Paste the address of a search on apply.careers.microsoft.com, with the location set there. Read once a day, newest first, up to 100 roles, slowly: the site turns away anything faster. Leave empty to turn this off."),
    ("apple_jobs_search", "Apple Jobs search", "text", "Job sources",
     "Paste the address of a search on jobs.apple.com, with the location set there. Read once a day, newest first, up to 200 roles. Leave empty to turn this off."),
    ("sources_location", "Location", "text", "Job sources", "A town or city. Leave empty to search the whole country, which finds more roles."),
    ("sources_country", "Country code", "text", "Job sources", "Two letters, e.g. gb, us, de."),
    ("sources_distance_km", "Distance (km)", "int:1-200", "Job sources", "Adzuna only: how far from the location to search."),
    ("sources_searches", "Searches", "lines", "Job sources", "One per line. Each one uses at least one request per source, so more searches use more allowance."),
    ("sources_max_age_days", "Oldest ad (days)", "int:1-30", "Source details", "Older ads are dropped. After a complete run, Gus only asks for what's new since then."),
    ("sources_close_after_days", "Close untouched ads after (days)", "int:14-180", "Source details", "These sources can't say when an ad has gone, so roles you haven't acted on are closed at this age."),
    ("adzuna_pages_per_search", "Adzuna: pages per search", "int:1-14", "Source details", "Up to 50 roles a page. More pages find more roles but use more requests."),
    ("jsearch_pages_per_search", "JSearch: pages per search", "int:1-10", "Source details", "About 10 roles a page. More pages find more roles but use more requests."),
    ("source_company_exclude", "Companies to leave out", "lines", "Source details", "Recruiters, agencies and job sites listed as the employer. One per line. Capitals don't matter. Use * for any ending, like headhunt*."),
    ("source_company_except", "Companies to keep", "lines", "Source details", "Real employers the list above would otherwise catch, like talent systems."),
    ("source_text_exclude", "Wording to leave out", "lines", "Source details", "Phrases in an ad that show it comes from an agency. One per line. Capitals don't matter."),
    ("links_ai_search", "Find employer pages with AI", "bool", "Job sources", "For strong roles with no employer link, Claude searches the web for the employer's own page. Uses your Claude allowance for each role."),
    ("links_ai_min_score", "AI search: lowest score", "int:5-10", "Job sources", "Only roles at or above this score get an AI search."),
    ("links_ai_per_run", "AI search: most roles per run", "int:1-30", "Job sources", "Others wait for the next run. Searches also stop at your daily AI limit."),
    ("sources_auto", "Check job sources each day", "bool", "Source limits", "Separate from the daily run, which can stay off. Only fetches sources that are turned on and due, then scores what's new."),
    ("sources_run_time", "Daily check time", "times", "Source limits", "24h, e.g. 09:00."),
    ("adzuna_every_days", "Adzuna: run every (days)", "int:1-30", "Source limits", "However often Gus runs, Adzuna is only asked this often."),
    ("adzuna_allow_month", "Adzuna: requests to allow a month", "int:1-100000", "Source limits", "Shared evenly across runs. Gus never goes over this or your limits below."),
    ("adzuna_limit_day", "Adzuna: daily limit", "int:0-100000", "Source limits", "Gus stops at this number. Change it if your Adzuna account differs."),
    ("adzuna_limit_week", "Adzuna: weekly limit", "int:0-100000", "Source limits", "Change it if your Adzuna account differs."),
    ("adzuna_limit_month", "Adzuna: monthly limit", "int:0-100000", "Source limits", "Change it if your Adzuna account differs."),
    ("jsearch_every_days", "JSearch: run every (days)", "int:1-30", "Source limits", "However often Gus runs, JSearch is only asked this often."),
    ("jsearch_allow_month", "JSearch: requests to allow a month", "int:1-100000", "Source limits", "Shared evenly across runs. Your plan's limit comes from JSearch itself, and Gus never goes over either."),

    ("source_x", "X", "bool", "Social", "Needs an X API bearer token (Keys tab). X bills per post."),
    ("x_daily_post_cap", "X posts per day", "int:0-2000", "Social", "Hard cap on posts fetched, and paid for, each day."),
    ("x_cost_per_post", "X price per post (USD)", "money", "Social", "Used to estimate X costs. Check your X developer console for the real rate."),
    ("source_bluesky", "Bluesky", "bool", "Social", "Free, open search. Also used when finding people."),
    ("social_queries", "Bluesky searches", "lines", "Social", "One search per line. Posts must also mention a target role and one of your locations."),
    ("source_mastodon", "Mastodon", "bool", "Social", "Free, open hashtag timelines."),
    ("social_mastodon_tags", "Mastodon hashtags", "lines", "Social", "Comma or line separated, without #."),
    ("mastodon_instance", "Mastodon server", "text", "Social", "e.g. mastodon.social or hachyderm.io"),
    ("social_role_include", "Posts must mention", "lines", "Social", "Leave empty to use your titles to include."),
    ("x_query", "X search", "text", "Social", "Leave empty to build one from your titles and locations."),
    ("social_include_bots", "Keep job-bot posts", "bool", "Social", "Bots repost listings. Off keeps only posts from people; on also uses bots to discover companies."),
    ("source_reddit", "Reddit", "bool", "Social", "Needs a free Reddit 'script' app: client id and secret in the Keys tab."),
    ("social_reddit_subs", "Subreddits", "lines", "Social", "Without r/."),

    ("feeds_disabled", "Feeds turned off", "lines", "Feeds", "Names of feeds to skip, one per line."),
    ("feeds_extra", "Your feeds", "lines", "Feeds",
     "One per line: Name | https://feed.url | kind | minutes. kind is signals (keyword-filtered), newsletter, jobs (reads the full post) or community."),

    ("source_gmail", "Read this Gmail account", "bool", "Email", "Checked on every scan. Read-only: nothing is sent, deleted or marked as read."),
    ("gmail_address", "Gmail address", "text", "Email", "The account that receives your recruiter and LinkedIn emails."),
    ("inbox_days", "Look back (days)", "int:1-30", "Email", "How far back each check goes."),
    ("inbox_senders", "Senders to read", "lines", "Email", "Any part of an email address, one per line. Other emails stay unopened."),
    ("inbox_subjects", "Subjects to read", "lines", "Email", "Emails with any of these in the subject are read too. One per line. Use * for any ending, like interview*."),

    ("my_employers", "Your employers", "lines", "Network",
     "One per line. Used to spot people who worked where you did."),

    ("notify_news", "Alert on hiring news", "bool", "Notifications",
     "A push when an A or B company posts about hiring, hires a product leader or lists a strong role. Once per event."),
    ("notify_ntfy_server", "ntfy server", "text", "Notifications", "https://ntfy.sh, or your own server."),
    ("quiet_hours", "Quiet hours", "text", "Notifications", "e.g. 22:00-07:00. Notifications are held until it ends. Empty for none."),
    ("cockpit_url", "Cockpit address", "text", "Notifications", "Where notifications link to, e.g. http://raspberrypi:8484 or your Tailscale name."),

    ("mcp_allow_writes", "Let Claude make changes", "bool", "Claude",
     "Off, Claude can only read your roles, companies, people and moves. On, it can also change stages, add roles, companies, people and news, update moves and start a run."),
]

PATTERN_ENDINGS = ("include", "exclude", "except")
ADVANCED = re.compile(r"[|\\()\[\]^$?+{}]")  # older settings written as patterns keep working
JOIN = r"[\s,(/&-]+"


def to_pattern(value) -> str:
    """Turn a plain list into one case-insensitive pattern. An empty list matches nothing."""
    parts = []
    for line in str(value or "").splitlines():
        item = line.strip()
        exact = item.lower().endswith(" (exactly)")
        item = item[:-len(" (exactly)")].strip() if exact else item
        if not item:
            continue
        if ADVANCED.search(item):
            parts.append(item)
            continue
        body = re.sub(r"(\\ |\\-)+", lambda _: JOIN, re.escape(item)).replace(r"\*", r"\w*")
        parts.append(rf"^\s*{body}\s*$" if exact else rf"(?<!\w){body}(?!\w)")
    return "|".join(parts) or "(?!)"


def groups(text) -> list:
    """Lines of `Label: words, more words` as (label, words), in order. A word of * matches anything."""
    out = []
    for line in str(text or "").splitlines():
        label, _, words = line.partition(":")
        if label.strip() and words.strip():
            out.append((label.strip(), [w.strip().lower() for w in words.split(",") if w.strip()]))
    return out


def group_of(text, rows) -> str:
    """The first group that fits. Short words like uk must stand alone; longer ones can sit inside a word."""
    text = (text or "").lower()
    for label, words in rows:
        for w in words:
            if w == "*" or (re.search(rf"\b{re.escape(w)}\b", text) if len(w) < 3 else w in text):
                return label
    return ""


SECRET_KEYS = {
    "anthropic_api_key": ("Anthropic API key", "From console.anthropic.com. Billed per use by Anthropic, separately from any Claude plan."),
    "openai_api_key": ("OpenAI-compatible API key", "Leave empty for local Ollama."),
    "gmail_app_password": ("Gmail app password", "A 16-letter password from your Google account, not your normal one."),
    "x_bearer_token": ("X API bearer token", "From developer.x.com."),
    "ntfy_topic": ("ntfy topic", "A long random name. Anyone who knows it can read your notifications."),
    "cockpit_token": ("Cockpit access token", "Required before opening the cockpit to other devices. Add ?t=<token> to the URL once."),
    "companies_house_key": ("Companies House API key", "Free, from the Companies House developer hub. Spots new UK branches."),
    "adzuna_app_id": ("Adzuna app ID", "From developer.adzuna.com, under your account."),
    "adzuna_app_key": ("Adzuna app key", "From developer.adzuna.com, under your account."),
    "jsearch_api_key": ("JSearch API key", "From app.openwebninja.com, under Account, Settings."),
    "reddit_client_id": ("Reddit client id", "From reddit.com/prefs/apps, a 'script' app."),
    "reddit_client_secret": ("Reddit client secret", ""),
}
# older single-file secrets, still honoured
LEGACY = {"x_bearer_token": ".x_token", "ntfy_topic": ".ntfy_topic", "cockpit_token": ".cockpit_token",
          "companies_house_key": ".companies_house_key"}
ENV = {"anthropic_api_key": "ANTHROPIC_API_KEY", "x_bearer_token": "X_BEARER_TOKEN", "ntfy_topic": "NTFY_TOPIC"}


def load() -> dict:
    return {**read(DEFAULTS), **read(USER)}


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
    if kind == "lines":
        return "\n".join(x.strip() for x in re.split(r"[\n,]", str(value)) if x.strip()) if "tags" in key or "subs" in key or "senders" in key \
            else _checked(key, row, "\n".join(x.strip() for x in str(value).splitlines() if x.strip()))
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


def _checked(key, row, value):
    if key.endswith(PATTERN_ENDINGS) or key == "inbox_subjects":
        try:
            re.compile(to_pattern(value))
        except re.error as e:
            raise ValueError(f"{row[1]}: one of these lines can't be read. Remove any brackets or symbols.") from e
    return value


def save(changes: dict) -> dict:
    user = read(USER)
    defaults = read(DEFAULTS)
    for k, v in changes.items():
        v = validate(k, v)
        if defaults.get(k) == v:
            user.pop(k, None)  # back to default
        else:
            user[k] = v
    USER.write_text(json.dumps(user, indent=2, ensure_ascii=False))
    return load()


def remember(key: str, value) -> None:
    """Gus's own notes, like first run being finished. Kept with your settings so a deploy can't lose them, but not on the form."""
    user = read(USER)
    user[key] = value
    tmp = USER.with_suffix(".tmp")
    tmp.write_text(json.dumps(user, indent=2, ensure_ascii=False))
    tmp.replace(USER)


def secret(key: str) -> str:
    if ENV.get(key) and os.environ.get(ENV[key]):
        return os.environ[ENV[key]]
    v = read(SECRETS).get(key)
    if v:
        return v
    legacy = LEGACY.get(key)
    if legacy and (ROOT / legacy).exists():
        return (ROOT / legacy).read_text().strip()
    return ""


def set_secret(key: str, value: str) -> None:
    if key not in SECRET_KEYS:
        raise ValueError("Unknown secret")
    s = read(SECRETS)
    if value:
        s[key] = value.strip()
    else:
        s.pop(key, None)
    _write_secrets(s)


def _write_secrets(s: dict) -> None:
    tmp = SECRETS.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the first byte
    with os.fdopen(fd, "w") as f:
        json.dump(s, f, indent=2)
    tmp.replace(SECRETS)
    os.chmod(SECRETS, stat.S_IRUSR | stat.S_IWUSR)


MCP_TOKEN = "mcp_token"  # not in SECRET_KEYS: only radar.mcp makes one, and only its hash is kept


def mcp_token() -> dict:
    """What Gus keeps about the token Claude connects with: its hash, last 4 characters, when it was made and whose it is."""
    v = read(SECRETS).get(MCP_TOKEN)
    return v if isinstance(v, dict) else {}


def set_mcp_token(record: dict | None) -> None:
    s = read(SECRETS)
    if record:
        s[MCP_TOKEN] = record
    else:
        s.pop(MCP_TOKEN, None)
    _write_secrets(s)


def secrets_masked() -> dict:
    out = {}
    for k in SECRET_KEYS:
        v = secret(k)
        out[k] = f"set (…{v[-4:]})" if len(v) > 8 else ("set" if v else "")
    return out
