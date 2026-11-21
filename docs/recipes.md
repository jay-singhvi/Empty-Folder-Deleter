# Recipes

A recipe is a named, ordered list of steps. A step is a tool id, that tool's saved options
(only the values that differ from the defaults) and an optional note.

## Storage

`recipes.json` in the same folder as `settings.toml`, with a format version (`1`). It is written
through a temporary file and `os.replace`. If it cannot be read (bad JSON, unknown format, a
recipe without a name) it is renamed to `recipes.json.bad-YYYYMMDD-HHMMSS`, the app or command
says so, and the list starts empty. Nothing is dropped silently.

## The working folder

A step does not store the tool's folder option. It is filled from the working folder when the
recipe runs. A step that was saved with "Use other folders" keeps its own folders. The CLI takes
the folders as arguments. Permanent-delete values are never saved in a step; the step falls back
to the option's default, so a permanent delete is always chosen again on purpose.

## Needs attention

A step is "needs attention" when its tool is not available or its options no longer validate
against the tool's options model. The whole recipe is blocked until every step is fixed, so a
recipe never applies half of itself because of a broken later step.

## Running

GUI: each step is a normal tool page built from the step's options and scanned at once, so the
results list, Apply window, cloud-only banner and safety questions are the usual ones. The tool
page's remembered options are not changed by a recipe. CLI: `recipe run NAME PATHS` scans the
first step only (later steps depend on it); with `--apply` each step is scanned, shown,
confirmed (`--yes` skips the question; changed locked options still need `--allow-locked`) and
applied. A step with nothing selected, a refusal, a cancel or a failure stops the recipe.
Pacing flags (`--batch-size`, `--pause`, `--wait-for-upload`, `--upload-timeout`) work as on
`apply`.

## History

Each applied step is its own journal run. Its options JSON (as stored in the journal only) gets
`"_recipe": {"name", "run_id", "step", "steps"}`; the plan and the tool's options are not
changed, so exports and re-runs are unaffected. History labels such runs and offers **Undo this
recipe run**: after one question, the steps are undone from last to first with the normal undo
rules; a step still being applied refuses the whole undo, a step that cannot be undone is
reported and the others go on.
