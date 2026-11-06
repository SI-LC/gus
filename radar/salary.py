"""Lower a role's score when its stated salary is under your line. Roles with no stated salary are left alone."""
from __future__ import annotations

import math
import re

MAX_DROP = 5
LOW, HIGH = 20_000, 500_000  # outside this it's a day rate, a funding round or a typo, not a yearly salary
CONTEXT = re.compile(r"salary|per year|per annum|\bp\.?a\b|package|compensation|\bbase\b|up to|\bdoe\b|\bote\b", re.I)


def _amount(text: str) -> int:
    n = float(text.lower().replace(",", "").rstrip("k"))
    return int(n * 1000 if text.lower().endswith("k") or n < 1000 else n)


def stated(job: dict, symbol: str = "£") -> int | None:
    """The top of the advertised range, from a real figure the source gave or the wording of the ad. Never an estimate."""
    if job.get("salary_max"):
        return int(job["salary_max"])
    money = r"\d{2,3}(?:,\d{3}|k)\b"
    range_ = re.compile(rf"{re.escape(symbol)}\s?({money})(?:\s*(?:-|–|—|to)\s*{re.escape(symbol)}?\s?({money}))?", re.I)
    text = job.get("description") or ""
    tops = []
    for m in range_.finditer(text):
        if CONTEXT.search(text[max(0, m.start() - 60): m.end() + 40]):
            tops.append(_amount(m.group(2) or m.group(1)))
    tops = [t for t in tops if LOW <= t <= HIGH]
    return max(tops) if tops else None


def drop(top: int | None, floor: int) -> int:
    """One point per 10% under the line, or part of it, up to MAX_DROP."""
    if not floor or not top or top >= floor:
        return 0
    return min(MAX_DROP, math.ceil((floor - top) / floor * 10))


def apply(job: dict, score: int, cfg: dict) -> tuple[int, str]:
    """The score after the salary check, and a flag explaining any change."""
    symbol = cfg.get("salary_symbol")
    if not symbol:  # without a currency, a figure in an ad can't be compared with your line
        return score, ""
    top = stated(job, symbol)
    points = drop(top, int(cfg.get("salary_floor") or 0))
    if not points:
        return score, ""
    return max(1, score - points), f"Salary up to {symbol}{top:,}: score lowered by {points}"
