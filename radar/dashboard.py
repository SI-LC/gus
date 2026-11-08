"""Render data/jobs.json as a self-contained local dashboard: data/dashboard.html."""
from __future__ import annotations

import json

from radar.scan import DATA, JOBS, ROOT
from radar.store import read

OUT = DATA / "dashboard.html"


def main():
    store = read(JOBS)
    jobs = [{k: v for k, v in j.items() if k != "description"} for j in store.values()]
    template = (ROOT / "radar" / "dashboard.html").read_text()
    OUT.write_text(template.replace("/*JOBS*/[]", json.dumps(jobs, ensure_ascii=False).replace("</", "<\\/")))
    print(OUT)


if __name__ == "__main__":
    main()
