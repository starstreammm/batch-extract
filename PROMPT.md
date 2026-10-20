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
Store all temporary copies and intermediate extraction files in `OUTPUT/temp/`
and remove this directory when the run finishes, including failures or cancellation.
Do not delete a pre-existing `OUTPUT/temp`; report it and stop extraction instead.

`pattern.yaml` is a mapping from automatically generated short hex UID to:

```yaml
"a1b2c3d4":
  pattern: ['^example$', 's/^download_(.*)$/\1/']
  layer: 2
  password: ['outer', 'inner']
  type: '7z'  # 7z | rar | zip
```

User clarification: patterns must be regular expressions and may include a
replacement to rename before extraction. Current syntax is Python `re.search`
for normal regexes and `s/regex/replacement/` (optional `i`) for substitutions.
Patterns are alternatives matching the outer filename after stripping archive
and volume suffixes, never directory paths. Rename only temporary copies; restore
missing archive suffixes using `type`. Regexes do not select individual layers.

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
