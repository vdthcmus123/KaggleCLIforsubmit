import json
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import kaggle_harvest as h


FIXTURE = Path(__file__).parent / "fixtures" / "competition.toml"


class OutputDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.comp = replace(
            h.load_config(FIXTURE)[0],
            slug="example-competition",
            file_pattern="auto",
            submission_file_name=None,
        )

    def test_schema_settings_are_not_part_of_the_competition_model(self) -> None:
        names = {item.name for item in fields(h.Competition)}
        self.assertNotIn("required_columns", names)
        self.assertNotIn("dataset_shapes", names)

    def test_canonical_submission_accepts_any_extension(self) -> None:
        for name in ("submission.csv", "submission.zip", "submission.parquet",
                     "submission.json", "submission.bin", "submission"):
            with self.subTest(name=name):
                self.assertEqual(h.select_output_names(self.comp, [name, "weights.pth"]), [name])

    def test_original_submission_is_preferred_over_clean_alternative(self) -> None:
        self.assertEqual(
            h.select_output_names(self.comp, ["submission_clean.csv", "submission.csv"]),
            ["submission.csv"],
        )

    def test_single_arbitrary_output_is_supported(self) -> None:
        self.assertEqual(
            h.select_output_names(self.comp, ["predictions/custom.dat", "run.log", "model.pt"]),
            ["predictions/custom.dat"],
        )

    def test_multiple_plausible_files_are_not_guessed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous submission outputs"):
            h.select_output_names(self.comp, ["predictions.csv", "metrics.json"])

    def test_multiple_canonical_formats_are_not_guessed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            h.select_output_names(self.comp, ["submission.csv", "submission.zip"])

    def test_explicit_nested_path_resolves_ambiguity(self) -> None:
        comp = replace(self.comp, submission_file_name="final/result.zip")
        self.assertEqual(
            h.select_output_names(comp, ["intermediate/result.zip", "final/result.zip"]),
            ["final/result.zip"],
        )

    def test_same_basename_in_multiple_folders_is_ambiguous(self) -> None:
        comp = replace(self.comp, submission_file_name="result.zip")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            h.select_output_names(comp, ["a/result.zip", "b/result.zip"])

    def test_missing_explicit_output_has_no_alternate_fallback(self) -> None:
        comp = replace(self.comp, submission_file_name="result.zip")
        self.assertEqual(h.select_output_names(comp, ["submission.csv"]), [])

    def test_legacy_regex_remains_supported(self) -> None:
        comp = replace(self.comp, file_pattern=r"(?i).*\.zip$")
        self.assertEqual(h.select_output_names(comp, ["a.zip", "b.csv"]), ["a.zip"])

    def test_unsafe_paths_are_rejected(self) -> None:
        for name in ("../submission.zip", "/submission.zip", "C:/submission.zip"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                h.select_output_names(self.comp, [name])

    def test_static_source_hints_select_nonstandard_filename(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "kernel-metadata.json").write_text(
                json.dumps({"code_file": "source.ipynb"}), encoding="utf-8",
            )
            cells = [
                {"cell_type": "code", "source": ["WORK = '/kaggle/working'\n",
                 "out = Path(WORK) / 'final' / 'answer.parquet'\n"]},
                {"cell_type": "code", "source": [
                 "%matplotlib inline\n", "df.to_parquet(out)\n",
                 "pd.read_csv('/kaggle/input/sample_submission.csv')\n"]},
            ]
            (root / "source.ipynb").write_text(json.dumps({"cells": cells}), encoding="utf-8")
            self.assertEqual(h.notebook_output_hints(root), {"final/answer.parquet"})
            self.assertEqual(
                h.select_output_names(self.comp, ["stats.json", "final/answer.parquet"], source_dir=root),
                ["final/answer.parquet"],
            )

    def test_generic_artifact_validation_never_imposes_a_schema(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for name, content in (("answer.csv", b"anything\n1\n"),
                                  ("answer.zip", b"PK\x03\x04"),
                                  ("answer.parquet", b"PAR1\x00\xff")):
                with self.subTest(name=name):
                    path = Path(folder) / name
                    path.write_bytes(content)
                    self.assertEqual(h.validate_submission_file(path), [])
                    self.assertEqual(path.read_bytes(), content)

    def test_missing_or_empty_artifacts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "answer"
            self.assertIn("does not exist", h.validate_submission_file(path)[0])
            path.touch()
            self.assertIn("empty", h.validate_submission_file(path)[0])

    def test_download_only_selected_nested_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / self.comp.slug / "owner__notebook"

            def download(args, **_kwargs):
                (target / "results").mkdir()
                (target / "results" / "submission.zip").write_bytes(b"zip")

            with (
                patch.object(h, "list_kernel_files", return_value=[
                    {"name": "results/submission.zip"}, {"name": "huge-model.pth"},
                ]),
                patch.object(h, "run_cmd", side_effect=download) as command,
            ):
                paths = h.download_output(self.comp, "owner/notebook", root)
            self.assertEqual(paths, [target / "results" / "submission.zip"])
            args = command.call_args.args[0]
            self.assertEqual(args[args.index("--file-pattern") + 1], r"^(?:results/submission\.zip)$")
            self.assertEqual(h.output_relative_name(paths[0], self.comp, "owner/notebook", root),
                             "results/submission.zip")

    def test_download_does_not_reuse_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / self.comp.slug / "owner__notebook"
            target.mkdir(parents=True)
            (target / "submission.zip").write_bytes(b"old")
            with patch.object(h, "list_kernel_files", return_value=[]):
                self.assertEqual(h.download_output(self.comp, "owner/notebook", root), [])
            self.assertFalse((target / "submission.zip").exists())

    def test_file_selection_never_returns_unrelated_artifact(self) -> None:
        self.assertIsNone(h.select_downloaded_file([Path("model.bin")], "submission.zip"))
        self.assertIsNone(h.select_downloaded_file(
            [Path("a/result.zip"), Path("b/result.zip")], "result.zip"))
        self.assertEqual(h.select_downloaded_file(
            [Path("root/final/result.zip")], "final/result.zip"), Path("root/final/result.zip"))

    def test_auto_copy_also_runs_for_file_upload_competitions(self) -> None:
        comp = replace(self.comp, code_competition=False)
        with patch.object(h, "process_auto_copy_code_submission", return_value={}) as process:
            h.process_competition(comp, state={}, output_dir=Path("out"),
                                  work_dir=Path("work"), submit=False, now=h.utc_now())
        process.assert_called_once()

    def test_file_upload_auto_copy_uses_hotness_web_list(self) -> None:
        comp = replace(self.comp, code_competition=False)
        with patch.object(h, "list_competition_code_kernels", return_value=[]) as listing:
            h.list_kernels(comp)
        listing.assert_called_once_with(comp)

    def test_code_submission_preserves_nested_output_name(self) -> None:
        api = SimpleNamespace(authenticate=Mock(), competition_submit_code=Mock(
            return_value=SimpleNamespace(ref=123)))
        with (
            patch.dict(sys.modules, {"kaggle": SimpleNamespace(api=api)}),
            patch.object(h, "wait_for_submission_result"),
        ):
            h.submit_file(self.comp, "owner/notebook", Path("local/result.zip"), "test",
                          kernel_version="4", output_file_name="final/result.zip")
        api.competition_submit_code.assert_called_once_with(
            "final/result.zip", "test", self.comp.slug, "owner/notebook", 4, quiet=True)

    def test_file_submission_uses_the_original_downloaded_artifact(self) -> None:
        comp = replace(self.comp, code_competition=False)
        api = SimpleNamespace(authenticate=Mock(), competition_submit=Mock(
            return_value=SimpleNamespace(ref=123)))
        with (
            patch.dict(sys.modules, {"kaggle": SimpleNamespace(api=api)}),
            patch.object(h, "wait_for_submission_result"),
        ):
            h.submit_file(comp, "owner/notebook", Path("local/result.zip"), "test",
                          kernel_version="4", output_file_name="final/result.zip")
        api.competition_submit.assert_called_once_with(
            str(Path("local/result.zip")), "test", comp.slug, quiet=True)


if __name__ == "__main__":
    unittest.main()
