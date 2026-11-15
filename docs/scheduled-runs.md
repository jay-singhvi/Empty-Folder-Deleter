# Scheduled runs

Run a saved preset on a schedule through Windows Task Scheduler. A scheduled run does exactly what
you would get from `various-tools TOOL apply --preset NAME --yes`, so every run is journaled and
can be undone from History like any other apply.

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
4. Shows the tool, the preset, when it runs and the exact command line, says that every run is
   journaled and can be undone, and asks you to confirm. `--yes` answers that question only. With
   no terminal and no `--yes`, it refuses.

A schedule is a standing apply: each run uses the preset as it is at that time and changes files
without asking. If you edit the preset later so that it deletes permanently or changes a locked
value, the run stops instead of asking (a locked change is refused when nobody can answer), and
`schedule run` checks the preset again before it starts.

## How it is set up

- The task is created in the `Various Tools` folder of the Task Scheduler library, for your own
  account only. It runs only while you are logged on and needs no administrator rights or password.
  Nothing outside that folder is ever listed, started or removed.
- The command is `various-tools.exe TOOL apply --preset PRESET --yes` from an installed build. From
  a source checkout it is the current Python with `-m various_tools.cli.main`. When you use a
  non-default settings file (`--config FILE`), the task gets the same `--config FILE`.
- An existing task is never overwritten; remove it first.
- Task Scheduler limits the command to 261 characters, so a very long preset name or install path
  is refused.

## After a run

Look at `various-tools history` (or the History page) for what each run did, and
`various-tools undo RUN_ID` to reverse it. `schedule list` also shows the last run time and the
last result code that Task Scheduler recorded (`0` means the program finished normally).
