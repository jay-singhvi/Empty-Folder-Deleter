# Scheduled runs

Run a saved preset on a schedule through Windows Task Scheduler. A scheduled run does what you
would get from `various-tools TOOL apply --preset NAME --yes`, with extra checks because nobody is
there to answer (see below). Every run is journaled and can be undone from History like any other
apply.

## Commands

```
various-tools schedule add NAME --tool TOOL --preset PRESET --daily 09:00
various-tools schedule add NAME --tool TOOL --preset PRESET --weekly MON,THU 18:30
various-tools schedule add NAME --tool TOOL --preset PRESET --monthly 1 07:00
various-tools schedule list
various-tools schedule run NAME        # start it once now
various-tools schedule remove NAME
```

Times are 24-hour `HH:MM`. Weekdays are `MON TUE WED THU FRI SAT SUN` (full names work too).
The day of the month is 1 to 28, so the run happens in every month. Names use letters, digits,
spaces and `. _ ( ) -`.

Save the preset first, for example `various-tools presets save empty_folders Weekly C:\Downloads`.
The preset must name the folders to work on.

## What `schedule add` does

1. Checks that the tool and the preset exist and that the preset is complete.
2. Refuses a preset that deletes permanently (`delete_mode = permanent`). Scheduled runs only ever
   send items to the Recycle Bin.
3. Refuses a preset that changes a locked option away from its tuned default. A scheduled run has
   nobody to ask, so these values are never accepted unattended.
4. Refuses a preset that replaces, removes or downloads original files: any option the tool marks
   as doing that, set away from its default. Today these are `media_resize` `--output overwrite`
   and `--remove-raw-originals`, `metadata_strip` `--in-place`, `media_convert`
   `--trash-originals`, and `organize_by_date` `--also-resize` and `--cloud-only`. Run unattended,
   such a preset would do it again on every run (for example a percentage resize that replaces the
   originals would shrink the same pictures again each time and recycle the previous version,
   until the Recycle Bin purges the true original).
5. Shows the tool, the preset, when it runs and the exact command line, says that every run is
   journaled and can be undone, and asks you to confirm. `--yes` answers that question only. With
   no terminal and no `--yes`, it refuses.

## Every run checks again

A schedule is a standing apply: each run uses the preset and the settings as they are at that
time and changes files without asking. The task's command ends with the hidden `--scheduled`
marker (in place of `--yes`, which it implies). Under that marker `apply`:

- checks the preset again with the same rules as `schedule add` (points 2 to 4), before anything
  is scanned or changed, so a preset edited later is refused instead of run;
- refuses the whole run, changing nothing, when the plan would delete anything permanently, even
  if the settings now allow permanent deletes, and whatever `--allow-locked` says;
- accepts only `--preset` (no `--plan`, option flags, folders or `--select-all`).

`schedule run` checks the preset again before it starts, and refuses a task made by an older
version without the marker: remove it and add it again.

## How it is set up

- The task is created in the `Various Tools` folder of the Task Scheduler library, for your own
  account only. It runs only while you are logged on and needs no administrator rights or password.
  Nothing outside that folder is ever listed, started or removed.
- The command is `various-tools.exe TOOL apply --preset PRESET --scheduled` from an installed
  build. From a source checkout it is the current Python with `-m various_tools.cli.main`. When you
  use a non-default settings file (`--config FILE`), the task gets the same `--config FILE`.
- An existing task is never overwritten; remove it first.
- Task Scheduler limits the command to 261 characters, so a very long preset name or install path
  is refused.

## After a run

Look at `various-tools history` (or the History page) for what each run did, and
`various-tools undo RUN_ID` to reverse it. A run that is still applying cannot be undone yet; a
run that never finished and wrote nothing to the journal for two hours (or longer than the
transcode timeout plus half an hour, if that is longer) counts as stopped and can be undone.
`schedule list` also shows the last run time and the last result code that Task Scheduler
recorded (`0` means the program finished normally).
