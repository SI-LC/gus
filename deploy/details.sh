#!/usr/bin/env bash
# Part of the code scan: none of your details (contacts, employers, the people you track) is in what would be published.
#   bash deploy/details.sh <file with everything that would be published>
# Your details are read from your private files here, or from the Pi ($GUS_PI, default sipi) when they aren't on this
# machine. They're kept in memory only. If neither is possible the check fails, so a push from a laptop can't skip it.
# On GitHub (CI set) there's nothing to read, and the push before it already ran this. GUS_PI= (empty) means "there is
# no Pi": only for a copy of Gus with no private data anywhere.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
content="$1"
pi="${GUS_PI-sipi}"

if [ -f profile/profile.md ] || [ -f settings.json ] || [ -f data/people.json ]; then
  terms=$(python3 deploy/personal_terms.py)
elif [ -n "${CI:-}" ]; then
  echo "  - your details: not checked on GitHub, your private files aren't here (the scan before each push covers this)"; exit 0
elif [ -z "$pi" ]; then
  echo "  - your details: not checked, no private files here and GUS_PI is empty (no Pi)"; exit 0
elif ! terms=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$pi" 'cd gus && python3 -' < deploy/personal_terms.py 2>/dev/null); then
  echo "  ✗ couldn't check your details: they aren't on this machine and $pi didn't answer with them (is the Pi on? set GUS_PI if it has another name)"; exit 1
fi

hits=$(TERMS="$terms" python3 -c '
import os, sys
text = open(sys.argv[1], errors="ignore").read()
print("\n".join(t for t in os.environ["TERMS"].splitlines() if t and t in text))' "$content")
n=$(grep -c . <<< "$terms")
if [ -z "$hits" ]; then echo "  ✓ none of your details ($n checked)"; else echo "  ✗ personal details found: $hits"; exit 1; fi
