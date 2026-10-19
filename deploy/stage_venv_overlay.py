"""Stage the venv OVERLAY: what the source half added on top of the deps half (#1782).

The final image assembles its venv from two layers — a large, stable one built
from ``uv.lock`` alone, and this small one built from cairn source. Docker cannot
diff *inside* a layer, so before the split every deploy re-exported the whole
~3.5GB venv to ship ~7MB of changed Python (409s of a ~13 min deploy, measured
on the prod box 2026-08-31).

The overlay is computed from installed METADATA rather than a file-tree diff: for
every distribution present after the workspace sync but absent before it, the
wheel's own ``RECORD`` names exactly the files it installed. That is the wheel
format's answer to "what is this package", so the overlay cannot drift from what
uv actually did.

A version-CHANGED distribution is REFUSED, not copied. Docker layers add and
replace files but cannot remove them, so laying v2 over v1 leaves v1's orphaned
files behind and the image silently runs a mixed install. If that ever happens
the fix is ordering — which package installs on which side of the boundary — and
the build says which distribution forced the question, the same posture as the
#1806 verify gates that caught the tokenizers clobber.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys


def site_packages(venv: str) -> str:
    lib = os.path.join(venv, "lib")
    for entry in sorted(os.listdir(lib)):
        cand = os.path.join(lib, entry, "site-packages")
        if os.path.isdir(cand):
            return cand
    raise SystemExit(f"[overlay] no site-packages under {lib}")


def record_path(sp: str, name: str) -> str | None:
    norm = name.replace("-", "_").lower()
    for entry in sorted(os.listdir(sp)):
        # PEP 427 escapes the NAME's dashes to underscores but keeps the dash
        # before the version, so only the name is normalized — normalizing the
        # whole entry would eat that separator and match nothing.
        if entry.endswith(".dist-info") and entry.lower().startswith(norm + "-"):
            rec = os.path.join(sp, entry, "RECORD")
            return rec if os.path.exists(rec) else None
    return None


def stage(venv: str, out: str, before_dists: dict, after_dists: dict,
          before_bin: list) -> tuple[int, int, int]:
    changed = sorted(n for n, v in after_dists.items()
                     if n in before_dists and before_dists[n] != v)
    if changed:
        raise SystemExit(
            "[overlay] REFUSING to split: these distributions changed version across "
            "the stage boundary, and a layered overlay cannot remove the old files:\n  "
            + "\n  ".join(f"{n}: {before_dists[n]} -> {after_dists[n]}" for n in changed)
            + "\nInstall them on ONE side of the boundary (see #1782/#1806)."
        )

    sp = site_packages(venv)
    added = sorted(n for n in after_dists if n not in before_dists)
    copied = absent = 0
    for name in added:
        rec = record_path(sp, name)
        if rec is None:
            raise SystemExit(f"[overlay] {name} has no RECORD — cannot determine its files")
        with open(rec, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if not row or not row[0]:
                    continue
                src = os.path.normpath(os.path.join(sp, row[0]))
                if not os.path.isfile(src):
                    absent += 1  # RECORD lists files an install may not have written
                    continue
                dst = os.path.join(out, os.path.relpath(src, venv))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1

    # Console scripts (cairn-server) are written into .venv/bin by the sync and are
    # not RECORD paths under site-packages, so they are collected by difference.
    binsrc = os.path.join(venv, "bin")
    if os.path.isdir(binsrc):
        known = set(before_bin)
        for f in sorted(os.listdir(binsrc)):
            if f in known:
                continue
            os.makedirs(os.path.join(out, "bin"), exist_ok=True)
            shutil.copy2(os.path.join(binsrc, f), os.path.join(out, "bin", f))
            copied += 1
    return len(added), copied, absent


def main(argv: list[str]) -> int:
    venv, before_json, after_json, before_bin_json, out = argv[1:6]
    before = {d["name"]: d["version"] for d in json.load(open(before_json))}
    after = {d["name"]: d["version"] for d in json.load(open(after_json))}
    n_dists, copied, absent = stage(
        venv, out, before, after, json.load(open(before_bin_json)))
    print(f"[overlay] {n_dists} distributions, {copied} files staged"
          + (f" ({absent} RECORD entries absent on disk)" if absent else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
