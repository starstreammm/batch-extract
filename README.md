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

`--add` asks for regexes one at a time, archive type, layer count, and passwords.
Enter a blank regex after the last rule. It generates an eight-character hex UID.
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
  pattern:
    - '^movie_\d+$'
    - 's/^download_(.+)$/\1/'
  layer: 2
  password: ['outer-password', 'inner-password']
  type: '7z'

"e5f6a7b8":
  pattern: ['(?i)^photos(?:_\d+)?$']
  layer: 1
  password: []
  type: 'zip'
```

- `pattern` is a nonempty list of alternatives using **Python regular expressions**.
  Matching uses `re.search`; add `^` and `$` to match the whole name. Matching is
  case-sensitive unless you use `(?i)` or a substitution's `i` flag.
- Only the filename is considered, never its directory. The archive and volume
  suffixes are removed first: `movie_12.7z.001` becomes `movie_12`.
- `type` specifies the outer archive format and the suffix to restore if missing.
  `movie_12` or split `movie_12.001` can therefore match the same rule as
  `movie_12.7z`. An unrecognized suffix is part of the basename; remove it with a
  substitution if desired, e.g. `s/\.download$//`.
- Renaming uses `s/REGEX/REPLACEMENT/`, with optional trailing `i` for case-insensitive
  matching. Delimiters `/`, `|`, `#`, and `~` are supported. Backreferences such as
  `\1` and `\g<name>` work. All occurrences are replaced, as with `re.sub`.
  Escape a literal delimiter with `\`. Paths and empty names are rejected.
- The first matching alternative in one UID wins. If multiple UIDs match the same
  archive/volume group, it is reported as ambiguous and retained, without extraction.
- Renames affect **temporary copies only**. For example,
  `s/^download_(.+)$/\1/` stages `download_demo.zip` as `demo.zip` and uses `demo`
  as the fallback output folder name. A sole folder already inside the archive
  keeps its own name. With `-s`, the original retains its original filename.
- `layer` is a positive integer: `1` extracts the outer archive, `2` also extracts
  archives inside it, and so on. Inner formats are detected independently and may
  differ from `type`. Regexes apply only to the outer archive.
- `password` contains strings in layer order. If shorter than `layer`, the final
  password is reused. `[]` or `['']` means no password; use an empty string for an
  unencrypted layer followed by an encrypted one. Extra passwords are unused.
- Quote numeric passwords and UIDs. Prefer YAML single quotes for regexes so YAML
  preserves backslashes. Unknown fields, invalid regexes, and duplicate YAML keys
  are rejected.

Passwords are stored as plaintext and displayed by `--list` and the recognizing
step as requested. 7-Zip also receives them as process arguments. Use a private
config file and avoid committing real passwords.

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

All temporary archive copies, intermediate layers, and staged extraction results
are stored under `OUTPUT/temp/`. The entire `temp` directory is removed when the
run finishes, including handled failures or cancellation. Allow space there for
copies, intermediate layers, and all extracted results until the ending stage.
An existing `OUTPUT/temp` is left untouched and blocks extraction; move or remove
it before retrying. A forced process kill or power loss can leave this directory
behind. `temp` is reserved during extraction, so an output folder with that name
is published as `temp (2)` (or the next available number).
Partial output is removed after failure. Nonzero
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
