import os, datetime, re
from collections import defaultdict

folder = "."
snap_pattern = re.compile(r'^Snapchat-\d+\.(jpg|jpeg|png|mp4|mov|mkv)$', re.IGNORECASE)

files = []
for f in os.listdir(folder):
    if snap_pattern.match(f):
        path = os.path.join(folder, f)
        mtime = os.path.getmtime(path)
        ext = f.rsplit('.', 1)[1].lower()
        files.append((f, mtime, ext))

files.sort(key=lambda x: x[1])

timestamp_counts = defaultdict(int)
renames = []

for orig_name, mtime, ext in files:
    dt = datetime.datetime.fromtimestamp(mtime)
    ts_key = dt.strftime("%Y%m%d-%H_%M")
    timestamp_counts[ts_key] += 1
    counter = timestamp_counts[ts_key]
    new_name = f"Snapchat-{dt.strftime('%Y%m%d')}-{dt.strftime('%H_%M')}-{counter:03d}.{ext}"
    renames.append((orig_name, new_name))

print(f"Found {len(renames)} Snapchat files to rename:\n")
print(f"{'Original Name':<35} → {'New Name'}")
print("-" * 80)
for orig, new in renames:
    print(f"{orig:<35} → {new}")

print("\nRenaming...")
for orig, new in renames:
    os.rename(os.path.join(folder, orig), os.path.join(folder, new))
    print(f"  ✓ {orig} → {new}")

print(f"\nDone! Renamed {len(renames)} files.")
