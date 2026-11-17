"""Run every test file at once, each in its own process, and show what the failing ones printed.
    python3 deploy/run_tests.py              all of tests/test_*.py
    python3 deploy/run_tests.py test_jobs    just the ones named
Each process loads tests/test_offline.py first, so no file can use the internet. The scan (deploy/scan.sh) runs this."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(name):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "tests"), os.environ.get("PYTHONPATH")])), "PYTHONUNBUFFERED": "1"}
    done = subprocess.run([sys.executable, "-m", "unittest", "test_offline", name], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return name, done.returncode, done.stdout


def main(names):
    names = [n[:-3] if n.endswith(".py") else n for n in names] or sorted(p.stem for p in (ROOT / "tests").glob("test_*.py") if p.stem != "test_offline")
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        results = list(pool.map(run, names))
    failed = [(name, out) for name, code, out in results if code]
    for name, out in failed:
        print(f"=== {name}\n{out}")
    print(f"{len(results) - len(failed)} of {len(results)} test files passed" + ("".join(f"\n  failed: {name}" for name, _ in failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
