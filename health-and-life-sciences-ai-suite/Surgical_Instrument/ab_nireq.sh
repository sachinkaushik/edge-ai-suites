#!/usr/bin/env bash
set -euo pipefail

# A/B latency runner: runs run_latency.sh N times for each nireq value, parses
# the per-element log, and reports the mean critical-path latency per config so
# we can see past the ~2-3 ms run-to-run GPU jitter.
#
# Usage:   bash ab_nireq.sh                 # nireq 1 vs 2, 3 runs each
#          REPS=5 NIREQS="1 2 4" bash ab_nireq.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
cd "${ROOT_DIR}"

REPS="${REPS:-3}"
NIREQS="${NIREQS:-1 2}"

parse_crit() {
  python3 - "$1" <<'PY'
import re, sys, statistics
rec = re.compile(r"\selement-latency,.*element=\(string\)([^,]+),.*time=\(guint64\)(\d+)")
COMPUTE = ("gvadetect", "gvawatermark", "gvafpscounter")
per = {}
for line in open(sys.argv[1], errors="ignore"):
    m = rec.search(line)
    if not m:
        continue
    ms = int(m.group(2)) / 1e6
    if ms > 60000:
        continue
    per.setdefault(m.group(1), []).append(ms)
crit = sum(statistics.mean(v) for n, v in per.items() if n.startswith(COMPUTE))
det = statistics.mean(per.get("gvadetect0", [0]))
print(f"{crit:.3f} {det:.3f}")
PY
}

declare -A CRIT_ALL
echo "REPS=${REPS}  NIREQS='${NIREQS}'"
echo "============================================================"

for n in ${NIREQS}; do
  crits=()
  for ((i = 1; i <= REPS; i++)); do
    echo ">>> nireq=${n}  run ${i}/${REPS} ..."
    NIREQ="${n}" bash run_latency.sh >/dev/null 2>&1
    read -r crit det < <(parse_crit "${LOG_DIR}/most_optimized.log")
    echo "    critical_path=${crit} ms   gvadetect=${det} ms"
    crits+=("${crit}")
  done
  CRIT_ALL[$n]="${crits[*]}"
done

echo "============================================================"
echo "SUMMARY (mean +/- spread of critical-path ms)"
for n in ${NIREQS}; do
  python3 - "$n" "${CRIT_ALL[$n]}" <<'PY'
import sys, statistics
n = sys.argv[1]
vals = [float(x) for x in sys.argv[2].split()]
mean = statistics.mean(vals)
spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
runs = ", ".join(f"{v:.2f}" for v in vals)
print(f"  nireq={n}:  mean={mean:.3f} ms   stdev={stdev:.3f}   spread={spread:.3f}   runs=[{runs}]")
PY
done
