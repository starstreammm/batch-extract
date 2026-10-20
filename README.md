# batch-extract

A Python **3.11.9** command-line project that finds archives using regex rules,
extracts them sequentially through external **7-Zip**, and organizes each result
in one top-level folder. Uses `pathlib`, `argparse`, `asyncio.create_subprocess_exec`,
PyYAML **6.0**, Rich **13.3.1**, and Pydantic **2.9.2** for configuration validation.

## Setup

Install `uv` and a 7-Zip executable supporting 7z, RAR, and ZIP. Make
`7z` or `7zz` available on your PATH, or pass `--seven-zip /path/to/7zz`.
`pyproject.toml` defines the project and its dependencies; `uv.lock` locks the
resolved versions. Run these commands from the project directory:

```sh
uv python install 3.11.9
uv sync --locked
uv run --locked python --version  # should print Python 3.11.9
uv run --locked python main.py --help
```

`uv sync` creates and manages `.venv`; `uv run` uses it without shell activation,
including on Windows. `.python-version` and `requires-python` select **3.11.9**.
Keep `pyproject.toml` and `uv.lock` in version control. After changing dependencies
in `pyproject.toml`, run `uv lock`, then `uv sync --locked`.
The Python dependencies do not install 7-Zip.

### Optional requirements.txt workflow

`requirements.txt` is an optional export of the lockfile for tools that need it.
Regenerate it after updating `uv.lock`:

```sh
uv export --locked --format requirements.txt --no-hashes --output-file requirements.txt
```

For an environment managed separately, dependencies can instead be installed with
`python -m pip install -r requirements.txt` using Python 3.11.9. Edit dependencies
in `pyproject.toml`, not in the generated export.

## Quick start

```sh
# The supplied pattern.yaml is empty. Create a rule interactively:
uv run --locked python main.py --add

# Recognize and extract, including child folders, keeping originals:
uv run --locked python main.py -p ./downloads -o ./unpacked -c -s

# Without -s, successfully extracted original archives/volumes are deleted:
uv run --locked python main.py -p ./downloads -o ./unpacked

uv run --locked python main.py --list
uv run --locked python main.py --delete a1b2c3d4
uv run --locked python main.py --config ./private-patterns.yaml --add
```

`--add` asks for one regex or replacement string, the required archive type
(`7z`, `rar`, or `zip`), layer count, and passwords.
It generates an eight-character hex UID.
Passwords are entered without terminal echo; later layers can reuse the last one.

| Argument | Meaning |
| --- | --- |
| `-p`, `--path PATH` | Search directory; defaults to the current directory. |
| `-o`, `--output PATH` | Output directory, created as needed; defaults to `PATH/extracted`. |
| `-s`, `--save` | Retain original archives and their volumes after success. |
| `-c`, `--child` | Search child directories; skip symlinks and the output directory. |
| `-l`, `--list` | List patterns, layers, types, and passwords. |
| `-d`, `--delete UID` | Remove the named pattern from YAML. |
| `-a`, `--add` | Guided pattern creation. |
| `--config PATH` | Configuration location; defaults to `pattern.yaml` beside `main.py`. |
| `--seven-zip EXECUTABLE` | Explicit executable path or command; otherwise try `7z`, then `7zz`. |

Management commands are mutually exclusive and cannot be combined with extraction
arguments. Exit codes: `0` success/no matches, `1` configuration or extraction
failure, `2` invalid CLI arguments, `130` interrupted input/extraction. One failed
archive does not stop unrelated archive groups.

## YAML patterns and renaming

```yaml
"a1b2c3d4":
  pattern: 's/^download_(.+)$/\1/'
  layer: 2
  password: ['outer-password', 'inner-password']
  type: '7z'

"e5f6a7b8":
  pattern: '(?i)^photos(?:_\d+)?$'
  layer: 1
  password: []
  type: 'zip'
```

- `pattern` is one nonempty string using **Python regular expressions**, optionally
  in replacement form. Lists are rejected; use regex alternation (`foo|bar`) or
  separate UID entries for alternatives.
  Matching uses `re.search`; add `^` and `$` to match the whole name. Matching is
  case-sensitive unless you use `(?i)` or a substitution's `i` flag.
- Only the filename is considered, never its directory. `type` is **required**
  and must be `7z`, `rar`, or `zip`; omitted or `null` values are rejected.
  First check whether the filename follows that type's naming style. If it does,
  strip the complete archive/volume suffix and match the basename.
  Naming-style checks are case-insensitive. Otherwise match the **whole filename**.
  For example, with `type: 7z`, `1111.7z.001` matches as `1111`,
  while `11111.8z` matches as `11111.8z`.
- `type` is a naming hint, not a filter: a `type: 7z` rule can match a ZIP file
  using a regex that includes `.zip`. Actual extraction uses 7-Zip format detection.
  Extensionless files are passed directly to 7-Zip; use a replacement rule if a
  particular filename suffix is needed.
  A matched file with no recognizable format is still attempted by 7-Zip;
  failed extraction retains the original.
