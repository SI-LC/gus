"""What counts as a role for you: the title and location rules from Settings, and the groups Role Inbox files roles under."""
from __future__ import annotations

import re

from radar.settings import PATTERN_ENDINGS, group_of, groups, load, to_pattern


def load_config() -> dict:
    """Your settings, with every pattern field compiled ready to search with."""
    return {k: re.compile(to_pattern(v), re.I) if k.endswith(PATTERN_ENDINGS) else v for k, v in load().items()}


def left_out(text, cfg, key) -> bool:
    """On the leave-out list, once its exceptions are ignored: "engineer" mustn't catch "product engineer"."""
    keep = cfg.get("title_exclude_except" if key == "title_exclude" else "source_company_except")
    return bool(cfg[key].search(keep.sub(" ", text) if keep else text))


def wanted_title(title, cfg) -> bool:
    return bool(cfg["title_include"].search(title)) and not left_out(title, cfg, "title_exclude")


def matches(job, cfg, hn=False) -> bool:
    title = job["title"]
    if hn:  # HN posts are free text, so test the whole comment
        words = cfg["hn_title_include"] if cfg["hn_title_include"].pattern != "(?!)" else cfg["title_include"]
        return bool(words.search(title + job["description"])) and bool(cfg["location_include"].search(job["description"]))
    place = " / ".join(dict.fromkeys(p.strip() for p in job["location"].split("/")))  # "Remote / Remote" is just remote
    place = place if place.strip() else title  # an ad that gives no place often names it in the title: "Product Manager, London"
    return wanted_title(title, cfg) and bool(cfg["location_include"].search(place))


# --- labels: how Role Inbox groups roles, from Settings -----------------------------------------------

def where(location: str, rows=None) -> str:
    return group_of(location, groups(load()["where_groups"]) if rows is None else rows)


def role_type(title: str, rows=None) -> str:
    return group_of(title, groups(load()["role_kinds"]) if rows is None else rows)
