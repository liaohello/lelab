#!/usr/bin/env python3
"""Run a package's own pytest suite inside the package's own environment.

The runner is deliberately dumb: it clears `PYTHONPATH`, points it at the
unpacked tree, and lets pytest decide. Results are read back from JUnit XML so
that skips carry their reasons and failures carry their first line - the report
should say *why*, not just "3 failed".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def summarise(junit: Path) -> dict:
    if not junit.is_file():
        return {"error": "no junit xml produced"}
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    cases = []
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, 0) or 0)
        for case in suite.iter("testcase"):
            item = {
                "name": case.get("name"),
                "classname": case.get("classname"),
                "time": float(case.get("time", 0) or 0),
                "outcome": "passed",
            }
            for child in case:
                if child.tag in ("failure", "error"):
                    item["outcome"] = child.tag
                    text = (child.get("message") or "") or (child.text or "")
                    item["message"] = " ".join(text.split())[:600]
                    break
                if child.tag == "skipped":
                    item["outcome"] = "skipped"
                    item["message"] = " ".join(((child.get("message") or "") or (child.text or "")).split())[:300]
            cases.append(item)
    return {"counts": counts, "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path, help="unpacked project dir")
    parser.add_argument("--tree", required=True, type=Path, help="payload root")
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--targets", default="", help="comma separated test paths, relative to the project")
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=7200.0)
    args = parser.parse_args()

    python = args.env / "bin/python"
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    env.update(
        {
            "PYTHONPATH": ":".join(
                # The ROS profile ships an ament_python package next to the
                # backend; without its parent on sys.path every test module that
                # imports `lelab_mobile_agent` fails at collection.
                str(path)
                for path in (
                    args.project,
                    args.tree / "lelab_mobile_agent",
                    args.tree / "lerobot" / "src",
                )
                if path.is_dir()
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "HF_HUB_OFFLINE": "1",
        }
    )
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    argv = [str(python), "-m", "pytest", *targets, "-q", "--tb=short",
            "-p", "no:cacheprovider", f"--junitxml={args.junit}"]
    started = time.time()
    with args.log.open("w") as handle:
        handle.write(f"$ {' '.join(argv)}\n(cwd={args.project})\n\n")
        handle.flush()
        done = subprocess.run(argv, cwd=args.project, env=env, stdout=handle,
                              stderr=subprocess.STDOUT, timeout=args.timeout)
    parsed = summarise(args.junit)
    report = {
        "label": args.label,
        "argv": argv,
        "cwd": str(args.project),
        "env_python": str(python),
        "pythonpath": env["PYTHONPATH"],
        "exit_code": done.returncode,
        "seconds": round(time.time() - started, 1),
        "log": str(args.log),
        "junit": str(args.junit),
        **parsed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"label": report["label"], "exit": report["exit_code"],
                      "counts": parsed.get("counts"), "seconds": report["seconds"]}, indent=2))
    for case in parsed.get("cases", []):
        if case["outcome"] != "passed":
            print(f"  {case['outcome'].upper():7} {case['name']}: {case.get('message', '')[:160]}")
    return 0 if done.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
