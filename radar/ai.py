"""One way to call an AI model, whichever provider you picked in Settings.

claude_subscription  Claude Code CLI, logged in with your Claude plan
anthropic_api        Claude Code CLI with your Anthropic API key (no login needed, e.g. on a Pi)
openai_compatible    any /chat/completions endpoint: OpenAI, OpenRouter, Ollama… (no tools)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from datetime import date

from radar import usage
from radar.scan import DATA, ROOT, load_config
from radar.settings import secret


class BudgetReached(RuntimeError):
    """Today's AI limit is used up; runs resume tomorrow or when the limit is raised."""


class LoginExpired(RuntimeError):
    """The Claude sign-in Gus uses has expired; nothing that needs AI works until you sign in again from the cockpit."""


SIGN_IN = "Sign in again from Settings → AI in the cockpit."
EXPIRED_FLAG = DATA / "login_expired"  # {"since": ..., "notified": date}: there while the last Claude run failed for want of a login
_EXPIRED = re.compile(r"oauth|authenticat|logged in|log in|/login|session expired", re.I)


def login_expired() -> dict | None:
    """When the last Claude run failed because the sign-in had expired, or None when AI last worked."""
    try:
        return json.loads(EXPIRED_FLAG.read_text())
    except (OSError, ValueError):
        return None


def _login_failed(detail: str) -> LoginExpired:
    """Record that the sign-in has expired and push once a day until a run works again."""
    from datetime import datetime
    from radar.notify import notify, cockpit_url
    state = login_expired() or {"since": datetime.now().astimezone().isoformat(timespec="seconds")}
    today = str(date.today())
    if state.get("notified") != today:
        state["notified"] = today
        notify("Claude sign-in expired", "", f"Gus can't score roles or run agents until you sign in again. {SIGN_IN}",
               click=f"{cockpit_url().rstrip('/')}/#settings/ai" if cockpit_url() else "")
    EXPIRED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    EXPIRED_FLAG.write_text(json.dumps(state))
    return LoginExpired(f"Claude sign-in has expired. {SIGN_IN} ({detail.strip()[:200]})")


def login_works():
    """A Claude run (or a fresh sign-in) worked, so the warning comes down."""
    try:
        EXPIRED_FLAG.unlink()
    except OSError:
        pass


def _check_budget(cfg):
    limit = float(cfg.get("ai_daily_budget_usd") or 0)  # 0 means no AI runs at all, never "no limit"
    if usage.today_cost() >= limit:
        flag = DATA / "budget_notified"
        today = str(date.today())
        if not flag.exists() or flag.read_text() != today:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(today)
            from radar.notify import notify
            notify("Gus: AI paused", "", "AI runs are off (limit $0)." if not limit else f"AI runs are paused until tomorrow (limit ${limit:g}).")
        raise BudgetReached("AI runs are off: the daily limit is $0" if not limit else f"Daily AI limit of ${limit:g} reached")


# Small answers only: no tools, project files, session history or extended thinking. Thinking alone was 3 of the 10 seconds.
QUICK = ["--strict-mcp-config", "--setting-sources", "", "--no-session-persistence", "--tools", "", "--effort", "low",
         "--system-prompt", "You answer with JSON only."]


class NeedsTools(Exception):
    """The chosen provider can't search the web or edit files."""


def _claude_env(provider):
    env = dict(os.environ)
    if provider == "anthropic_api":
        key = secret("anthropic_api_key")
        if not key:
            raise RuntimeError("Anthropic API key not set (Settings → AI)")
        env["ANTHROPIC_API_KEY"] = key
    else:
        env.pop("ANTHROPIC_API_KEY", None)  # use the subscription login, not a stray key
    return env


