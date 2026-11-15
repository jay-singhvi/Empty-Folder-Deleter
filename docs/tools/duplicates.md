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

Nothing is fixed in the code. Every value below is an option with a default and a hint (title and
description) in the GUI and in `--help`. 🔒 marks values that were tuned by hand on real data. They
are advanced options, and the GUI warns before one is changed.

| Option | Default | What it does |
|---|---|---|
| Folders | (required) | Folders to check, with their sub-folders. A drive root, a protected folder, or a junction is refused or not followed. |
| What to check (`media`) | both | Photos, videos or both. |
| Exact copies only | off | Finds identical copies only. Skips visual, burst and re-encode matches. |
| Action | move | For exact and visual copies: move them to the duplicate folder, or send them to the Recycle Bin. Burst and re-encode matches always move to the review folder. |
| Duplicate folder | the `duplicate_folder` setting (`duplicate`), inside each folder | Where exact and visual copies go. |
| Review folder | the `review_folder` setting (`review`), inside each folder | Where burst and re-encode matches go. |
| Use the scan cache | on | Reuses each photo's results until its size or modified time changes. The cache lives in the `cache_folder` setting, with one file per combination of the hashing options below, so changing one never reuses old hashes. |
| Workers | the `workers` setting (your CPU count) | How many photos are hashed at the same time. |
| Photo file types (`image_extensions`) | `.bmp .gif .heic .heif .jpeg .jpg .png .tif .tiff .webp` | Extensions treated as photos. Lower-case with a dot. |
| Video file types (`video_extensions`) | `.3gp .avi .m4v .mkv .mov .mp4 .webm` | Extensions treated as videos. A type cannot be both a photo and a video type. |
| Compare only by bytes (`visual_skip_extensions`) | `.gif` | Photo types with no look-alike check, because an animated GIF has no single picture. |
| Burst file-name pattern (`burst_name`) | `^(?:IMG_)?(\d{8}_\d{6})` | Regular expression with exactly one group, the timestamp that the frames of a burst share. |
| Copy-name pattern (`copy_mark`) | `(\(\d+\)\|\bcopy\b\|-\s*copy)` | Regular expression (case-insensitive) for names that look like a copy. Such a name is kept last when copies are otherwise equal. |
| Verify borderline matches with Ollama | off | Asks a local vision model about close visual matches. Only a clear YES confirms a match. See below. |
| Ollama model | `lfm3-vl:latest` | The model used for that check. In the app this is an editable dropdown filled from the models your Ollama server reports (`GET /api/tags`); you can still type a name that is not listed. The command line takes plain text (`--ollama-model`). |
| Ollama question (`ollama_prompt`) | "Image 1 and image 2 are photos. Are they the same photo? Answer only YES or NO." | The question sent with the two photos. Only an answer starting with YES confirms. |
| Ollama picture size (`ollama_image_px`) | 512 | Each photo is shrunk to fit this many pixels before it is sent. |
| Ollama picture quality (`ollama_jpeg_quality`) | 85 | JPEG quality (30 to 100) of the pictures sent. |
| Borderline block difference (`verify_borderline_block`) | 2.0 | A match whose largest 8x8 block differs by more than this, or that sits at a hash limit, is sent to the Ollama check. |
| Pixel-check thumbnail (`thumb_size`) | 128 | Side in pixels of the grey thumbnail for the pixel check. Must be a multiple of 8, 16 to 512. |
| Photo hash grid (`phash_size`) | 16 | Side of the perceptual hash grid for photos (16 means 256 bits). The hash limits were tuned for 16. |
| Video frame hash grid (`video_phash_size`) | 16 | The same for each sampled video frame. |
| Sharpness measure size (`sharpness_px`) | 1600 | A photo is shrunk to fit this many pixels before its sharpness is measured (burst shots keep the sharpest). |
| Video frame width (`frame_width`) | 320 | Sampled frames are scaled to this width before hashing. |
| Frame grab attempts (`frame_retries`) | 6 | Tries for a frame, stepping back in time, when the video track ends early. |
| Frame step-back factor (`frame_step_back`) | 0.8 | Each retry looks at this fraction of the previous time. |
| 🔒 `phash_max` | 2 | Most bits two perceptual hashes may differ. |
| 🔒 `dhash_max` | 4 | Most bits two difference hashes may differ. |
| 🔒 `aspect_tol` | 0.02 | Allowed difference of the width-to-height ratio. |
| 🔒 `burst_phash_max` | 40 | Most bits two burst frames' hashes may differ. |
| 🔒 `pixel_block_max` | 3.0 | Largest average grey difference in any 8x8 block. |
| 🔒 `dur_tol_s`, `dur_tol_pct` | 0.6 s, 0.01 | Video lengths may differ by the larger of the two. |
| 🔒 `frame_mean_max`, `frame_worst_max` | 10, 24 | Average and worst hash difference of the sampled video frames. |
| 🔒 `sample_at` | 0.1, 0.3, 0.5, 0.7, 0.9 | Where frames are taken from a video, as fractions of its length. |

The locked thresholds were tuned on real photos and videos. Raising one finds more look-alikes but
risks false matches (files moved that are not duplicates). Lowering one misses real duplicates.
Hash grid, thumbnail and frame sizes change the hashes themselves, so check the thresholds again
if you change them.

These app-wide settings also apply (Settings page): the duplicate and review folder names, the
cache folder, the number of workers, the protected folders, the hash read size, the `ffprobe`,
frame-grab and Ollama timeouts, and the Ollama address.

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

- The **Trash** action is journaled. Undo from History puts trashed files back from the Recycle
  Bin when it finds them there (same path, same time, same size); otherwise it lists them to
  restore by hand. Moves can be undone from History.
- Videos need `ffprobe`, and the re-encode check needs `ffmpeg`. Without them, the video part is
  skipped with a note, and exact copies of videos are still found when `ffprobe` is present.
  The plan note shows the install command (`winget install Gyan.FFmpeg`, from the program list in
  `core/deps.py`).
- Videos are checked in the folders you choose, including their sub-folders, as photos are.
