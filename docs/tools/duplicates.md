# Duplicate Finder

Finds duplicate photos and videos in the folders you choose, and sets the extra copies aside. It
replaces `find_duplicates` (photos) and `find_duplicate_videos` (videos). It uses the same
thresholds, which were tuned by hand on real photos and videos, so the same files are matched.

## How it works

1. **Scan** finds the duplicates and changes nothing on disk (the scan cache is the only exception).
2. **Preview** lists every copy that would move, with the copy that stays and the reason.
3. **Apply** moves or trashes the selected copies. Each move is journaled, so it can be undone from History.

## What it finds

| Kind | Media | Rule | Goes to |
|---|---|---|---|
| Exact | photos and videos | Identical bytes (SHA-256) | `duplicate` |
| Visual | photos | The same picture, re-saved or resized: two perceptual hashes agree, the aspect ratio matches, the capture times agree when both have one, and no 8×8 block differs too much | `duplicate` |
| Burst | photos | Frames taken in the same second (from the file name) that show the same scene. The sharpest frame stays. | `review` |
| Re-encode | videos | The same footage in another file: duration, aspect ratio and frames sampled along the clip match | `review` |

In each group, the copy that stays is the largest picture (or video frame), then the largest file
(or the highest bitrate for videos), then a name without `(1)` or `copy`, then the oldest.
File names are not trusted for matching. Only the content decides.

## Options

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders to check, with their sub-folders. A drive root, a protected folder, or a junction is refused or not followed. |
| Media | both | Photos, videos or both. |
| Exact copies only | off | Finds identical copies only. Skips visual, burst and re-encode matches. |
| Action | move | For exact and visual copies: move them to the duplicate folder, or send them to the Recycle Bin. Burst and re-encode matches always move to the review folder. |
| Duplicate folder | `duplicate` inside each folder | Where exact and visual copies go. |
| Review folder | `review` inside each folder | Where burst and re-encode matches go. |
| Use the scan cache | on | Reuses each photo's results until its size or modified time changes. |
| Workers | your CPU count | How many photos are hashed at the same time. |
| Verify borderline matches with Ollama | off | Asks a local vision model about close visual matches. Only a clear YES confirms a match. See below. |
| Ollama model | `lfm3-vl:latest` | The model used for that check. |
| Thresholds (advanced) | tuned values | `phash_max` 2, `dhash_max` 4, `aspect_tol` 0.02, `burst_phash_max` 40, `pixel_block_max` 3.0, `dur_tol_s` 0.6, `dur_tol_pct` 0.01, `frame_mean_max` 10, `frame_worst_max` 24, `sample_at` 0.1, 0.3, 0.5, 0.7, 0.9. Change them only if you know why. |

## Ollama check

Optional. When it is on, each close visual match is shown to a local Ollama vision model, which
answers whether the two pictures are the same photo. Only a clear YES confirms the match. Errors
and a missing model count as "not confirmed", so nothing moves on a failed check. If Ollama cannot
be reached, borderline matches are not moved, and the plan says so.

## Safety

- Cloud-only OneDrive files are never read or hashed. They are listed as "skipped: cloud-only".
- The duplicate and review folders are never scanned, so a second run does not see moved files.
- A file that cannot be opened is listed as unreadable, and it is never moved.
- The folder you chose is never moved or trashed. Junctions and symlinks are not followed.
- Protected folders (Windows, Program Files, your user folder, drive roots) are refused.
- Apply checks every action again, and a name that is already taken is never overwritten.

## Limits

- The **Trash** action is journaled, but History cannot undo it. Restore those files from the
  Recycle Bin by hand. Moves can be undone from History.
- Videos need `ffprobe`, and the re-encode check needs `ffmpeg`. Without them, the video part is
  skipped with a note, and exact copies of videos are still found when `ffprobe` is present.
  Install both with `winget install Gyan.FFmpeg`.
- Videos are checked in the folders you choose, including their sub-folders, as photos are.
