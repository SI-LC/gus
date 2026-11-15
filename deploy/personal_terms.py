"""Print your details, one per line: the emails and phone numbers in your profile, your employers and the people you
track. Run from a Gus folder. The scan runs it here, or on the Pi over ssh when your private files aren't on this
machine, and never saves what it prints. Exits 1 when none of the private files is there."""
import json
import re
import sys

terms, found = set(), False
try:
    t = open("profile/profile.md").read()
    found = True
    terms |= set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", t)) | set(re.findall(r"\+?\d[\d ]{9,}\d", t))
except OSError:
    pass
try:
    s = json.load(open("settings.json"))
    found = True
    terms |= {x.strip() for x in s.get("my_employers", "").splitlines() if len(x.strip()) > 3}
except (OSError, ValueError):
    pass
try:
    p = json.load(open("data/people.json"))
    found = True
    terms |= {x["name"] for x in p.values() if x.get("name")}
except (OSError, ValueError):
    pass
print("\n".join(sorted(terms)))
sys.exit(0 if found else 1)
