import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import kaggle_harvest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_KERNEL = "pilkwang/biohub-cell-tracking-blend-preprocessings"
DEEPCENTER_DATASET = "pilkwang/biohub-deepcenter-unet3d-center-prior-v1"


def notebook_html(script_version_id: int) -> str:
    return (
        '<link rel="alternate" type="application/json+oembed" '
        'href="https://www.kaggle.com/oembed/kernel?url=x'
        f'%3FscriptVersionId%3D{script_version_id}" />'
    )


class BestScoreVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.competition = kaggle_harvest.load_config(ROOT / "config.toml")[0]
        self.version_sessions = {
            version: 330_000_000 + version * 1000 for version in range(1, 34)
        }

    def fetch_best_v25(self, url: str) -> str:
        marker = "/versions/"
        if marker not in url:
            return notebook_html(self.version_sessions[25])
        version = int(url.rsplit(marker, 1)[1])
        return notebook_html(self.version_sessions[version])

    def test_biohub_config_uses_dynamic_resolution(self) -> None:
        self.assertEqual(self.competition.source_kernel_versions, {})
        self.assertEqual(self.competition.dataset_source_versions, {})

    def test_best_score_session_is_mapped_to_exact_version(self) -> None:
        version, session_id = kaggle_harvest.resolve_best_score_version(
            SOURCE_KERNEL,
            33,
            fetch_html=self.fetch_best_v25,
        )
        self.assertEqual(version, 25)
        self.assertEqual(session_id, self.version_sessions[25])

    def test_source_ref_is_always_versioned(self) -> None:
        source_ref, resolution, session_id = (
            kaggle_harvest.resolved_source_kernel(
                self.competition,
                {"ref": SOURCE_KERNEL, "currentVersionNumber": 33},
                fetch_html=self.fetch_best_v25,
            )
        )
        self.assertEqual(source_ref, f"{SOURCE_KERNEL}/25")
        self.assertEqual(resolution, "best-score")
        self.assertEqual(session_id, self.version_sessions[25])

    def test_zero_list_version_uses_get_kernel_metadata(self) -> None:
        requested = []

        def fetch_metadata(kernel: str) -> dict[str, int]:
            requested.append(kernel)
            return {"currentVersionNumber": 33}

        source_ref, resolution, session_id = (
            kaggle_harvest.resolved_source_kernel(
                self.competition,
                {"ref": SOURCE_KERNEL, "currentVersionNumber": 0},
                fetch_html=self.fetch_best_v25,
                fetch_metadata=fetch_metadata,
            )
        )
        self.assertEqual(requested, [SOURCE_KERNEL])
        self.assertEqual(source_ref, f"{SOURCE_KERNEL}/25")
        self.assertEqual(resolution, "best-score")
        self.assertEqual(session_id, self.version_sessions[25])

    def test_missing_metadata_discovers_version_bound_from_public_pages(self) -> None:
        def fetch_without_metadata(_kernel: str) -> dict[str, int]:
            return {"currentVersionNumber": 0}

        source_ref, resolution, session_id = (
            kaggle_harvest.resolved_source_kernel(
                self.competition,
                {"ref": SOURCE_KERNEL, "currentVersionNumber": 0},
                fetch_html=self.fetch_best_v25,
                fetch_metadata=fetch_without_metadata,
            )
        )
        self.assertEqual(source_ref, f"{SOURCE_KERNEL}/25")
        self.assertEqual(resolution, "best-score")
        self.assertEqual(session_id, self.version_sessions[25])

    def test_kernel_list_metadata_retains_current_version(self) -> None:
        row = kaggle_harvest.kernel_metadata_row(
            SimpleNamespace(
                ref=SOURCE_KERNEL,
                current_version_number=33,
                id=126_200_290,
            )
        )
        self.assertEqual(row["currentVersionNumber"], 33)
        self.assertEqual(row["kernelId"], 126_200_290)

    def test_copy_rejects_an_unversioned_source_before_api_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "exact version required"):
                kaggle_harvest.copy_and_run_public_kernel(
                    self.competition,
                    SOURCE_KERNEL,
                    work_dir=Path(temp_dir),
                    output_files=["submission.csv"],
                )

    def test_relay_kernel_pins_exact_source_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            source_ref = f"{SOURCE_KERNEL}/25"
            target_ref = "example/auto-biohub-relay"
            kaggle_harvest.prepare_output_relay_kernel(
                folder,
                comp=self.competition,
                source_ref=source_ref,
                target_ref=target_ref,
                output_files=["submission_clean.csv", "submission.csv"],
            )

            metadata = json.loads(
                (folder / "kernel-metadata.json").read_text(encoding="utf-8")
            )
            notebook = json.loads(
                (folder / "relay.ipynb").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["id"], target_ref)
            self.assertEqual(metadata["kernel_sources"], [source_ref])
            self.assertEqual(
                metadata["competition_sources"],
                [self.competition.slug],
            )
            self.assertFalse(metadata["enable_gpu"])
            code = notebook["cells"][0]["source"]
            compile(code, "relay.ipynb", "exec")
            self.assertIn("biohub-cell-tracking-blend-preprocessings", code)
            self.assertIn(
                "pilkwang/biohub-cell-tracking-blend-preprocessings/versions/25",
                code,
            )
            self.assertIn("submission.csv", code)
            self.assertIn("submission_clean.csv", code)

    def test_candidate_with_two_outputs_runs_only_once(self) -> None:
        report = {
            "competition": self.competition.slug,
            "enabled": True,
            "auto_submit": True,
            "sort_by": "scoreDescending",
            "dry_run": False,
            "kernels_seen": 0,
            "candidates": [],
            "submitted": [],
            "skipped": [],
            "errors": [],
        }
        state = {"schema_version": 1, "submissions": []}
        source_ref = f"{SOURCE_KERNEL}/25"

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            submission = folder / "submission.csv"
            cleaned = folder / "submission_clean.csv"
            submission.touch()
            cleaned.touch()

            with (
                patch.object(
                    kaggle_harvest,
                    "list_kernels",
                    return_value=[{"ref": SOURCE_KERNEL}],
                ),
                patch.object(
                    kaggle_harvest,
                    "resolved_source_kernel",
                    return_value=(source_ref, "best-score", 330025000),
                ),
                patch.object(
                    kaggle_harvest,
                    "copy_and_run_public_kernel",
                    return_value=("example/relay", "1", source_ref),
                ) as run_mock,
                patch.object(kaggle_harvest, "wait_for_kernel_complete"),
                patch.object(
                    kaggle_harvest,
                    "download_output",
                    return_value=[cleaned, submission],
                ),
                patch.object(
                    kaggle_harvest,
                    "validate_submission_file",
                    return_value=[],
                ),
                patch.object(kaggle_harvest, "sha256_file", return_value="abc"),
                patch.object(kaggle_harvest, "submit_file") as submit_mock,
            ):
                kaggle_harvest.process_auto_copy_code_submission(
                    self.competition,
                    state=state,
                    output_dir=folder,
                    work_dir=folder,
                    submit=True,
                    now=kaggle_harvest.utc_now(),
                    report=report,
                )

        run_mock.assert_called_once()
        self.assertEqual(
            run_mock.call_args.kwargs["output_files"],
            [],
        )
        submit_mock.assert_called_once()
        self.assertEqual(report["submitted"][0]["file_name"], "submission.csv")
        self.assertEqual(
            report["candidates"][0]["matching_files"],
            ["submission.csv", "submission_clean.csv"],
        )

    def test_download_output_removes_files_from_an_older_relay_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            target = (
                output_dir
                / self.competition.slug
                / "example__auto-biohub-relay"
            )
            target.mkdir(parents=True)
            (target / "submission.csv").write_text("stale", encoding="utf-8")

            with patch.object(kaggle_harvest, "run_cmd"):
                downloaded = kaggle_harvest.download_output(
                    self.competition,
                    "example/auto-biohub-relay",
                    output_dir,
                )

            self.assertEqual(downloaded, [])
            self.assertFalse((target / "submission.csv").exists())

    def test_only_the_exact_submitted_source_version_is_skipped(self) -> None:
        state = {
            "submissions": [
                {
                    "competition": self.competition.slug,
                    "source_kernel_ref": f"{SOURCE_KERNEL}/25",
                }
            ]
        }
        self.assertTrue(
            kaggle_harvest.source_kernel_already_submitted(
                state,
                self.competition.slug,
                f"{SOURCE_KERNEL}/25",
            )
        )
        self.assertFalse(
            kaggle_harvest.source_kernel_already_submitted(
                state,
                self.competition.slug,
                f"{SOURCE_KERNEL}/26",
            )
        )

    def test_real_run_without_submission_is_reported_as_failure(self) -> None:
        report = {
            "competition": self.competition.slug,
            "enabled": True,
            "auto_submit": True,
            "submitted": [],
            "skipped": [],
        }
        self.assertEqual(
            kaggle_harvest.failed_submission_competitions(
                [report],
                submit=True,
            ),
            [self.competition.slug],
        )
        report["skipped"] = ["submission limit reached"]
        self.assertEqual(
            kaggle_harvest.failed_submission_competitions(
                [report],
                submit=True,
            ),
            [],
        )

    def test_unmapped_best_score_never_falls_back_to_latest(self) -> None:
        def fetch_unmapped(url: str) -> str:
            if "/versions/" not in url:
                return notebook_html(999_999_999)
            version = int(url.rsplit("/versions/", 1)[1])
            return notebook_html(self.version_sessions[version])

        with self.assertRaisesRegex(RuntimeError, "refusing to use latest"):
            kaggle_harvest.resolve_best_score_version(
                SOURCE_KERNEL,
                33,
                fetch_html=fetch_unmapped,
            )

    def test_existing_dataset_version_is_replaced(self) -> None:
        actual = kaggle_harvest.with_pinned_version(
            f"{DEEPCENTER_DATASET}/5",
            {DEEPCENTER_DATASET: 4},
        )
        self.assertEqual(actual, f"{DEEPCENTER_DATASET}/4")

    def test_metadata_uses_exact_notebook_and_dataset_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            metadata_path = folder / "kernel-metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "id": SOURCE_KERNEL,
                        "competition_sources": [],
                        "dataset_sources": [
                            f"{DEEPCENTER_DATASET}/5",
                            "someone/unchanged-dataset",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            pinned_competition = replace(
                self.competition,
                dataset_source_versions={DEEPCENTER_DATASET: 4},
            )
            kaggle_harvest.rewrite_kernel_metadata(
                folder,
                comp=pinned_competition,
                public_ref=f"{SOURCE_KERNEL}/25",
                target_ref="example/copied-notebook",
            )

            rewritten = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                rewritten["dataset_sources"],
                [f"{DEEPCENTER_DATASET}/4", "someone/unchanged-dataset"],
            )
            self.assertEqual(rewritten["source_kernel"], f"{SOURCE_KERNEL}/25")

    def test_invalid_pinned_inputs_are_reported(self) -> None:
        result = SimpleNamespace(
            invalidDatasetSources=[f"{DEEPCENTER_DATASET}/4"],
            invalidKernelSources=None,
            invalidModelSources=None,
            invalidCompetitionSources=None,
        )
        self.assertEqual(
            kaggle_harvest.invalid_push_sources(result),
            [f"dataset:{DEEPCENTER_DATASET}/4"],
        )


if __name__ == "__main__":
    unittest.main()
