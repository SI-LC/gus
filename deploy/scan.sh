#!/usr/bin/env bash
# The code scan. Runs before every push (.githooks/pre-push), on every pull request (.github/workflows/scan.yml)
# and every deploy. Any failure stops it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
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

if grep -qEf deploy/secret-patterns.txt "$content"; then bad "something that looks like a key"; else good "no keys"; fi

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
if [ ! -f profile/profile.md ] && [ ! -f settings.json ]; then
  echo "  - your details: not checked here, your private files aren't on this machine (the checks before a push cover this)"
elif [ -z "$hits" ]; then good "none of your details"; else bad "personal details found: $hits"; fi

# 4. Risky patterns this project has rules about
grep -rnE 'shell=True|\beval\(|\bexec\(|verify=False|_create_unverified_context' radar --include=*.py && bad "unsafe Python" || good "no unsafe Python patterns"
grep -nE 'href="\$\{esc\(' radar/cockpit.html && bad "link not passed through link()" || good "cockpit links are checked"
grep -qE '"(Read|Edit|Write)"' radar/agent.py && bad "agent has unrestricted file tools" || good "agent tools are limited"
grep -q '"Read(./data/emails.json)"' radar/agent.py && good "web agents can't read email" || bad "web agents can read email"

# 5. Bandit, high severity only (installed outside the repo, only if missing)
venv="${XDG_CACHE_HOME:-$HOME/.cache}/gus-scan"
[ -x "$venv/bin/bandit" ] || { python3 -m venv "$venv" >/dev/null && "$venv/bin/pip" -q install bandit >/dev/null 2>&1; }
if [ -x "$venv/bin/bandit" ]; then
  "$venv/bin/bandit" -q -r radar -lll >/dev/null 2>&1 && good "bandit: nothing high severity" || bad "bandit found high-severity issues (run: $venv/bin/bandit -r radar -lll)"
else
  bad "couldn't install bandit"
fi

# 6. Likely mistakes in the code (rules in ruff.toml, pinned so a new release can't block a deploy by surprise)
"$venv/bin/pip" -q install "ruff==0.17.4" "shellcheck-py==0.11.0.1" >/dev/null 2>&1
if [ -x "$venv/bin/ruff" ] && [ -x "$venv/bin/shellcheck" ]; then
  "$venv/bin/ruff" check -q --no-cache radar tests evals deploy >/dev/null 2>&1 && good "ruff: no likely mistakes" || bad "ruff found likely mistakes (run: $venv/bin/ruff check radar tests evals deploy)"
  "$venv/bin/shellcheck" -S warning run.sh deploy/*.sh >/dev/null 2>&1 && good "shellcheck: no problems in shell scripts" || bad "shellcheck found problems (run: $venv/bin/shellcheck -S warning run.sh deploy/*.sh)"
else
  bad "couldn't install ruff and shellcheck"
fi

# 7. It still works
python3 -m compileall -q radar tests && good "Python compiles" || bad "Python doesn't compile"
if [ -d data ]; then python3 -m radar.schema check >/dev/null 2>&1 && good "data files match docs/objects.md" || bad "data files don't match docs/objects.md (run: python3 -m radar.schema check)"; fi
if out=$(python3 -m unittest discover -s tests 2>&1); then good "tests pass"; else bad "tests fail (python3 -m unittest discover -s tests)"; echo "$out" | grep -v ResourceWarning | tail -30; fi
if command -v node >/dev/null; then
  js="$(mktemp).js"; python3 -c "import re;print('\n'.join(re.findall(r'<script>(.*?)</script>',open('radar/cockpit.html').read(),re.S)))" > "$js"
  node --check "$js" 2>/dev/null && good "cockpit script is valid" || bad "cockpit script has errors"; rm -f "$js"
fi
bash -n run.sh deploy/*.sh && good "shell scripts are valid" || bad "shell script errors"

[ "$fail" = 0 ] && echo "Scan passed" || { echo "Scan FAILED: fix the above before deploying."; exit 1; }
