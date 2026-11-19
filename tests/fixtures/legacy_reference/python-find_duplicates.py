"""
Find duplicate images and move them into a "duplicate" subfolder.

Folders to scan are read from find_duplicates_input_list.txt, which sits in the
same folder as this script. One path per line; quotes are optional, blank lines
and lines starting with # are ignored.

Two passes:
  1. Exact duplicates   - identical bytes (SHA-256).
  2. Visual duplicates  - same picture re-saved / resized / different format,
                          detected with perceptual hashes (pHash + dHash).
                          Both hashes must agree, the aspect ratio must match
                          and the capture time (EXIF DateTimeOriginal) must be
                          the same when both files have one. The capture-time
                          check stops separate shots of the same scene from
                          being flagged.
In every group the "best" copy stays put:
  highest resolution -> largest file -> name without "(1)"/"copy" -> oldest.

Burst mode: photos taken in the same second (from the file name) that show
the same scene are grouped with a looser check. The sharpest frame stays
put and the rest go to "review" so you can confirm before deleting.
Exact copies of any file still go to "duplicate".

Usage:
  python find_duplicates.py                   # dry run: report only
  python find_duplicates.py --move            # actually move files
  python find_duplicates.py --explain A B     # show why two files do / don't match
  python find_duplicates.py --verify          # also ask a local Ollama vision model about borderline matches
  python find_duplicates.py --verify --model qwen3-vl:2b
Every run writes find_duplicates_review_list_<date_time>.txt next to this script.
The review list gives the reason for every move.
Every move is logged to duplicates_log.csv so it can be undone.

Scaling:
  - Images are hashed in parallel, one worker process per CPU core.
  - Scan results are cached in find_duplicates_cache.sqlite next to this script,
    keyed by path, size and modified time. Re-runs only hash new or changed
    files, and an interrupted scan picks up where it stopped.
  - Visual matching only compares images that share a pHash band (pigeonhole
    principle), instead of every image against every other. The result is the
    same as comparing all pairs.
"""
import base64
import csv
import functools
import hashlib
import io
import json
import multiprocessing as mp
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
import zlib
from collections import defaultdict
from datetime import datetime

import numpy as np
from PIL import Image, ImageFilter, ImageOps
import imagehash

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_LIST = os.path.join(SCRIPT_DIR, "find_duplicates_input_list.txt")
CACHE_PATH = os.path.join(SCRIPT_DIR, "find_duplicates_cache.sqlite")
CACHE_VERSION = 2                # bump when the hashing logic or cached fields change

BASE = os.path.join(os.path.expanduser("~"), "OneDrive", "Pictures")   # duplicate/, review/ and the log live here
DUP_DIR = os.path.join(BASE, "duplicate")
REVIEW_DIR = os.path.join(BASE, "review")
LOG_PATH = os.path.join(BASE, "duplicates_log.csv")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
VISUAL_SKIP_EXT = {".gif"}      # animated - exact match only
PHASH_MAX = 2                   # max Hamming distance (256-bit hashes); was 4
DHASH_MAX = 4                   # was 6
ASPECT_TOL = 0.02               # 2 % aspect-ratio tolerance
BURST_PHASH_MAX = 40            # looser match for frames of the same burst
PIXEL_BLOCK_MAX = 3.0           # max avg grey-level diff in any 8x8 block of a
                                # 128x128 thumbnail; re-saves score well below 3,
                                # different shots of the same scene score higher
THUMB = 128
BANDS = PHASH_MAX + 1           # a pHash within PHASH_MAX bits matches exactly on >= 1 band
EXIF_IFD = 0x8769               # Exif sub-IFD holding the capture time
TAG_DATETIME_ORIGINAL = 36867

OLLAMA_URL = "http://localhost:11434"
DEFAULT_VERIFY_MODEL = "lfm3-vl:latest"   # ~17 s per pair on this machine; qwen3-vl:4b took over 10 min
VERIFY_BORDERLINE_BLOCK = 2.0   # visual matches above this block diff (or near a hash limit) get a model check
VERIFY_MODEL = None             # set by --verify; None = no model check
VERIFY_STATS = {"checked": 0, "rejected": 0, "errors": 0}

