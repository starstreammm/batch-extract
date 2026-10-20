import tempfile
import unittest
import re
from pathlib import Path

from src.config import ConfigError, Pattern, load_patterns, save_patterns
from src.extract import ExtractionError, check_listing_line, publish
from src.matching import parse_rule
from src.recognize import groups, intermediate_groups, recognize, walk_files


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def files(self, *names):
        paths = [self.root / name for name in names]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return paths

    def test_regex_and_substitution(self):
        self.assertEqual(parse_rule(r"^movie_\d+$").apply("movie_12"), "movie_12")
        self.assertIsNone(parse_rule(r"^movie_\d+$").apply("other"))
        self.assertEqual(
            parse_rule(r"s/^download_(.*)$/\1/").apply("download_test"), "test"
        )
        self.assertEqual(parse_rule("s|^MOVIE|film|i").apply("movie_12"), "film_12")
        self.assertEqual(parse_rule(r"s/a\|b/c/").apply("a|b"), "c")

    def test_bad_substitutions(self):
        for value in ("s/a/b", "s/a/b/g", r"s/(a)/\2/"):
            with self.subTest(value=value), self.assertRaises((ValueError, re.error)):
                parse_rule(value)
        for value in ("s/a/../", "s|a|../bad|"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_rule(value).apply("a")

    def test_sed_basic_substitution(self):
        rule = parse_rule(r"s/\([a-zA-Z]\+\)\.7z\.exe$/\1.7z.001/")
        self.assertEqual(rule.apply("Example.7z.exe"), "Example.7z.001")
        self.assertIsNone(rule.apply("123.7z.exe"))
        self.assertEqual(parse_rule(r"s/\(ab\)\{2\}$/\1/").apply("abab"), "ab")
        # Valid Python patterns with literal parentheses retain their meaning.
        self.assertEqual(parse_rule(r"s/\(ab\)/literal/").apply("(ab)"), "literal")
        with self.assertRaises(re.error):
            parse_rule(r"s/\(ab\)/\2/")

    def test_config_roundtrip_password_reuse(self):
        path = self.root / "config.yaml"
        pattern = Pattern("ab12", r"s/^x(.*)$/\1/", 3, ("123", "last"), "7z")
        save_patterns(path, [pattern])
        self.assertEqual(load_patterns(path), [pattern])
        self.assertEqual(pattern.password(2), "last")
        self.assertEqual(Pattern("aa", "x", 1, (), "zip").password(0), "")

    def test_bad_config(self):
        path = self.root / "config.yaml"
        for content in (
            "[]",
            "a: {}",
            "a: 1\na: 2",
            "a: {pattern: x, layer: true, password: [], type: zip}",
            "a: {pattern: x, layer: 1, password: [123], type: zip}",
            'a: {pattern: x, layer: "1", password: [], type: zip}',
            "a: {pattern: x, layer: 0, password: [], type: zip}",
            "a: {pattern: [], layer: 1, password: [], type: zip}",
            "a: {pattern: [123], layer: 1, password: [], type: zip}",
            "a: {pattern: [x], layer: 1, password: [], type: zip}",
            "a: {pattern: 123, layer: 1, password: [], type: zip}",
            "a: {pattern: '', layer: 1, password: [], type: zip}",
            "a: {pattern: x, layer: 1, password: secret, type: zip}",
            "a: {pattern: x, layer: 1, password: [], type: tar}",
            "a: {pattern: x, layer: 1, password: [], type: zip, extra: 1}",
            '"bad uid": {pattern: x, layer: 1, password: [], type: zip}',
            'a: {pattern: x, layer: 1, password: ["bad\\0password"], type: zip}',
        ):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ConfigError):
                load_patterns(path)

    def test_validation_error_does_not_echo_password(self):
        path = self.root / "config.yaml"
        path.write_text(
            'a: {pattern: x, layer: 1, password: "private-secret", type: zip}'
        )
        with self.assertRaises(ConfigError) as caught:
            load_patterns(path)
        self.assertNotIn("private-secret", str(caught.exception))

    def test_config_requires_type(self):
        path = self.root / "config.yaml"
        for suffix in ("", ", type: null"):
            path.write_text("a: {pattern: 'sample.zip', layer: 1, password: []" + suffix + "}")
            with self.assertRaises(ConfigError):
                load_patterns(path)

    def test_matching_uses_required_type_naming_style(self):
        cases = [
            ("sample.7z", "7z", r"^sample$", True),
            ("sample.7z", "7z", r"^sample\.7z$", False),
            ("sample.zip", "7z", r"^sample\.zip$", True),
            ("sample.zip", "7z", r"^sample$", False),
            ("sample.ZIP", "zip", r"^sample$", True),
            ("sample", "7z", r"^sample$", True),
            ("sample.7z.001", "7z", r"^sample\.7z\.001$", False),
            ("sample.7z.001", "7z", r"^sample$", True),
            ("sample.part01.rar", "rar", r"^sample$", True),
            ("sample.part01.rar", "7z", r"^sample\.part01\.rar$", True),
            ("sample.zip.001", "zip", r"^sample$", True),
            ("sample.z01", "zip", r"^sample$", True),
            ("sample.r00", "rar", r"^sample$", True),
            ("11111.8z", "7z", r"^11111\.8z$", True),
            ("1111.7z.001", "7z", r"^1111$", True),
        ]
        for filename, kind, regex, expected in cases:
            with self.subTest(filename=filename, kind=kind, regex=regex):
                result, errors = recognize(self.files(filename), [Pattern("a", regex, 1, (), kind)])
                self.assertEqual(bool(result), expected)
                self.assertFalse(errors)

    def test_multipart_match_on_any_volume(self):
        paths = self.files("sample.7z.001", "sample.7z.002")
        result, errors = recognize(paths, [Pattern("a", r"^sample$", 1, (), "7z")])
        self.assertFalse(errors)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].parts), 2)
        self.assertEqual(result[0].name, "sample")

    def test_full_filename_replacement_with_mismatched_type(self):
        result, errors = recognize(
            self.files("download_sample.zip"),
            [Pattern("a", r"s/^download_(.*)\.zip$/\1.zip/", 1, (), "7z")],
        )
        self.assertFalse(errors)
        self.assertEqual(result[0].name, "sample")
        self.assertEqual(result[0].parts[0].suffix, ".zip")

    def test_full_filename_replacement_can_change_suffix(self):
        result, errors = recognize(
            self.files("sample.zip"),
            [Pattern("a", r"s/^sample\.zip$/renamed.7z/", 1, (), "7z")],
        )
        self.assertFalse(errors)
        self.assertEqual(result[0].name, "renamed")
        self.assertEqual(result[0].parts[0].suffix, ".7z")
        self.assertEqual(result[0].primary.name, "sample.zip")

    def test_intermediate_volume_name_normalization(self):
        cases = [
            ("7z", ["movie.part01.bin", "movie.part02.bin"],
             ["movie.7z.001", "movie.7z.002"]),
            ("7z", ["movie.001.download", "movie.002.download"],
             ["movie.7z.001", "movie.7z.002"]),
            ("zip", ["movie.part01", "movie.part02"],
             ["movie.zip.001", "movie.zip.002"]),
            ("rar", ["movie.001.payload", "movie.002.payload"],
             ["movie.part01.rar", "movie.part02.rar"]),
            ("7z", ["movie.7z.001.exe", "movie.7z.002.exe"],
             ["movie.7z.001", "movie.7z.002"]),
            ("7z", ["movie.part01.7z", "movie.part02.7z"],
             ["movie.7z.001", "movie.7z.002"]),
            ("rar", ["movie.rar", "movie.r00"], ["movie.rar", "movie.r00"]),
            ("zip", ["movie.zip", "movie.z01"], ["movie.zip", "movie.z01"]),
        ]
        for kind, names, expected in cases:
            with self.subTest(kind=kind, names=names):
                result = intermediate_groups(self.files(*names), kind)
                self.assertEqual(len(result), 1)
                self.assertFalse(result[0].error)
                self.assertEqual(
                    [p.target_name or p.path.name for p in result[0].parts], expected
                )
                self.assertTrue(all(p.path.exists() for p in result[0].parts))

    def test_intermediate_missing_and_duplicate_volumes(self):
        for names in (
            ["movie.part01.bin", "movie.part03.bin"],
            ["movie.001.a", "movie.001.b"],
        ):
            with self.subTest(names=names):
                self.assertTrue(intermediate_groups(self.files(*names), "7z")[0].error)

    def test_volume_formats(self):
        cases = [
            (["item.7z.001", "item.7z.002"], "7z"),
            (["item.001", "item.002"], "7z"),
            (["item.part01.rar", "item.part02.rar"], "rar"),
            (["item.part01", "item.part02"], "rar"),
            (["item.rar", "item.r00", "item.r01"], "rar"),
            (["item.zip", "item.z01", "item.z02"], "zip"),
        ]
        for names, kind in cases:
            result = groups(self.files(*names), kind)
            with self.subTest(names=names):
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].name, "item")
                self.assertEqual(result[0].error, "")
                self.assertEqual(result[0].primary.name, names[0])

    def test_missing_volumes(self):
        for names in (
            ["a.7z.002"],
            ["a.part01.rar", "a.part03.rar"],
            ["a.r00"],
            ["a.z01"],
        ):
            with self.subTest(names=names):
                self.assertTrue(groups(self.files(*names))[0].error)

    def test_recognition_rename_and_conflict(self):
        files = self.files("download_name.zip", "unrelated.txt")
        pattern = Pattern("abc", r"s/^download_(.*)$/\1/", 1, (), "zip")
        result, errors = recognize(files, [pattern])
        self.assertFalse(errors)
        self.assertEqual(result[0].name, "name")
        result, errors = recognize(
            files, [pattern, Pattern("def", ".*", 1, (), "zip")]
        )
        self.assertTrue(errors)
        self.assertNotIn(files[0], [a.primary for a in result])

    def test_extensionless_and_suffix_stripping(self):
        files = self.files("archive", "other.7z")
        result, errors = recognize(files, [Pattern("a", "^archive$", 1, (), "zip")])
        self.assertEqual(result[0].parts[0].suffix, ".zip")
        self.assertFalse(errors)

    def test_walk_excludes_output_and_symlinks(self):
        self.files("a.zip", "sub/b.zip", "out/c.zip")
        (self.root / "link").symlink_to(self.root / "sub", target_is_directory=True)
        self.assertEqual(len(walk_files(self.root, False)), 1)
        self.assertEqual(len(walk_files(self.root, True, (self.root / "out",))), 2)

    def test_folder_normalization_and_collision(self):
        output = self.root / "out"
        for expected in ("inside", "inside (2)"):
            payload = self.root / "payload"
            (payload / "inside").mkdir(parents=True)
            (payload / "inside" / "file").write_text("data")
            target = publish(payload, output, "archive")
            self.assertEqual(target.name, expected)
            self.assertEqual((target / "file").read_text(), "data")
            (payload / "inside").rmdir()
            payload.rmdir()
        payload.mkdir()
        (payload / "single").touch()
        target = publish(payload, output, "archive")
        self.assertEqual(target.name, "archive")
        self.assertTrue((target / "single").is_file())

    def test_archive_paths(self):
        for name in ("../evil", "/absolute", r"C:\evil", r"..\evil", "file:stream"):
            with self.subTest(name=name), self.assertRaises(ExtractionError):
                check_listing_line(f"Path = {name}")
        check_listing_line("Path = safe/file.txt")
        with self.assertRaises(ExtractionError):
            check_listing_line("Symbolic Link = outside")
        with self.assertRaises(ExtractionError):
            check_listing_line("Attributes = A lrwxrwxrwx")


if __name__ == "__main__":
    unittest.main()