- Volume grouping is independent of matching. Each volume filename is considered;
  a match on any volume selects the entire group once. `movie.7z.001`,
  `movie.part01.rar`, and `movie.zip.001` all match as `movie` when their
  configured type is `7z`, `rar`, and `zip`, respectively.
  Legacy RAR `.r00` and ZIP `.z01` suffixes are also recognized for their types.
  A bare `movie.001` does not follow the complete 7z naming style, so it matches
  in full. Replacements must yield a consistent basename across volumes.
- Renaming uses `s/REGEX/REPLACEMENT/`, with optional trailing `i` for case-insensitive
  matching. Delimiters `/`, `|`, `#`, and `~` are supported. Backreferences such as
  `\1` and `\g<name>` work. All occurrences are replaced, as with `re.sub`.
  Escape a literal delimiter with `\`. Paths and empty names are rejected.
  Sed-style escaped capture groups and quantifiers are also accepted when a
  replacement backreference cannot be resolved as a Python regex. For example:
  `s/\([a-zA-Z]\+\)\.7z\.exe$/\1.7z.001/`.
  Enter **single backslashes** at the interactive prompt or inside YAML single
  quotes; do not double them as you would in a JSON string. This is compatibility
  for basic regex captures/quantifiers, not a complete sed command interpreter.
- If multiple UIDs match the same
  archive/volume group, it is reported as ambiguous and retained, without extraction.
- Renames happen **in place in the source directory**. For example,
  with `type: zip`, `s/^download_(.+)$/\1/` renames `download_demo.zip` to `demo.zip` and uses `demo`
  as the fallback output folder name. A sole folder already inside the archive
  keeps its own name. If extraction or output placement fails, original filenames
  are restored. On success, `-s` retains the files under their renamed names;
  without `-s`, successful inputs are deleted. Existing rename destinations are
  never intentionally overwritten; collisions fail the group.
  A stripped archive/volume suffix is reattached after substitution. For names
  that do not follow the configured style, write substitutions against the full
  filename, e.g. `s/^download_(.+)\.zip$/\1.zip/` with `type: 7z`.
  Planned names are grouped before files are changed: renaming `demo.7z.exe` to
  `demo.7z.001` joins it with existing `demo.7z.002`, `.003`, and later volumes.
- `layer` is a positive integer: `1` extracts the outer archive, `2` also extracts
  archives inside it, and so on. Before extracting each inner layer, its filenames
  are normalized to the configured type's naming rules as described below.
  Renaming does not convert archive contents; 7-Zip still detects the actual format.
  Regexes apply only to the outer archive.
- `password` contains strings in layer order. If shorter than `layer`, the final
  password is reused. `[]` or `['']` means no password; use an empty string for an
  unencrypted layer followed by an encrypted one. Extra passwords are unused.
- Quote numeric passwords and UIDs. Prefer YAML single quotes for regexes so YAML
  preserves backslashes. Unknown fields, invalid regexes, and duplicate YAML keys
  are rejected.

Passwords are stored as plaintext and displayed by `--list` and the recognizing
step as requested. 7-Zip also receives them as process arguments. Use a private
config file and avoid committing real passwords.

Examples of the text passed to the regex:

| Filename | Configured `type` | Text to match |
| --- | --- | --- |
| `report.7z` | `7z` | `report` |
| `report.zip` | `7z` | `report.zip` |
| `11111.8z` | `7z` | `11111.8z` |
| `report` | `7z` | `report` |
| `report.7z.001` | `7z` | `report` |
| `report.part01.rar` | `rar` | `report` |
| `report.zip.001` | `zip` | `report` |
| `report.001` | `7z` | `report.001` |

## Intermediate archive names

For `layer > 1`, intermediate files may have missing or misleading extensions.
Before the next extraction, normalize them **in place inside `OUTPUT/temp`**:

| Intermediate input | Configured `type` | Normalized input |
| --- | --- | --- |
| `inner.payload` containing an archive | `7z` | `inner.7z` |
| `inner.part01.bin`, `inner.part02.bin` | `7z` | `inner.7z.001`, `inner.7z.002` |
| `inner.001.payload`, `inner.002.payload` | `zip` | `inner.zip.001`, `inner.zip.002` |
| `inner.part01`, `inner.part02` | `rar` | `inner.part01.rar`, `inner.part02.rar` |
| `inner.7z.001.exe`, `inner.7z.002.exe` | `7z` | `inner.7z.001`, `inner.7z.002` |

Recognized names already following the type's style are retained, including
native `.rar`/`.r00` and `.zip`/`.z01` groups. Loose `partNN` markers (with a
separator or at the start) and `.NNN` numeric markers, optionally followed by
another extension, identify intermediate volume groups. Numbers must start at
one and be consecutive, except for the native legacy formats. Duplicate volume
numbers, missing parts, and rename collisions fail the outer archive safely.

Other intermediate archive candidates are identified by archive names or file
signatures. A lone unrecognized file is attempted as the next layer. Ordinary
sidecar files are retained; do not use volume-style names for sidecars. No input
copies are made, and the original outer filenames are restored if a later layer
fails. A file's name alone cannot guarantee that its contents are a valid archive.

## Three stages

1. **recognizing** lists matched filenames (all volumes), combined size, passwords
   by layer, UID, and renamed basename. Scanning is nonrecursive unless `-c` is set.
2. **extracting** processes one group at a time, reading live 7-Zip percentage output
   into Rich progress bars. The progress percentage resets for each layer or nested
   archive. Small archives may finish before 7-Zip emits intermediate percentages.
3. **ending** validates results, places output folders, deletes originals unless
   `-s` was supplied, and reports archive, destination, status, and elapsed time.

Each successful original archive produces one top-level output directory:

```text
sample.zip contains:          Output:
  Pictures/a.jpg                OUTPUT/Pictures/a.jpg

