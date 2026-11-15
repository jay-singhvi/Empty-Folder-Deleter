"""
Find duplicate videos and move them out of the way.

  1. Exact duplicates  - identical bytes (SHA-256)            -> "duplicate"
  2. Same video re-encoded / resized (e.g. WhatsApp copy)     -> "review"
     Matched by duration plus perceptual hashes of 5 frames
     taken at 10/30/50/70/90 % of the clip.

File names are NOT trusted: "(1)" files are sometimes a different phone
recording the same moment, so only content decides.

In every group the best copy stays put:
  highest resolution -> highest bitrate -> name without "(1)"/"copy" -> oldest.

Needs ffmpeg/ffprobe on PATH (Windows: `winget install ffmpeg`)
and:  pip install pillow imagehash numpy

Usage:
  python find_duplicate_videos.py            # dry run: report only
  python find_duplicate_videos.py --move     # actually move files
Every move is logged to video_duplicates_log.csv so it can be undone.
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

import imagehash
from PIL import Image

FOLDER = os.path.dirname(os.path.abspath(__file__))
DUP_DIR = os.path.join(FOLDER, "duplicate")
REVIEW_DIR = os.path.join(FOLDER, "review")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}
SAMPLE_AT = (0.1, 0.3, 0.5, 0.7, 0.9)
DUR_TOL_S = 0.6            # durations must match within this many seconds...
DUR_TOL_PCT = 0.01         # ...or 1 %, whichever is larger
ASPECT_TOL = 0.02
FRAME_MEAN_MAX = 10        # mean pHash distance over sampled frames (256-bit)
FRAME_WORST_MAX = 24       # no single frame may differ more than this

COPY_MARK = re.compile(r"(\(\d+\)|\bcopy\b|-\s*copy)", re.I)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", "-select_streams", "v:0", path],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return float(j["format"]["duration"])


def grab_frame(path, t):
    # the video track can end a little before the audio, so step back if empty
    for _ in range(6):
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
             "-vf", "scale=320:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, check=True).stdout
        if out:
            return Image.open(io.BytesIO(out)).convert("RGB")
        t *= 0.8
    raise RuntimeError(f"no video frame near {t:.2f}s")


def grab_size(path):
    """Display size after rotation (ffmpeg auto-rotates)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "bmp", "-"],
        capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(out)).size


def keep_score(v):
    name = os.path.basename(v["path"])
    return (-(v["w"] * v["h"]), -v["size"] / max(v["dur"], 0.1),
            1 if COPY_MARK.search(name) else 0, len(name), v["mtime"], name)


def scan():
    files = sorted(os.path.join(FOLDER, f) for f in os.listdir(FOLDER)
                   if os.path.isfile(os.path.join(FOLDER, f))
                   and os.path.splitext(f)[1].lower() in VIDEO_EXT)
    vids, errors = [], []
    for i, p in enumerate(files, 1):
        try:
            st = os.stat(p)
            v = {"path": p, "size": st.st_size, "mtime": st.st_mtime}
            v["dur"] = probe(p)
            v["w"], v["h"] = grab_size(p)
            v["frames"] = [imagehash.phash(grab_frame(p, v["dur"] * f), 16) for f in SAMPLE_AT]
            vids.append(v)
        except Exception as e:
            errors.append((p, str(e)[:200]))
        print(f"  scanned {i}/{len(files)}", end="\r")
    print()
    return vids, errors


def same_video(a, b):
    if abs(a["dur"] - b["dur"]) > max(DUR_TOL_S, DUR_TOL_PCT * max(a["dur"], b["dur"])):
        return False
    ar = a["w"] / a["h"]
    if abs(ar - b["w"] / b["h"]) / ar > ASPECT_TOL:
        return False
    d = [x - y for x, y in zip(a["frames"], b["frames"])]
    return sum(d) / len(d) <= FRAME_MEAN_MAX and max(d) <= FRAME_WORST_MAX


def build_plan(vids):
    plan = []
    # pass 1: exact copies (hash only files of equal size)
    by_size = defaultdict(list)
    for v in vids:
        by_size[v["size"]].append(v)
    by_sha = defaultdict(list)
    for same in by_size.values():
        for v in same:
            key = sha256(v["path"]) if len(same) > 1 else ("unique", v["path"])
            by_sha[key].append(v)
    reps = []
    for members in by_sha.values():
        members.sort(key=keep_score)
        reps.append(members[0])
        plan += [(members[0], d, "exact", DUP_DIR) for d in members[1:]]
    # pass 2: same footage, different encode -> review
    reps.sort(key=keep_score)
    leaders = []
    for r in reps:
        for L in leaders:
            if same_video(L, r):
                plan.append((L, r, "re-encode", REVIEW_DIR))
                break
        else:
            leaders.append(r)
    return plan


def main():
    do_move = "--move" in sys.argv
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg/ffprobe not found on PATH.")
    print(f"Scanning {FOLDER}")
    vids, errors = scan()
    plan = build_plan(vids)
    counts = defaultdict(int)
    for *_, kind, _ in plan:
        counts[kind] += 1
    print(f"Videos scanned : {len(vids)}")
    print(f"Unreadable     : {len(errors)}")
    print(f"To 'duplicate' : {counts['exact']}  (exact copies)")
    print(f"To 'review'    : {counts['re-encode']}  (same footage, different file)")
    for p, e in errors:
        print(f"  ERROR {os.path.basename(p)}: {e}")
    if not do_move:
        for k, d, kind, _ in plan:
            print(f"  [{kind:9}] {os.path.basename(d['path'])}  ->  keeps {os.path.basename(k['path'])}")
        print("\nDry run only. Re-run with --move to move files.")
        return
    log_path = os.path.join(FOLDER, "video_duplicates_log.csv")
    new_log = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(["moved_file", "moved_to", "original_name", "kept_original", "match_type"])
        for k, d, kind, folder in plan:
            os.makedirs(folder, exist_ok=True)
            name = os.path.basename(d["path"])
            dest = os.path.join(folder, name)
            base, ext = os.path.splitext(name)
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(folder, f"{base}_dup{n}{ext}"); n += 1
            shutil.move(d["path"], dest)
            w.writerow([os.path.basename(dest), os.path.basename(folder), name,
                        os.path.basename(k["path"]), kind])
    print(f"Moved {len(plan)} files.\nLog: {log_path}")


if __name__ == "__main__":
    main()
