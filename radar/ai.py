"""One way to call an AI model, whichever provider you picked in Settings.

claude_subscription  Claude Code CLI, logged in with your Claude plan
anthropic_api        Claude Code CLI with your Anthropic API key (no login needed, e.g. on a Pi)
openai_compatible    any /chat/completions endpoint: OpenAI, OpenRouter, Ollama… (no tools)
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import date

from radar import usage
from radar.scan import DATA, ROOT, load_config
from radar.settings import secret


class BudgetReached(RuntimeError):
    """Today's AI limit is used up; runs resume tomorrow or when the limit is raised."""


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


class NeedsTools(Exception):
    """The chosen provider can't search the web or edit files."""


def _claude_env(provider):
    env = dict(os.environ)
    if provider == "anthropic_api":
        key = secret("anthropic_api_key")
        if not key:
            raise RuntimeError("Anthropic API key not set (Settings → Keys)")
        env["ANTHROPIC_API_KEY"] = key
    else:
        env.pop("ANTHROPIC_API_KEY", None)  # use the subscription login, not a stray key
    return env


def run(prompt: str, task: str = "scoring", tools: list[str] | None = None, timeout: int = 900,
        deny: list[str] | None = None, budget: bool = True) -> str:
    """task picks the model (scoring | people | agent). tools are Claude Code tool rules."""
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
    out = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout,
                         cwd=ROOT, env=_claude_env(provider))
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
        hint = " (run `claude` and /login)" if provider == "claude_subscription" else ""
        raise RuntimeError(f"Claude Code failed{hint}: {(d.get('result') or out.stderr or out.stdout).strip()[:300]}")
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


def test() -> dict:
    """Small round-trip used by the cockpit's Test connection button."""
    try:
        text = run('Reply with exactly: {"ok": true}', task="scoring", timeout=120, budget=False)
        return {"ok": '"ok"' in text, "reply": text.strip()[:200]}
    except Exception as e:
        return {"ok": False, "reply": str(e)[:300]}
