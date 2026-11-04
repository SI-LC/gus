#!/usr/bin/env bash
# The code scan. Runs before every push (.githooks/pre-push) and every deploy. Any failure stops it.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
bad() { echo "  ✗ $1"; fail=1; }
good() { echo "  ✓ $1"; }

echo "Code scan"
# 1. Nothing personal in tracked files
tracked=$(git ls-files | grep -E '^(data/|content/|profile/profile\.md$|watchlist\.csv$|settings\.json$|\.secrets\.json$|\.x_token$|\.ntfy_topic$|\.cockpit_token$|\.companies_house_key$|\.env)' || true)
[ -z "$tracked" ] && good "no personal files tracked" || bad "personal files tracked: $tracked"

# 2. Everything that would be published: tracked files, plus every commit not yet on GitHub
range=$(git rev-parse --verify -q origin/main >/dev/null && echo "origin/main..HEAD" || echo "HEAD")
content=$(mktemp); trap 'rm -f "$content"' EXIT
git grep -I -h -e '' -- . > "$content" 2>/dev/null
git log -p --format='%an %ae %s%n%b' "$range" >> "$content"

if grep -qE 'sk-ant-[A-Za-z0-9_-]{20}|sk-[A-Za-z0-9]{32}|gh[pousr]_[A-Za-z0-9]{30}|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{20}|BEGIN [A-Z ]*PRIVATE KEY' "$content"; then bad "something that looks like a key"; else good "no keys"; fi

# 3. Your details: contact info, employers and the people you're tracking, read from your private files
hits=$(CONTENT="$content" python3 - <<'PY'
import json, os, re
terms = set()
try:
    t = open("profile/profile.md").read()
    terms |= set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", t)) | set(re.findall(r"\+?\d[\d ]{9,}\d", t))
except OSError:
    pass
try:
    terms |= {x.strip() for x in json.load(open("settings.json")).get("my_employers", "").splitlines() if len(x.strip()) > 3}
except (OSError, ValueError):
    pass
try:
    terms |= {p["name"] for p in json.load(open("data/people.json")).values() if p.get("name")}
except (OSError, ValueError):
    pass
text = open(os.environ["CONTENT"], errors="ignore").read()
print("\n".join(sorted(t for t in terms if t in text)))
PY
)
[ -z "$hits" ] && good "none of your details" || bad "personal details found: $hits"

# 4. Risky patterns this project has rules about
grep -rnE 'shell=True|\beval\(|\bexec\(|verify=False|_create_unverified_context' radar --include=*.py && bad "unsafe Python" || good "no unsafe Python patterns"
grep -nE 'href="\$\{esc\(' radar/cockpit.html && bad "link not passed through link()" || good "cockpit links are checked"
grep -qE '"(Read|Edit|Write)"' radar/agent.py && bad "agent has unrestricted file tools" || good "agent tools are limited"

# 5. Bandit, high severity only (installed outside the repo, only if missing)
venv="${XDG_CACHE_HOME:-$HOME/.cache}/pm-radar-scan"
[ -x "$venv/bin/bandit" ] || { python3 -m venv "$venv" >/dev/null && "$venv/bin/pip" -q install bandit >/dev/null 2>&1; }
if [ -x "$venv/bin/bandit" ]; then
  "$venv/bin/bandit" -q -r radar -lll >/dev/null 2>&1 && good "bandit: nothing high severity" || bad "bandit found high-severity issues (run: $venv/bin/bandit -r radar -lll)"
else
  bad "couldn't install bandit"
fi

# 6. It still works
python3 -m compileall -q radar tests && good "Python compiles" || bad "Python doesn't compile"
python3 -m unittest discover -s tests >/dev/null 2>&1 && good "tests pass" || bad "tests fail (python3 -m unittest discover -s tests)"
if command -v node >/dev/null; then
  js="$(mktemp).js"; python3 -c "import re;print('\n'.join(re.findall(r'<script>(.*?)</script>',open('radar/cockpit.html').read(),re.S)))" > "$js"
  node --check "$js" 2>/dev/null && good "cockpit script is valid" || bad "cockpit script has errors"; rm -f "$js"
fi
bash -n run.sh deploy/*.sh && good "shell scripts are valid" || bad "shell script errors"

[ "$fail" = 0 ] && echo "Scan passed" || { echo "Scan FAILED: fix the above before deploying."; exit 1; }
