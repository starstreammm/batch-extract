"""Real 7-Zip tests; skipped only if neither 7z nor 7zz is installed."""

import asyncio
import io
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from rich.console import Console

from src.cli import main, parser, extract_batch
from src.config import Pattern, load_patterns, save_patterns
from src.extract import Extractor, run_7z

SEVEN_ZIP = shutil.which("7z") or shutil.which("7zz")


@unittest.skipUnless(SEVEN_ZIP, "7z or 7zz is required for integration tests")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def zip(self, name, contents):
        path = self.source / name
        with zipfile.ZipFile(path, "w") as archive:
            for filename, content in contents.items():
                archive.writestr(filename, content)
        return path

    def run_batch(self, pattern=None, save=False, child=False):
        args = parser().parse_args(
            ["-p", str(self.source), "-o", str(self.output)]
            + (["-s"] if save else [])
            + (["-c"] if child else [])
        )
        self.log = io.StringIO()
        console = Console(file=self.log, width=160, color_system=None)
        return asyncio.run(
            extract_batch(
                args, [pattern or Pattern("abc", "^sample$", 1, (), "zip")], console
            )
        )

    def test_single_folder_and_delete(self):
        archive = self.zip("sample.zip", {"folder/file.txt": "hello"})
        self.assertEqual(self.run_batch(), 0, self.log.getvalue())
        self.assertEqual((self.output / "folder/file.txt").read_text(), "hello")
        self.assertFalse(archive.exists())
        for stage in ("recognizing", "extracting", "ending"):
            self.assertIn(stage, self.log.getvalue())

    def test_intermediates_use_output_temp_and_are_cleaned(self):
        inner = self.zip("inner.zip", {"file.txt": "data"})
        self.zip("sample.zip", {"inner.zip": inner.read_bytes()})
        inner.unlink()
        workspaces = []
        original = Extractor.extract

        async def inspected(extractor, *args, **kwargs):
            self.assertEqual(extractor.workspace, self.output.resolve() / "temp")
            self.assertTrue(extractor.workspace.is_dir())
            archive = args[0]
            if len(args) > 4:  # Recursive calls read intermediate archives.
                self.assertTrue(archive.primary.is_relative_to(extractor.workspace))
            else:
                self.assertTrue(archive.primary.is_relative_to(self.source.resolve()))
            payload = await original(extractor, *args, **kwargs)
            self.assertEqual(payload.parent, self.output.resolve())
            workspaces.append(payload)
            return payload

        with patch.object(Extractor, "extract", inspected):
            self.assertEqual(
                self.run_batch(Pattern("abc", "^sample$", 2, (), "zip")),
                0,
                self.log.getvalue(),
            )
        self.assertEqual(len(workspaces), 2)
        self.assertFalse((self.output / "temp").exists())
        self.assertEqual((self.output / "sample/file.txt").read_text(), "data")

    def test_existing_temp_is_preserved(self):
        archive = self.zip("sample.zip", {"file.txt": "data"})
        temporary = self.output / "temp"
        temporary.mkdir(parents=True)
        (temporary / "keep.txt").write_text("existing")
        self.assertEqual(self.run_batch(save=True), 0, self.log.getvalue())
        self.assertEqual((temporary / "keep.txt").read_text(), "existing")
        self.assertTrue(archive.exists())
        with self.assertRaisesRegex(ValueError, "Temporary path already exists"):
            self.run_batch(Pattern("a", "^sample$", 2, (), "zip"))

    def test_temp_is_cleaned_on_cancellation(self):
        archive = self.zip("sample.zip", {"file.txt": "data"})

        async def cancelled(extractor, *args, **kwargs):
            (extractor.workspace / "partial").write_text("incomplete")
            raise asyncio.CancelledError

        with patch.object(Extractor, "extract", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                self.run_batch(Pattern("a", "^sample$", 2, (), "zip"))
        self.assertFalse((self.output / "temp").exists())
        self.assertTrue(archive.exists())

    def test_final_folder_named_temp_is_preserved(self):
        self.zip("sample.zip", {"temp/file.txt": "data"})
        self.assertEqual(self.run_batch(), 0, self.log.getvalue())
        self.assertEqual((self.output / "temp/file.txt").read_text(), "data")

    def test_extensionless_rename_and_save(self):
        archive = self.zip("download_sample", {"file.txt": "hello"})
        original_inode = archive.stat().st_ino
        pattern = Pattern("abc", r"s/^download_(.*)$/\1/", 1, (), "zip")
        self.assertEqual(self.run_batch(pattern, save=True), 0, self.log.getvalue())
        self.assertFalse(archive.exists())
        self.assertEqual((self.source / "sample").stat().st_ino, original_inode)
        self.assertEqual((self.output / "sample/file.txt").read_text(), "hello")

    def test_single_layer_reads_original_and_never_creates_temp(self):
        archive = self.zip("sample.zip", {"file.txt": "hello"})
        original_inode = archive.stat().st_ino
        seen = []

        async def inspected(executable, args, **kwargs):
            self.assertFalse((self.output / "temp").exists())
            self.assertEqual(Path(args[-1]), archive.resolve())
            self.assertEqual(Path(args[-1]).stat().st_ino, original_inode)
            seen.append(args[0])
            return await run_7z(executable, args, **kwargs)

        with patch("src.extract.run_7z", side_effect=inspected), patch(
            "src.extract.shutil.copy2", side_effect=AssertionError("Must not copy inputs")
        ):
            self.assertEqual(self.run_batch(save=True), 0, self.log.getvalue())
        self.assertEqual(seen, ["l", "x"])
        self.assertTrue(archive.exists())
        self.assertFalse((self.output / "temp").exists())

    def test_failed_rename_is_restored(self):
        archive = self.source / "download_sample.zip"
        archive.write_bytes(b"invalid zip")
        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 1, (), "zip")
        self.assertEqual(self.run_batch(pattern), 1)
        self.assertEqual(archive.read_bytes(), b"invalid zip")
        self.assertFalse((self.source / "sample.zip").exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_rename_collision_preserves_both_files(self):
        archive = self.zip("download_sample.zip", {"file": "new"})
        existing = self.zip("sample.zip", {"file": "old"})
        before = {p: p.read_bytes() for p in (archive, existing)}
        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 1, (), "zip")
        self.assertEqual(self.run_batch(pattern), 1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_rename_restored_on_publication_failure(self):
        archive = self.zip("download_sample.zip", {"file": "data"})
        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 1, (), "zip")
        with patch("src.cli.publish", side_effect=OSError("cannot publish")):
            self.assertEqual(self.run_batch(pattern), 1)
        self.assertTrue(archive.exists())
        self.assertFalse((self.source / "sample.zip").exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_rename_restored_when_inner_layer_fails(self):
        archive = self.zip("download_sample.zip", {"inner.zip": "invalid"})
        before = archive.read_bytes()
        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 2, (), "zip")
        self.assertEqual(self.run_batch(pattern), 1)
        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse((self.source / "sample.zip").exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_partial_volume_rename_is_rolled_back(self):
        paths = [self.source / f"download_sample.7z.{index:03}" for index in (1, 2)]
        for path in paths:
            path.write_text("original")
        original_rename = Path.rename

        def fail_second(path, target):
            if path == paths[1].resolve():
                raise OSError("cannot rename second volume")
            return original_rename(path, target)

        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 1, ())
        with patch.object(Path, "rename", fail_second):
            self.assertEqual(self.run_batch(pattern), 1)
        self.assertEqual(set(self.source.iterdir()), set(paths))
        self.assertTrue(all(path.read_text() == "original" for path in paths))

    def test_rename_restored_on_cancellation(self):
        archive = self.zip("download_sample.zip", {"file": "data"})
        pattern = Pattern("a", r"s/^download_(.*)$/\1/", 1, (), "zip")

        async def cancelled(extractor, *args, **kwargs):
            self.assertFalse(archive.exists())
            self.assertTrue((self.source / "sample.zip").exists())
            (kwargs["destination"] / "partial").write_text("incomplete")
            raise asyncio.CancelledError

        with patch.object(Extractor, "extract", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                self.run_batch(pattern)
        self.assertTrue(archive.exists())
        self.assertFalse((self.source / "sample.zip").exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_basic_regex_renamed_first_volume_joins_siblings(self):
        data = self.root / "data.bin"
        data.write_bytes(bytes(range(256)) * 16)
        asyncio.run(run_7z(
            SEVEN_ZIP, ["a", "-mx=0", "-v1k", str(self.source / "sample.7z"), str(data)]
        ))
        first = self.source / "sample.7z.exe"
        (self.source / "sample.7z.001").rename(first)
        inode = first.stat().st_ino
        pattern = Pattern("a", r"s/\([a-zA-Z]\+\)\.7z\.exe$/\1.7z.001/", 1, ())
        self.assertEqual(self.run_batch(pattern, save=True), 0, self.log.getvalue())
        self.assertEqual((self.source / "sample.7z.001").stat().st_ino, inode)
        self.assertFalse(first.exists())
        self.assertEqual((self.output / "sample/data.bin").read_bytes(), data.read_bytes())
        self.assertFalse((self.output / "temp").exists())
        # A subsequent ordinary split-volume extraction deletes the entire group.
        self.assertEqual(self.run_batch(Pattern("b", r"^sample\.7z\.\d+$", 1, ())), 0,
                         self.log.getvalue())
        self.assertFalse(list(self.source.iterdir()))

    def test_optional_or_mismatched_type_full_filename(self):
        for kind in (None, "7z"):
            with self.subTest(kind=kind):
                archive = self.zip("download_sample.zip", {"file.txt": "hello"})
                pattern = Pattern("a", r"s/^download_(.*)\.zip$/\1.zip/", 1, (), kind)
                self.assertEqual(self.run_batch(pattern), 0, self.log.getvalue())
                self.assertFalse(archive.exists())
                self.assertEqual((self.output / "sample/file.txt").read_text(), "hello")
                shutil.rmtree(self.output)

    def test_extensionless_without_type(self):
        self.zip("sample", {"file.txt": "hello"})
        self.assertEqual(self.run_batch(Pattern("a", "^sample$", 1, ())), 0, self.log.getvalue())
        self.assertEqual((self.output / "sample/file.txt").read_text(), "hello")

    def test_multiple_folders(self):
        self.zip("sample.zip", {"one/a": "1", "two/b": "2"})
        self.assertEqual(self.run_batch(), 0, self.log.getvalue())
        self.assertTrue((self.output / "sample/one/a").is_file())
        self.assertTrue((self.output / "sample/two/b").is_file())

    def test_collision(self):
        self.zip("sample.zip", {"file.txt": "new"})
        (self.output / "sample").mkdir(parents=True)
        (self.output / "sample/file.txt").write_text("old")
        self.assertEqual(self.run_batch(), 0, self.log.getvalue())
        self.assertEqual((self.output / "sample/file.txt").read_text(), "old")
        self.assertEqual((self.output / "sample (2)/file.txt").read_text(), "new")

    def test_nested_and_password_reuse(self):
        data = self.root / "payload.txt"
        data.write_text("secret data")
        inner = self.root / "inner.7z"
        outer = self.source / "sample.7z"
        for archive, member in ((inner, data), (outer, inner)):
            asyncio.run(
                run_7z(
                    SEVEN_ZIP, ["a", "-psecret", "-mhe=on", str(archive), str(member)]
                )
            )
        pattern = Pattern("abc", "^sample$", 2, ("secret",), "7z")
        self.assertEqual(self.run_batch(pattern), 0, self.log.getvalue())
        self.assertEqual(
            (self.output / "sample/payload.txt").read_text(), "secret data"
        )

    def test_nested_multiple_and_sidecars(self):
        inner = self.zip("inner.zip", {"a.txt": "inner"})
        inner_bytes = inner.read_bytes()
        inner.unlink()
        self.zip(
            "sample.zip",
            {"one.zip": inner_bytes, "two.zip": inner_bytes, "readme.txt": "keep"},
        )
        self.assertEqual(
            self.run_batch(Pattern("abc", "^sample$", 2, (), "zip")),
            0,
            self.log.getvalue(),
        )
        self.assertEqual((self.output / "sample/one/a.txt").read_text(), "inner")
        self.assertTrue((self.output / "sample/two/a.txt").is_file())
        self.assertEqual((self.output / "sample/readme.txt").read_text(), "keep")

    def test_split_7z_and_missing_suffix(self):
        data = self.root / "data.bin"
        data.write_bytes(bytes(range(256)) * 32)
        archive = self.source / "sample.7z"
        asyncio.run(run_7z(SEVEN_ZIP, ["a", "-mx=0", "-v1k", str(archive), str(data)]))
        for part in self.source.iterdir():
            part.rename(part.with_name(part.name.replace(".7z.", ".")))
        self.assertGreater(len(list(self.source.iterdir())), 1)
        self.assertEqual(
            self.run_batch(Pattern("abc", r"^sample\.\d{3}$", 1, (), "7z")),
            0,
            self.log.getvalue(),
        )
        self.assertEqual(
            (self.output / "sample/data.bin").read_bytes(), data.read_bytes()
        )
        self.assertFalse(list(self.source.iterdir()))

    def test_wrong_password_retains_original(self):
        data = self.root / "data"
        data.write_text("secret")
        archive = self.source / "sample.7z"
        asyncio.run(
            run_7z(SEVEN_ZIP, ["a", "-psecret", "-mhe=on", str(archive), str(data)])
        )
        self.assertEqual(
            self.run_batch(Pattern("abc", "^sample$", 1, ("wrong",), "7z")), 1
        )
        self.assertTrue(archive.exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_nested_bare_split_volumes(self):
        data = self.root / "data.bin"
        data.write_bytes(bytes(range(256)) * 16)
        inner = self.root / "nested.7z"
        asyncio.run(run_7z(SEVEN_ZIP, ["a", "-mx=0", "-v1k", str(inner), str(data)]))
        self.zip(
            "sample.zip",
            {
                p.name.replace(".7z.", "."): p.read_bytes()
                for p in self.root.glob("nested.7z.*")
            },
        )
        self.assertEqual(
            self.run_batch(Pattern("abc", "^sample$", 2, (), "zip")),
            0,
            self.log.getvalue(),
        )
        self.assertEqual(
            (self.output / "sample/data.bin").read_bytes(), data.read_bytes()
        )

    def test_missing_split_volume_retains_all_remaining(self):
        data = self.root / "data.bin"
        data.write_bytes(bytes(range(256)) * 16)
        archive = self.source / "sample.7z"
        asyncio.run(run_7z(SEVEN_ZIP, ["a", "-mx=0", "-v1k", str(archive), str(data)]))
        (self.source / "sample.7z.002").unlink()
        remaining = set(self.source.iterdir())
        self.assertEqual(self.run_batch(Pattern("abc", r"^sample\.7z\.\d{3}$", 1, (), "7z")), 1)
        self.assertEqual(set(self.source.iterdir()), remaining)

    def test_no_password_does_not_hang(self):
        data = self.root / "data"
        data.write_text("secret")
        archive = self.source / "sample.7z"
        asyncio.run(
            run_7z(SEVEN_ZIP, ["a", "-psecret", "-mhe=on", str(archive), str(data)])
        )
        args = parser().parse_args(["-p", str(self.source), "-o", str(self.output)])
        console = Console(file=io.StringIO())

        async def bounded():
            return await asyncio.wait_for(
                extract_batch(
                    args, [Pattern("abc", "^sample$", 1, (), "7z")], console
                ),
                5,
            )

        self.assertEqual(asyncio.run(bounded()), 1)
        self.assertTrue(archive.exists())

    def test_zip_symlink_rejected(self):
        archive = self.source / "sample.zip"
        with zipfile.ZipFile(archive, "w") as output:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            output.writestr(info, "../outside")
        self.assertEqual(self.run_batch(), 1)
        self.assertTrue(archive.exists())
        self.assertFalse(list(self.output.iterdir()))

    def test_corrupt_and_wrong_layer_retain_original(self):
        archive = self.source / "sample.zip"
        archive.write_text("not an archive")
        self.assertEqual(self.run_batch(), 1)
        self.assertTrue(archive.exists())
        self.zip("sample.zip", {"file": "data"})
        self.assertEqual(self.run_batch(Pattern("abc", "^sample$", 2, (), "zip")), 1)
        self.assertTrue(archive.exists())

    def test_traversal_rejected(self):
        archive = self.zip("sample.zip", {"../escape.txt": "bad"})
        self.assertEqual(self.run_batch(), 1)
        self.assertTrue(archive.exists())
        self.assertFalse((self.root / "escape.txt").exists())

    def test_child_flag(self):
        archive = self.zip("sample.zip", {"file": "data"})
        (self.source / "child").mkdir()
        archive.rename(self.source / "child/sample.zip")
        self.assertEqual(self.run_batch(), 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.run_batch(child=True), 0, self.log.getvalue())
        self.assertTrue((self.output / "sample/file").exists())

    def test_source_change_blocks_delete(self):
        archive = self.zip("sample.zip", {"file": "data"})
        from src.extract import publish

        def modified_publish(*args):
            destination = publish(*args)
            archive.write_bytes(archive.read_bytes() + b"changed")
            return destination

        with patch("src.cli.publish", side_effect=modified_publish):
            self.assertEqual(self.run_batch(), 1)
        self.assertTrue(archive.exists())
        self.assertTrue((self.output / "sample/file").exists())

    def test_cli_management(self):
        config = self.root / "pattern.yaml"
        save_patterns(config, [Pattern("abc", "sample", 1, (), "zip")])
        with patch("src.cli.Console", return_value=Console(file=io.StringIO())):
            self.assertEqual(main(["--config", str(config), "--list"]), 0)
            self.assertEqual(main(["--config", str(config), "--delete", "abc"]), 0)
            self.assertEqual(main(["--config", str(config), "--delete", "abc"]), 1)

    def test_guided_add(self):
        config = self.root / "pattern.yaml"
        console = Console(file=io.StringIO())
        with patch("src.cli.Console", return_value=console), patch(
            "src.cli.Prompt.ask",
            side_effect=[r"s/^prefix_(.*)$/\1/", "7z", "secret"],
        ), patch("src.cli.IntPrompt.ask", return_value=3), patch(
            "src.cli.Confirm.ask", return_value=True
        ):
            self.assertEqual(main(["--config", str(config), "--add"]), 0)
        pattern = load_patterns(config)[0]
        self.assertRegex(pattern.uid, "^[a-f0-9]{8}$")
        self.assertEqual(pattern.password(2), "secret")

    def test_guided_add_without_type(self):
        config = self.root / "pattern.yaml"
        with patch("src.cli.Console", return_value=Console(file=io.StringIO())), patch(
            "src.cli.Prompt.ask", side_effect=[r"^sample\.zip$", "auto", ""]
        ), patch("src.cli.IntPrompt.ask", return_value=1):
            self.assertEqual(main(["--config", str(config), "--add"]), 0)
            self.assertEqual(main(["--config", str(config), "--list"]), 0)
        self.assertIsNone(load_patterns(config)[0].type)
        self.assertNotIn("type:", config.read_text())


if __name__ == "__main__":
    unittest.main()