COPY_MARK = re.compile(r"(\(\d+\)|\bcopy\b|-\s*copy)", re.I)
BURST_NAME = re.compile(r"^(?:IMG_)?(\d{8}_\d{6})")   # timestamp to the second


# ---------- hashing helpers ----------

def to_int(h):
    """imagehash array -> 256-bit int, so Hamming distance is an XOR + popcount."""
    return int.from_bytes(np.packbits(h.hash.flatten()).tobytes(), "big")


def hamming(a, b):
    return (a ^ b).bit_count()


def band_keys(h):
    edges = [256 * i // BANDS for i in range(BANDS + 1)]
    return [(i, (h >> edges[i]) & ((1 << (edges[i + 1] - edges[i])) - 1))
            for i in range(BANDS)]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_time(im):
    """EXIF DateTimeOriginal as a string, or None when the photo has none."""
    value = im.getexif().get_ifd(EXIF_IFD).get(TAG_DATETIME_ORIGINAL)
    return str(value).strip() if value else None


def hash_image(path):
    """Runs in a worker process: every feature needed for the comparison."""
    st = os.stat(path)
    info = {"path": path, "size": st.st_size, "mtime": st.st_mtime,
            "sha": sha256(path), "phash": None, "dhash": None, "sharp": None,
            "thumb": None, "taken": None}
    with Image.open(path) as im:
        info["w"], info["h"] = im.size
        info["taken"] = capture_time(im)
        if os.path.splitext(path)[1].lower() not in VISUAL_SKIP_EXT:
            im = ImageOps.exif_transpose(im).convert("RGB")
            info["w"], info["h"] = im.size
            info["phash"] = to_int(imagehash.phash(im, hash_size=16))
            info["dhash"] = to_int(imagehash.dhash(im, hash_size=16))
            grey = im.convert("L")
            info["thumb"] = np.asarray(grey.resize((THUMB, THUMB), Image.LANCZOS),
                                       dtype=np.uint8).tobytes()
            grey.thumbnail((1600, 1600))
            info["sharp"] = float(np.asarray(
                grey.filter(ImageFilter.FIND_EDGES), dtype=np.float32).var())
    return info


def hash_safely(path):
    try:
        return hash_image(path), None
    except Exception as e:
        return None, (path, str(e))


# ---------- scan cache ----------

def open_cache():
    conn = sqlite3.connect(CACHE_PATH)
    if conn.execute("PRAGMA user_version").fetchone()[0] != CACHE_VERSION:
        conn.execute("DROP TABLE IF EXISTS files")
        conn.execute(f"PRAGMA user_version = {CACHE_VERSION}")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
                        path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha TEXT,
                        w INTEGER, h INTEGER, phash TEXT, dhash TEXT, sharp REAL,
                        thumb BLOB, taken TEXT)""")
    return conn


def hex_or_none(v):
    return None if v is None else format(v, "064x")


def from_row(row):
    path, size, mtime, sha, w, h, phash, dhash, sharp, taken = row
    return {"path": path, "size": size, "mtime": mtime, "sha": sha, "w": w, "h": h,
            "phash": None if phash is None else int(phash, 16),
            "dhash": None if dhash is None else int(dhash, 16),
            "sharp": sharp, "taken": taken}


def make_thumb_loader(conn):
    """Thumbnails stay in the cache file, not in RAM; load them only when compared."""
    @functools.lru_cache(maxsize=20000)
    def load_thumb(path):
        blob, = conn.execute("SELECT thumb FROM files WHERE path = ?", (path,)).fetchone()
        return thumb_array(zlib.decompress(blob))
    return load_thumb


def thumb_array(raw):
    return np.frombuffer(raw, dtype=np.uint8).reshape(THUMB, THUMB).astype(np.float32)


# ---------- file discovery and scanning ----------

def read_input_list():
    if not os.path.exists(INPUT_LIST):
        sys.exit(f"Input list not found: {INPUT_LIST}")
    roots = []
    with open(INPUT_LIST, encoding="utf-8-sig") as fh:
        for line in fh:
            p = line.strip().strip('"').strip()
            if not p or p.startswith("#"):
                continue
            if not os.path.isdir(p):
                print(f"  WARNING folder not found, skipped: {p}")
                continue
            roots.append(p)
    if not roots:
        sys.exit("No valid folders in the input list.")
    return roots


def find_files(roots):
    """(path, size, mtime) for every image under roots (never inside duplicate/review)."""
    skip = {os.path.normcase(DUP_DIR), os.path.normcase(REVIEW_DIR)}
    seen, files, errors = set(), [], []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if os.path.normcase(os.path.join(dirpath, d)) not in skip]
            for f in filenames:
                p = os.path.abspath(os.path.join(dirpath, f))
                if os.path.splitext(f)[1].lower() not in IMAGE_EXT:
                    continue
                if os.path.normcase(p) in seen:       # overlapping roots count once
                    continue
                seen.add(os.path.normcase(p))
                try:
                    st = os.stat(p)
                except OSError as e:
                    errors.append((p, str(e)))
                    continue
                files.append((p, st.st_size, st.st_mtime))
    return sorted(files), errors


def scan(files, conn):
    """Hashes new or changed files in parallel; the rest come from the cache."""
    cached = {row[0]: row for row in conn.execute(
        "SELECT path, size, mtime, sha, w, h, phash, dhash, sharp, taken FROM files")}
    present = {p for p, _, _ in files}
    conn.executemany("DELETE FROM files WHERE path = ?",
                     [(p,) for p in cached if p not in present])

    infos, todo = [], []
    for p, size, mtime in files:
        row = cached.get(p)
        if row and row[1] == size and row[2] == mtime:
            infos.append(from_row(row))
        else:
            todo.append(p)
    print(f"  from cache: {len(infos)}   to hash: {len(todo)}")

    errors = []
    if todo:
        with mp.Pool(os.cpu_count()) as pool:
            for done, (info, err) in enumerate(
                    pool.imap_unordered(hash_safely, todo, chunksize=16), 1):
                if err:
                    errors.append(err)
                else:
                    thumb = info.pop("thumb")
                    conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        info["path"], info["size"], info["mtime"], info["sha"],
                        info["w"], info["h"], hex_or_none(info["phash"]),
                        hex_or_none(info["dhash"]), info["sharp"],
                        zlib.compress(thumb, 1) if thumb else None, info["taken"]))
                    infos.append(info)
                if done % 500 == 0:
                    conn.commit()
                    print(f"  hashed {done}/{len(todo)}")
    conn.commit()
    return infos, errors


# ---------- comparison ----------

def keep_score(info):
    """Lower tuple sorts first = the copy we keep."""
    name = os.path.basename(info["path"])
    return (-(info["w"] * info["h"]), -info["size"],
            1 if COPY_MARK.search(name) else 0, len(name), info["mtime"], name)


def same_burst(a, b):
    """Two different shots taken in the same second (burst mode)."""
    ma = BURST_NAME.match(os.path.basename(a["path"]))
    mb = BURST_NAME.match(os.path.basename(b["path"]))
    return bool(ma and mb and ma.group(1) == mb.group(1))


def burst_key(info):
    m = BURST_NAME.match(os.path.basename(info["path"]))
    return m.group(1) if m else None


def same_capture(A, B):
    """False only when both photos have a capture time and the times differ."""
    return A["taken"] is None or B["taken"] is None or A["taken"] == B["taken"]


def cheap_match(A, B):
    """Everything except the pixel check: aspect ratio and both hashes."""
    if A["phash"] is None or B["phash"] is None:
        return False
    ar = A["w"] / A["h"]
    return (abs(ar - B["w"] / B["h"]) / ar <= ASPECT_TOL
            and hamming(A["phash"], B["phash"]) <= PHASH_MAX
            and hamming(A["dhash"], B["dhash"]) <= DHASH_MAX)


def max_block_diff(a, b):
    """Largest mean abs difference over 8x8 blocks - catches local motion."""
    return float(np.abs(a - b).reshape(16, 8, 16, 8).mean(axis=(1, 3)).max())


def visual_reason(L, r, block):
    return (f"pHash {hamming(L['phash'], r['phash'])}/{PHASH_MAX}, "
            f"dHash {hamming(L['dhash'], r['dhash'])}/{DHASH_MAX}, "
            f"block diff {block:.2f}/{PIXEL_BLOCK_MAX}, "
            f"taken {L['taken'] or '?'} vs {r['taken'] or '?'}")


def find_visual_match(r, leaders, bands, load_thumb):
    """(index, reason) of the first leader (in keep order) that looks like r, or None."""
    if r["phash"] is None:
        return None
    candidates = set()
    for key in band_keys(r["phash"]):
        candidates.update(bands.get(key, ()))
    for idx in sorted(candidates):
        L = leaders[idx]
        if same_burst(L, r) or not cheap_match(L, r) or not same_capture(L, r):
            continue
        block = max_block_diff(load_thumb(L["path"]), load_thumb(r["path"]))
        if block > PIXEL_BLOCK_MAX:
            continue
        reason = visual_reason(L, r, block)
        if VERIFY_MODEL and is_borderline(L, r, block):
            if not model_confirms(L, r):
                continue
            reason += f", {VERIFY_MODEL} confirmed"
        return idx, reason
    return None


def is_borderline(L, r, block):
    """Matches close to a limit are the ones worth a second opinion."""
    return (block > VERIFY_BORDERLINE_BLOCK
            or hamming(L["phash"], r["phash"]) >= PHASH_MAX
            or hamming(L["dhash"], r["dhash"]) >= DHASH_MAX - 1)


def jpeg_b64(path, size=512):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ask_model(a_path, b_path):
    body = {
        "model": VERIFY_MODEL, "stream": False, "think": False,
        "options": {"temperature": 0, "num_predict": 16},
        "messages": [{
            "role": "user",
            "content": "Image 1 and image 2 are photos. Are they the same photo? Answer only YES or NO.",
            "images": [jpeg_b64(a_path), jpeg_b64(b_path)],
        }],
    }
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        answer = json.load(resp)["message"]["content"]
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip().upper()
    return answer.startswith("YES")


def model_confirms(L, r):
    """True only when the model says YES. Errors count as not confirmed, so nothing moves on a failed check."""
    VERIFY_STATS["checked"] += 1
    try:
        same = ask_model(L["path"], r["path"])
    except Exception as e:
        VERIFY_STATS["errors"] += 1
        print(f"  model error, not moved: {os.path.basename(r['path'])}: {e}")
        return False
    if not same:
        VERIFY_STATS["rejected"] += 1
    return same


def check_ollama(model):
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as resp:
            names = {m["name"] for m in json.load(resp)["models"]}
    except Exception as e:
        sys.exit(f"Ollama is not reachable at {OLLAMA_URL}: {e}")
    if model not in names and f"{model}:latest" not in names:
        sys.exit(f"Model {model} is not installed. Run: ollama pull {model}")


def build_plan(infos, load_thumb):
    """Returns [(kept_copy, file_to_move, match_type, destination_folder, reason)]."""
    plan = []
    # pass 1: exact copies (identical bytes) -> duplicate
    by_sha = defaultdict(list)
    for inf in infos:
        by_sha[inf["sha"]].append(inf)
    reps = []
    for members in by_sha.values():
        members.sort(key=keep_score)
        reps.append(members[0])
        plan += [(members[0], d, "exact", DUP_DIR, "identical bytes (SHA-256)")
                 for d in members[1:]]

    # pass 2: same picture re-saved / resized -> duplicate
    # (burst frames are kept as leaders here; pass 3 handles them)
    reps.sort(key=keep_score)
    leaders = []
    bands = defaultdict(list)      # (band, value) -> leader indexes
    for r in reps:
        match = find_visual_match(r, leaders, bands, load_thumb)
        if match is not None:
            idx, reason = match
            plan.append((leaders[idx], r, "visual", DUP_DIR, reason))
            continue
        leaders.append(r)
        if r["phash"] is not None:
            for key in band_keys(r["phash"]):
                bands[key].append(len(leaders) - 1)

    # pass 3: burst frames -> keep the sharpest, rest go to review
    bursts = defaultdict(list)
    for L in leaders:
        if burst_key(L) and L["phash"] is not None:
            bursts[burst_key(L)].append(L)
    for frames in bursts.values():
        frames.sort(key=lambda f: -f["sharp"])
        best = []
        for f in frames:
            for b in best:
                if (b["w"], b["h"]) == (f["w"], f["h"]) and hamming(b["phash"], f["phash"]) <= BURST_PHASH_MAX:
                    reason = (f"burst {burst_key(f)}, pHash {hamming(b['phash'], f['phash'])}"
                              f"/{BURST_PHASH_MAX}, sharper frame kept")
                    plan.append((b, f, "burst", REVIEW_DIR, reason))
                    break
            else:
                best.append(f)
    return plan


# ---------- explain ----------

def explain(path_a, path_b):
    """Prints every check the visual pass would make for two files."""
    A, B = hash_image(path_a), hash_image(path_b)
    print(f"A: {A['path']}\n   {A['w']}x{A['h']}  taken {A['taken'] or '(no EXIF)'}")
    print(f"B: {B['path']}\n   {B['w']}x{B['h']}  taken {B['taken'] or '(no EXIF)'}\n")
    if A["phash"] is None or B["phash"] is None:
        sys.exit("Not a visual-comparable image (animated GIF or unreadable).")

    ar = A["w"] / A["h"]
    ar_diff = abs(ar - B["w"] / B["h"]) / ar
    block = max_block_diff(thumb_array(A["thumb"]), thumb_array(B["thumb"]))
    checks = [
        ("aspect ratio diff", f"{ar_diff * 100:.2f}%", f"<= {ASPECT_TOL * 100:.0f}%",
         ar_diff <= ASPECT_TOL),
        ("pHash distance", hamming(A["phash"], B["phash"]), f"<= {PHASH_MAX}",
         hamming(A["phash"], B["phash"]) <= PHASH_MAX),
        ("dHash distance", hamming(A["dhash"], B["dhash"]), f"<= {DHASH_MAX}",
         hamming(A["dhash"], B["dhash"]) <= DHASH_MAX),
        ("block diff (max 8x8)", f"{block:.2f}", f"<= {PIXEL_BLOCK_MAX}", block <= PIXEL_BLOCK_MAX),
        ("capture time", f"{A['taken'] or '?'} vs {B['taken'] or '?'}", "same (or missing)",
         same_capture(A, B)),
    ]
    for name, value, rule, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {str(value):<42} {rule}")
    verdict = all(ok for *_, ok in checks)
    print(f"\nVisual match: {'YES - would be moved to duplicate' if verdict else 'NO'}")


# ---------- output ----------

def write_review_list(path, stamp, do_move, roots, counts, n_scanned, errors, rows):
    """rows: (kind, file, destination, kept_copy, reason) for every planned or done move."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Find duplicates - review list\n")
        fh.write(f"Created : {datetime.strptime(stamp, '%Y%m%d_%H%M%S'):%Y-%m-%d %H:%M:%S}\n")
        fh.write(f"Mode    : {'MOVE (files were moved)' if do_move else 'DRY RUN (nothing was moved)'}\n")
        fh.write(f"Rules   : visual = pHash<={PHASH_MAX}, dHash<={DHASH_MAX}, "
                 f"block diff<={PIXEL_BLOCK_MAX}, same capture time (when both have one)\n")
        if VERIFY_MODEL:
            fh.write(f"Model   : {VERIFY_MODEL} checked borderline pairs "
                     f"({VERIFY_STATS['checked']} checked, {VERIFY_STATS['rejected']} rejected, "
                     f"{VERIFY_STATS['errors']} errors)\n")
        fh.write("\n")
        fh.write(f"Folders scanned ({len(roots)}):\n")
        fh.writelines(f"  {r}\n" for r in roots)
        fh.write(f"\nImages scanned : {n_scanned}\n")
        fh.write(f"Unreadable     : {len(errors)}\n")
        fh.write(f"To 'duplicate' : {counts['exact'] + counts['visual']}  "
                 f"(exact {counts['exact']}, visual {counts['visual']})\n")
        fh.write(f"To 'review'    : {counts['burst']}  (burst frames)\n")

        for title, folder_kinds in (("TO DUPLICATE", ("exact", "visual")), ("TO REVIEW", ("burst",))):
            fh.write(f"\n{'=' * 70}\n{title}\n{'=' * 70}\n")
            section = [r for r in rows if r[0] in folder_kinds]
            if not section:
                fh.write("(none)\n")
            for kind, src, dest, keeper, reason in section:
                fh.write(f"[{kind}] {src}\n")
                fh.write(f"    {'moved to' if do_move else 'would move to'}: {dest}\n")
                fh.write(f"    keeps   : {keeper}\n")
                fh.write(f"    why     : {reason}\n")

        if errors:
            fh.write(f"\n{'=' * 70}\nUNREADABLE FILES\n{'=' * 70}\n")
            for p, e in errors:
                fh.write(f"{p}\n    {e}\n")


