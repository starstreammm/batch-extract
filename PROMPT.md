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
  type: '7z'  # Required: 7z | rar | zip.
```

User clarification: patterns must be regular expressions and may include a
replacement to rename before extraction. Current syntax is Python `re.search`
for normal regexes and `s/regex/replacement/` (optional `i`) for substitutions.
Support sed-style escaped captures/quantifiers as a fallback for otherwise-invalid
replacement backreferences, e.g. `s/\([a-zA-Z]\+\)\.7z\.exe$/\1.7z.001/`.
Use single backslashes in interactive input and YAML single-quoted strings.
`pattern` must be a single string, never a list; `type` is required, never null.
First check each filename against the configured type's complete archive/volume
naming style, case-insensitively. If it matches, apply the regex to the basename:
`1111.7z.001` with type `7z` becomes `1111`; `foo.part01.rar` with type `rar`
becomes `foo`. Otherwise match the whole name: `11111.8z` remains `11111.8z`
with type `7z`. A mismatched type never excludes a file. Never match directories.
Test each volume name and extract its complete group once if any volume matches.
Reattach any stripped archive/volume suffix after substitution. Group planned
names before extraction so a renamed `.7z.exe` -> `.7z.001` joins existing siblings.
For every intermediate layer, normalize filenames in place inside `OUTPUT/temp`
to the configured type before extracting. Recognize loose `partNN` and `.NNN.`
markers even when extensions are missing/misleading. Normalize multipart 7z to
`base.7z.001`, ZIP to `base.zip.001`, and RAR to `base.part01.rar`. Preserve valid
native legacy styles. Identify ordinary inner archives by names/signatures, or
attempt a lone opaque file; retain unrelated sidecars. Reject missing/duplicate
volumes and collisions. This renames files, not their actual formats. Regexes
apply only to the outer layer.

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
