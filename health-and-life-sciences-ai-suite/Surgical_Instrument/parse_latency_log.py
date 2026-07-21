#!/usr/bin/env python3
import argparse
import json
import re
import statistics
from pathlib import Path

def pct_nearest_rank(vals, p):
    if not vals:
        return None
    k = max(1, int((p / 100.0) * len(vals) + 0.5))
    return vals[min(k - 1, len(vals) - 1)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log_file")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--txt-out", default=None)
    ap.add_argument(
        "--element",
        default=None,
        help=(
            "Parse per-element latency for the named element (e.g. gvadetect0) "
            "from element-latency records instead of whole-pipeline latency. "
            "Substring match on the element name."
        ),
    )
    args = ap.parse_args()

    log_path = Path(args.log_file)
    vals_ms = []

    if args.element:
        # element-latency, element-id=(string)..., element=(string)NAME,
        # src=(string)..., time=(guint64)NNN, ts=(guint64)MMM;
        elem_re = re.compile(
            r"\selement-latency,.*element=\(string\)([^,]+),.*time=\(guint64\)(\d+)"
        )
        with log_path.open("r", errors="ignore") as f:
            for line in f:
                m = elem_re.search(line)
                if m and args.element in m.group(1):
                    ms = int(m.group(2)) / 1e6
                    # Drop guint64 underflow wraps (negative latency -> ~2^64).
                    if ms > 60_000.0:
                        continue
                    vals_ms.append(ms)
        if not vals_ms:
            raise SystemExit(
                f"No element-latency records for element '{args.element}'. "
                "Run with GST_TRACERS='latency(flags=element)'."
            )
    else:
        with log_path.open("r", errors="ignore") as f:
            for line in f:
                if " element-latency," in line:
                    continue
                m = re.search(r"\slatency,.*time=\(guint64\)(\d+)", line)
                if m:
                    vals_ms.append(int(m.group(1)) / 1e6)
        if not vals_ms:
            raise SystemExit("No whole-pipeline latency records found in log.")

    vals_ms.sort()

    result = {
        "log_file": str(log_path),
        "element": args.element or "pipeline",
        "samples": len(vals_ms),
        "mean_ms": round(statistics.mean(vals_ms), 3),
        "p50_ms": round(pct_nearest_rank(vals_ms, 50), 3),
        "p90_ms": round(pct_nearest_rank(vals_ms, 90), 3),
        "p95_ms": round(pct_nearest_rank(vals_ms, 95), 3),
        "p99_ms": round(pct_nearest_rank(vals_ms, 99), 3),
        "max_ms": round(vals_ms[-1], 3),
    }

    txt = (
        f"log_file: {result['log_file']}\n"
        f"element : {result['element']}\n"
        f"samples : {result['samples']}\n"
        f"mean_ms : {result['mean_ms']}\n"
        f"p50_ms  : {result['p50_ms']}\n"
        f"p90_ms  : {result['p90_ms']}\n"
        f"p95_ms  : {result['p95_ms']}\n"
        f"p99_ms  : {result['p99_ms']}\n"
        f"max_ms  : {result['max_ms']}\n"
    )

    print(txt, end="")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2) + "\n")
    if args.txt_out:
        Path(args.txt_out).write_text(txt)

if __name__ == "__main__":
    main()
