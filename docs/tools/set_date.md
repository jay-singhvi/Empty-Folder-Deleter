# Change date

Sets the date photos and videos were taken, for example when a camera clock was wrong. New in
0.6.0, with no legacy script. It is also reached from the Timeline (right-click, **Change date…**),
where the dialog shows each file's old and new date before anything is changed.

## Modes

| Mode | New date | Notes |
|---|---|---|
| `exact` | One date and time for every file (`YYYY-MM-DD HH:MM`; a date alone means 12:00). | |
| `month_year` | The month and/or year change; the day and the time of day stay. | The day is cut back to the length of the new month (31 January to February gives 28 or 29). 0 keeps a file's own month or year. |
| `shift` | Every date moves by the same days, hours and minutes (can be negative). | For a camera clock that was wrong. |

Dates must fall between 1980 and 2100.

## How it works

1. **Scan** reads each file's current date the way the media library does (EXIF date, video
   creation time, else the modified time) and writes nothing. Each file that would change is one
   `convert` action with a reason such as `2019-07-04 10:12 → 2021-03-04 10:12`. A file that
   already has the new date is counted, not listed. A OneDrive cloud-only file is skipped and
   reported (it is never read).
2. **Apply** uses the in-place pattern of Strip metadata. A copy with the new date is written
   next to the file, the original goes to the Recycle Bin (its own journal row), the copy takes
   the original's name and gets the new modified time. History can undo the run.

## What is written

* With ExifTool: photos get `DateTimeOriginal`, `CreateDate` and `ModifyDate` (checked by reading
  the copy back); MP4 and QuickTime videos get the QuickTime `CreateDate`, `ModifyDate`,
  `MediaCreateDate`, `MediaModifyDate`, `TrackCreateDate` and `TrackModifyDate`.
* Without ExifTool, or for a file type ExifTool cannot write: only the modified time changes.
  The plan notes say so. The date inside the file stays, so the Timeline keeps showing it.

## Options

`roots` (folders, with `recursive`) and `paths` (single files) say what to change; `mode`,
`new_date`, `year`, `month`, `shift_days`, `shift_hours` and `shift_minutes` say how.

## Safety

Preview first (scan changes nothing); originals go to the Recycle Bin; cloud-only files are never
read or changed; files must be inside the chosen folders, which are never changed themselves;
junctions are not followed; protected folders are refused.
