# Editable project brief

Build a batch archive extractor targeting **Python 3.11.9**.

Use `pathlib` for paths, `argparse` for arguments, Rich for terminal tables/progress,
and external 7-Zip through `asyncio.create_subprocess_exec`. Extract sequentially.
Use **uv** project management with `pyproject.toml` and a generated `uv.lock`.
Pin Python to **3.11.9** in `.python-version` and `requires-python`. Use
`uv sync --locked` for the environment and `uv run --locked python main.py` to run.
Define dependencies in `pyproject.toml`, pinned to **Rich 13.3.1**, **PyYAML 6.0**,
and **Pydantic 2.9.2**. Use Pydantic for configuration validation.
Keep `requirements.txt` only as an optional export, generated with
`uv export --locked --format requirements.txt --no-hashes --output-file requirements.txt`.
After dependency changes, regenerate `uv.lock` and the optional export.
Keep usage instructions in `README.md`.

Place implementation modules (`cli.py`, `config.py`, `matching.py`,
`recognize.py`, and `extract.py`) in `src/`. Keep `main.py` as the root entry point.
Read original archives directly: never copy or move inputs into temporary storage.
Apply replacement renames in their source directory, restoring original filenames
on failure or cancellation. After success, `-s` retains renamed inputs; otherwise
delete them. Refuse rename collisions. Single-layer output goes directly into its
output folder. Create `OUTPUT/temp/` only for runs with `layer > 1`, storing only
intermediate extracted layers there; final layers go to output. Remove the owned
temp directory on completion/failure/cancellation. A pre-existing temp blocks
multilayer runs but is left untouched by single-layer runs.

`pattern.yaml` is a mapping from automatically generated short hex UID to:

```yaml
"a1b2c3d4":
  pattern: 's/^download_(.*)$/\1/'
  layer: 2
  password: ['outer', 'inner']
  type: '7z'  # Optional: 7z | rar | zip; may be omitted or null.
```

User clarification: patterns must be regular expressions and may include a
replacement to rename before extraction. Current syntax is Python `re.search`
for normal regexes and `s/regex/replacement/` (optional `i`) for substitutions.
Support sed-style escaped captures/quantifiers as a fallback for otherwise-invalid
replacement backreferences, e.g. `s/\([a-zA-Z]\+\)\.7z\.exe$/\1.7z.001/`.
Use single backslashes in interactive input and YAML single-quoted strings.
`pattern` must be a single string, never a list. For each file and rule, strip
only the configured `.<type>` suffix if the filename ends with it (case-insensitive).
Otherwise match the whole filename, including any suffix. With `type` omitted,
always match the whole filename. A mismatched type never excludes a file.
Never match directory paths. Volume grouping is separate: test each volume name
and extract its complete group once if any volume matches. Thus `foo.7z.001`
is matched in full even with `type: 7z`; `foo.part01.rar` becomes `foo.part01`
with `type: rar`. Reattach any stripped suffix after substitution. Group planned
names before extraction so a renamed `.7z.exe` -> `.7z.001` joins existing siblings.
Use format detection when no type is supplied. Regexes do not select layers.

Required CLI:

- `-p/--path`: directory to search.
- `-o/--output`: destination, create as needed.
- `-s/--save`: retain original archives after success.
- `-c/--child`: include child directories.
- `-l/--list`: list patterns.
- `-d/--delete UID`: delete a pattern.
- `-a/--add`: guided pattern creation and automatic UID.

Three stages, each with an informational message:

1. Recognizing: match names and multipart groups; show filenames, sizes, passwords.
2. Extracting: process archives one by one, using live 7-Zip output for progress.
3. Ending: validate/normalize folders, delete originals unless saved, report what,
   where, status, and duration.

Each archive produces one top-level directory. Preserve a sole existing folder;
otherwise wrap the files/folders using the archive basename. Preserve existing
output on collisions. Reuse the last password when fewer passwords than layers
are supplied. Inner layers may contain multiple archives and adjacent files;
retain adjacent files and require all nested branches to reach the requested depth.

Implementation decisions: deterministic scanning, skip symlinks and separate
output tree, reject ambiguous UID matches, stage extraction, preserve originals
on failure, and delete all volumes only after successful output placement. Do not
silently overwrite output or discard partial failures. Test on Python 3.11.9 with
real 7-Zip where available, and document limitations honestly.