sample.zip contains:          Output:
  a.jpg                         OUTPUT/sample/a.jpg

sample.zip contains:          Output:
  one/a.txt                     OUTPUT/sample/one/a.txt
  two/b.txt                     OUTPUT/sample/two/b.txt
```

Existing destinations are preserved; collisions become `name (2)`, `name (3)`, etc.
Empty archives produce an empty container folder. A single nested archive is
unwrapped through the requested layers, avoiding extra intermediate containers.
If a layer contains several archives or adjacent files, each nested archive is
extracted in its own directory and adjacent files are retained. Every nested
archive must reach the requested depth; a missing layer fails the entire outer
archive and retains its originals.

## Multipart archives and failure behavior

Recognized groups include:

- `name.7z.001`, `.002`, …; `name.zip.001`, `.002`, …; and extensionless
  `name.001`, `.002`, … using the configured type.
- `name.part01.rar`, `name.part02.rar`, …, including missing `.rar` suffixes.
- Legacy RAR `name.rar`, `name.r00`, `name.r01`, ….
- Split ZIP `name.zip`, `name.z01`, `name.z02`, ….

All volumes must be in the same directory. Missing first/intermediate volumes are
reported before extraction; 7-Zip detects other missing or damaged data. The
installed 7-Zip must support the specific multipart format. RAR extraction needs
a RAR-capable build. The utility cannot create RAR archives. Multipart groups are
listed and handled once; after success, deletion covers all recognized volumes.

7-Zip reads original files directly from their source directories. Input archives
are never copied or moved into temporary storage. Single-layer extraction writes
directly into a newly reserved output folder and does not create `OUTPUT/temp`.
After extraction, a sole root folder is normalized as described above.

Only runs containing a rule with `layer > 1` create `OUTPUT/temp/`, and only
intermediate extracted layers are stored there. The last layer is extracted into
its output folder. Intermediate sidecar files are moved into the final tree.
The owned `temp` directory is removed when the run finishes, including handled
failures or cancellation. An existing `OUTPUT/temp` blocks multilayer extraction
and is left untouched; single-layer extraction can proceed alongside it.
During multilayer runs, `temp` is reserved, so a final folder of that name becomes
`temp (2)` (or the next available number). Single-layer runs can produce a final
folder named `temp` normally.

Partial output is removed after failure, and source renames are rolled back.
Successful `-s` runs retain the renamed source files. A forced process kill or
power loss can leave intermediate output and renamed sources behind.
Nonzero
7-Zip exits, including warnings, count as failures. Source files changed during
extraction are retained. Deletion errors are reported with the number of volumes
actually deleted, and successfully published output remains available.

Archive member paths are checked before extraction; absolute paths, traversal,
and links are rejected. This check is not an operating-system sandbox. Keep your
7-Zip installation current and use trusted archives. Symlink inputs are skipped.
Output may equal the search directory; output cannot be an ancestor of it. On a
later recursive run with output equal to input, existing extracted contents are
also eligible for matching, so a separate output directory is usually clearer.

## Tests and layout

```sh
uv run --locked python -m unittest discover -s tests -v
```

Tests use the standard library. Integration tests generate real ZIP/7z archives
and exercise external 7-Zip; they are skipped if neither `7z` nor `7zz` is on PATH.
RAR grouping is unit-tested; RAR extraction uses the same external execution path.

- `main.py`: entry point.
- `pyproject.toml`: project metadata and pinned direct dependencies.
- `uv.lock`: resolved dependencies for the primary uv workflow.
- `requirements.txt`: optional generated dependency export.
- `src/config.py`: validated YAML loading/saving and UIDs.
- `src/matching.py`: regex and substitution parsing.
- `src/recognize.py`: scanning, suffix handling, multipart grouping.
- `src/extract.py`: async subprocesses, nested extraction, output placement.
- `src/cli.py`: argparse, interactive editing, three-stage Rich UI.
- `PROMPT.md`: editable requirements for future agents.

Rich progress uses its documented `Progress`/column API:
[Rich progress documentation](https://rich.readthedocs.io/en/stable/progress.html).