def run(prompt: str, task: str = "scoring", tools: list[str] | None = None, timeout: int = 900,
        deny: list[str] | None = None, budget: bool = True) -> str:
    """task picks the model (scoring | people | agent | quick). tools are Claude Code tool rules."""
    cfg = load_config()
    if budget:
        _check_budget(cfg)
    provider = cfg.get("ai_provider", "claude_subscription")
    if provider == "openai_compatible":
        if tools:
            raise NeedsTools(f"{task} needs web search and tools, which only the Claude providers have")
        return _openai(prompt, cfg)
    model = cfg.get(f"model_{task}", "sonnet")
    args = ["claude", "-p", "--model", model, "--output-format", "json"]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    if deny:
        args += ["--disallowedTools", ",".join(deny)]
    if task == "quick":
        args += QUICK
    out = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout,
                         cwd=tempfile.gettempdir() if task == "quick" else ROOT,
                         env={**_claude_env(provider), "MAX_THINKING_TOKENS": "0"} if task == "quick" else _claude_env(provider))
    try:
        d = json.loads(out.stdout)
    except ValueError:
        d = {}
    if d:
        u = d.get("usage") or {}
        usage.record(kind="ai", task=task, provider=provider, model=model, cost_usd=d.get("total_cost_usd"),
                     input_tokens=(u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                     output_tokens=u.get("output_tokens"), searches=(u.get("server_tool_use") or {}).get("web_search_requests"),
                     duration_ms=d.get("duration_ms"), ok=not d.get("is_error"))
    if out.returncode != 0 or d.get("is_error"):
        detail = (d.get("result") or out.stderr or out.stdout).strip()[:300]
        if provider == "claude_subscription" and _EXPIRED.search(detail):
            raise _login_failed(detail)
        hint = " (if it says you're not logged in, sign in again from Settings → AI)" if provider == "claude_subscription" else " (check Settings → AI)"
        raise RuntimeError(f"Claude Code failed{hint}: {detail}")
    if provider == "claude_subscription":
        login_works()
    return d.get("result") or out.stdout


def _openai(prompt, cfg):
    base = cfg.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if secret("openai_api_key"):
        headers["Authorization"] = f"Bearer {secret('openai_api_key')}"
    body = {"model": cfg.get("openai_model"), "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    u = d.get("usage") or {}
    usage.record(kind="ai", task="scoring", provider="openai_compatible", model=cfg.get("openai_model"), cost_usd=None,
                 input_tokens=u.get("prompt_tokens"), output_tokens=u.get("completion_tokens"), ok=True)
    return d["choices"][0]["message"]["content"]


def status() -> dict:
    """Is AI ready? For a Claude login this answers in a blink, with no AI used. Anything less clear gets a real test."""
    provider = load_config().get("ai_provider", "claude_subscription")
    if provider == "openai_compatible":
        return test()
    if not shutil.which("claude"):
        return {"ok": False, "reply": "Claude Code isn't installed on this machine."}
    if provider == "anthropic_api":
        return test() if secret("anthropic_api_key") else {"ok": False, "reply": "No API key yet."}
    if login_expired():  # `claude auth status` still says yes with a saved login that no longer refreshes
        return {"ok": False, "reply": f"Your Claude sign-in has expired. {SIGN_IN}"}
    try:
        out = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=8, env=_claude_env(provider))
        return {"ok": bool(json.loads(out.stdout).get("loggedIn")), "reply": "Not logged in to Claude yet."}
    except (OSError, ValueError, subprocess.SubprocessError):
        return test()


_login = None
LOGIN_MINUTES = 10


def login_start() -> dict:
    """Start Claude's own sign-in. With no terminal it gives a link, and the sign-in page gives back a code to paste, so this
    works from the cockpit even on a machine with no browser. Gus never sees your password."""
    global _login
    if not shutil.which("claude"):
        return {"ok": False, "reply": "Claude Code isn't installed on this machine."}
    if _login and _login.poll() is None:
        _login.kill()
    _login = subprocess.Popen(["claude", "auth", "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=_claude_env("claude_subscription"))
    timer = threading.Timer(LOGIN_MINUTES * 60, _login.kill)  # a sign-in you walked away from doesn't wait for ever
    timer.daemon = True
    timer.start()
    for line in _login.stdout:
        found = re.search(r"https://\S+", line)
        if found:
            return {"ok": True, "url": found[0]}
    return {"ok": False, "reply": "Claude's sign-in didn't start. Try again."}


def login_finish(code: str) -> dict:
    if not (_login and _login.poll() is None):
        return {"ok": False, "reply": "That sign-in has ended. Start again."}
    try:
        _login.stdin.write(code.strip() + "\n")
        _login.stdin.flush()
        _login.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        _login.kill()
        return {"ok": False, "reply": "That didn't work. Start again and paste the new code."}
    if _login.returncode != 0:
        return {"ok": False, "reply": "Claude didn't accept that code. Start again to get a new one."}
    login_works()
    return status()


def test() -> dict:
    """Small round-trip used by the cockpit's Test connection button."""
    try:
        text = run('Reply with exactly: {"ok": true}', task="scoring", timeout=120, budget=False)
        return {"ok": '"ok"' in text, "reply": text.strip()[:200]}
    except Exception as e:
        return {"ok": False, "reply": str(e)[:300]}