def main():
    global VERIFY_MODEL
    if "--explain" in sys.argv:
        i = sys.argv.index("--explain")
        if len(sys.argv) < i + 3:
            sys.exit("Usage: python find_duplicates.py --explain FILE_A FILE_B")
        explain(sys.argv[i + 1], sys.argv[i + 2])
        return

    if "--verify" in sys.argv:
        VERIFY_MODEL = (sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv
                        else DEFAULT_VERIFY_MODEL)
        check_ollama(VERIFY_MODEL)

    do_move = "--move" in sys.argv
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(SCRIPT_DIR, f"find_duplicates_review_list_{stamp}.txt")

    roots = read_input_list()
    print(f"Scanning {len(roots)} folders (with sub-folders)")
    files, errors = find_files(roots)
    print(f"Images found   : {len(files)}")

    conn = open_cache()
    infos, scan_errors = scan(files, conn)
    errors += scan_errors
    plan = build_plan(infos, make_thumb_loader(conn))
    conn.close()

    counts = defaultdict(int)
    for _, _, kind, _, _ in plan:
        counts[kind] += 1
    print(f"\nImages scanned : {len(infos)}")
    print(f"Unreadable     : {len(errors)}")
    print(f"To 'duplicate' : {counts['exact'] + counts['visual']}  "
          f"(exact {counts['exact']}, visual {counts['visual']})")
    print(f"To 'review'    : {counts['burst']}  (burst frames)")
    if VERIFY_MODEL:
        print(f"Model check ({VERIFY_MODEL}): {VERIFY_STATS['checked']} borderline pairs, "
              f"{VERIFY_STATS['rejected']} rejected, {VERIFY_STATS['errors']} errors")

    rows = []
    if do_move:
        new_log = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_log:
                w.writerow(["moved_file", "moved_to", "original_name", "kept_original", "match_type",
                            "original_path", "kept_path"])
            for keeper, d, kind, folder, reason in plan:
                os.makedirs(folder, exist_ok=True)
                name = os.path.basename(d["path"])
                dest = os.path.join(folder, name)
                base, ext = os.path.splitext(name)
                k = 1
                while os.path.exists(dest):
                    dest = os.path.join(folder, f"{base}_dup{k}{ext}"); k += 1
                shutil.move(d["path"], dest)
                w.writerow([os.path.basename(dest), os.path.basename(folder), name,
                            os.path.basename(keeper["path"]), kind, d["path"], keeper["path"]])
                rows.append((kind, d["path"], dest, keeper["path"], reason))
        print(f"Moved {len(plan)} files.\nLog: {LOG_PATH}")
    else:
        rows = [(kind, d["path"], os.path.join(folder, os.path.basename(d["path"])), keeper["path"], reason)
                for keeper, d, kind, folder, reason in plan]
        print("\nDry run only. Re-run with --move to move files.")

    write_review_list(report_path, stamp, do_move, roots, counts, len(infos), errors, rows)
    print(f"Review list: {report_path}")


if __name__ == "__main__":
    main()
