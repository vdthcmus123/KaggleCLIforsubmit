import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
        self.assertEqual(
            self.competition.dataset_shapes["44b6_0113de3b"],
            (100, 64, 256, 256),
        )
        self.assertEqual(self.competition.submission_file_name, "submission.csv")
        self.assertTrue(self.competition.require_public_score)
        self.assertEqual(self.competition.max_submissions_per_day, 5)

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
            with self.assertRaisesRegex(ValueError, "versioned kernel ref"):
                kaggle_harvest.copy_and_run_public_kernel(
                    self.competition,
                    SOURCE_KERNEL,
                    work_dir=Path(temp_dir),
                )

    def test_current_source_version_is_pulled_unversioned_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            source_ref = f"{SOURCE_KERNEL}/25"
            api = SimpleNamespace(kernels_pull=Mock())
            with patch.object(
                kaggle_harvest,
                "fetch_kernel_metadata",
                side_effect=[
                    {"currentVersionNumber": 25},
                    {"currentVersionNumber": 25},
                ],
            ):
                pulled_ref = kaggle_harvest.pull_public_kernel_source(
                    api,
                    source_ref,
                    folder,
                )

        self.assertEqual(pulled_ref, SOURCE_KERNEL)
        api.kernels_pull.assert_called_once_with(
            SOURCE_KERNEL,
            path=str(folder),
            metadata=True,
            quiet=True,
        )

    def test_historical_source_is_requested_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            source_ref = f"{SOURCE_KERNEL}/25"
            api = SimpleNamespace(kernels_pull=Mock())
            with patch.object(
                kaggle_harvest,
                "fetch_kernel_metadata",
                return_value={"currentVersionNumber": 33},
            ):
                pulled_ref = kaggle_harvest.pull_public_kernel_source(
                    api,
                    source_ref,
                    folder,
                )

        self.assertEqual(pulled_ref, source_ref)
        api.kernels_pull.assert_called_once_with(
            source_ref,
            path=str(folder),
            metadata=True,
            quiet=True,
        )

    def test_unavailable_historical_source_is_never_relayed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            source_ref = f"{SOURCE_KERNEL}/25"
            api = SimpleNamespace(kernels_pull=Mock(side_effect=RuntimeError("403")))
            with (
                patch.object(
                    kaggle_harvest,
                    "fetch_kernel_metadata",
                    return_value={"currentVersionNumber": 33},
                ),
                self.assertRaisesRegex(
                    kaggle_harvest.ExactSourceUnavailableError,
                    "Refusing to run a different version or relay",
                ),
            ):
                kaggle_harvest.pull_public_kernel_source(
                    api,
                    source_ref,
                    folder,
                )

        api.kernels_pull.assert_called_once()

    def test_copy_runs_the_downloaded_full_source(self) -> None:
        source_ref = f"{SOURCE_KERNEL}/25"
        api = SimpleNamespace(
            kernels_push=Mock(
                return_value=SimpleNamespace(
                    versionNumber=4,
                    url="https://www.kaggle.com/code/example/copied",
                    error=None,
                )
            )
        )

        def fake_pull(_api: object, _source_ref: str, destination: Path) -> str:
            (destination / "source.ipynb").write_text("{}", encoding="utf-8")
            (destination / "kernel-metadata.json").write_text(
                json.dumps(
                    {
                        "id": SOURCE_KERNEL,
                        "code_file": "source.ipynb",
                        "competition_sources": [self.competition.slug],
                        "dataset_sources": [],
                    }
                ),
                encoding="utf-8",
            )
            return SOURCE_KERNEL

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(kaggle_harvest, "kaggle_api", return_value=api),
                patch.object(
                    kaggle_harvest,
                    "kaggle_username",
                    return_value="example",
                ),
                patch.object(
                    kaggle_harvest,
                    "pull_public_kernel_source",
                    side_effect=fake_pull,
                ) as pull_mock,
            ):
                target_ref, version, launched_ref = (
                    kaggle_harvest.copy_and_run_public_kernel(
                        self.competition,
                        source_ref,
                        work_dir=Path(temp_dir),
                    )
                )
                pushed_folder = Path(api.kernels_push.call_args.args[0])
                metadata = json.loads(
                    (pushed_folder / "kernel-metadata.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(target_ref, metadata["id"])
        self.assertEqual(version, "4")
        self.assertEqual(launched_ref, source_ref)
        self.assertEqual(metadata["source_kernel"], source_ref)
        self.assertEqual(metadata["code_file"], "source.ipynb")
        pull_mock.assert_called_once()
        api.kernels_push.assert_called_once()

    def test_candidate_rejects_invalid_native_file_without_using_alternative(
        self,
    ) -> None:
        report = {
            "competition": self.competition.slug,
            "enabled": True,
            "auto_submit": True,
            "sort_by": "scoreDescending",
            "dry_run": False,
            "kernels_seen": 0,
            "candidates": [],
            "submitted": [],
            "rejected_sources": [],
            "skipped": [],
            "errors": [],
        }
        state = {
            "schema_version": 1,
            "submissions": [],
            "failed_submissions": [],
            "rejected_sources": [],
        }
        source_ref = f"{SOURCE_KERNEL}/25"

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            raw_folder = folder / "raw"
            raw_folder.mkdir()
            submission = raw_folder / "submission.csv"
            cleaned = raw_folder / "submission_clean.csv"
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
                    return_value=("example/clone", "1", source_ref),
                ) as run_mock,
                patch.object(
                    kaggle_harvest,
                    "wait_for_kernel_complete",
                ) as wait_mock,
                patch.object(
                    kaggle_harvest,
                    "download_output",
                    return_value=[cleaned, submission],
                ),
                patch.object(
                    kaggle_harvest,
                    "validate_submission_file",
                    side_effect=lambda path, *_args: (
                        ["negative node coordinate t"]
                        if path == submission
                        else []
                    ),
                ),
                patch.object(
                    kaggle_harvest,
                    "sha256_file",
                    return_value="bad-hash",
                ),
                patch.object(
                    kaggle_harvest,
                    "submit_file",
                    return_value=123,
                ) as submit_mock,
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

        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(
            run_mock.call_args_list[0].kwargs,
            {"work_dir": folder},
        )
        self.assertEqual(wait_mock.call_count, 1)
        submit_mock.assert_not_called()
        self.assertEqual(report["submitted"], [])
        self.assertEqual(
            report["candidates"][0]["matching_files"],
            ["submission.csv", "submission_clean.csv"],
        )
        self.assertEqual(len(state["rejected_sources"]), 1)
        self.assertEqual(
            state["rejected_sources"][0]["source_kernel_ref"],
            source_ref,
        )
        self.assertEqual(
            state["rejected_sources"][0]["file_name"],
            "submission.csv",
        )
        self.assertEqual(state["rejected_sources"][0]["sha256"], "bad-hash")

    def test_candidate_without_submission_output_is_persistently_rejected(
        self,
    ) -> None:
        report = {
            "competition": self.competition.slug,
            "enabled": True,
            "auto_submit": True,
            "sort_by": "scoreDescending",
            "dry_run": False,
            "kernels_seen": 0,
            "candidates": [],
            "submitted": [],
            "rejected_sources": [],
            "skipped": [],
            "errors": [],
        }
        state = {
            "schema_version": 1,
            "submissions": [],
            "failed_submissions": [],
            "rejected_sources": [],
        }
        source_ref = f"{SOURCE_KERNEL}/25"

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
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
                    return_value=("example/clone", "1", source_ref),
                ),
                patch.object(kaggle_harvest, "wait_for_kernel_complete"),
                patch.object(kaggle_harvest, "download_output", return_value=[]),
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

        submit_mock.assert_not_called()
        self.assertEqual(len(state["rejected_sources"]), 1)
        self.assertEqual(
            state["rejected_sources"][0]["source_kernel_ref"],
            source_ref,
        )
        self.assertIn("no matching submission", report["skipped"][0])

    def test_unavailable_exact_source_is_persistently_rejected(self) -> None:
        report = {
            "competition": self.competition.slug,
            "candidates": [],
            "submitted": [],
            "rejected_sources": [],
            "skipped": [],
            "errors": [],
        }
        state = {
            "schema_version": 1,
            "submissions": [],
            "failed_submissions": [],
            "rejected_sources": [],
        }
        source_ref = f"{SOURCE_KERNEL}/25"

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
                side_effect=kaggle_harvest.ExactSourceUnavailableError("403"),
            ),
        ):
            kaggle_harvest.process_auto_copy_code_submission(
                self.competition,
                state=state,
                output_dir=Path("submissions"),
                work_dir=Path("work"),
                submit=True,
                now=kaggle_harvest.utc_now(),
                report=report,
            )

        self.assertEqual(len(state["rejected_sources"]), 1)
        self.assertEqual(
            state["rejected_sources"][0]["source_kernel_ref"],
            source_ref,
        )
        self.assertIn("rejected source", report["skipped"][0])
        self.assertEqual(report["errors"], [])

    def test_cell_tracking_validation_rejects_negative_sentinel_nodes(self) -> None:
        shape = {"test_dataset": (2, 3, 4, 5)}
        valid_rows = [
            [0, "test_dataset", "node", 1, 0, 1, 2, 3, -1, -1],
            [1, "test_dataset", "node", 2, 1, 2, 3, 4, -1, -1],
            [2, "test_dataset", "edge", -1, -1, -1, -1, -1, 1, 2],
        ]
        invalid_rows = [
            [0, "test_dataset", "node", 1, -1000, -10000, -10000, -10000, -1, -1],
            [1, "test_dataset", "node", 2, 1, 2, 3, 4, -1, -1],
            [2, "test_dataset", "edge", -1, -1, -1, -1, -1, 1, 2],
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            valid_path = Path(temp_dir) / "submission_clean.csv"
            invalid_path = Path(temp_dir) / "submission.csv"
            for path, rows in ((valid_path, valid_rows), (invalid_path, invalid_rows)):
                with path.open("w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(kaggle_harvest.CELL_TRACKING_COLUMNS)
                    writer.writerows(rows)

            self.assertEqual(
                kaggle_harvest.validate_submission_file(
                    valid_path,
                    kaggle_harvest.CELL_TRACKING_COLUMNS,
                    shape,
                ),
                [],
            )
            errors = kaggle_harvest.validate_submission_file(
                invalid_path,
                kaggle_harvest.CELL_TRACKING_COLUMNS,
                shape,
            )

        self.assertTrue(any("negative node coordinate t" in error for error in errors))
        self.assertTrue(any("negative node coordinate z" in error for error in errors))

    def test_cell_tracking_validation_rejects_duplicate_node_coordinates(self) -> None:
        shape = {"test_dataset": (2, 3, 4, 5)}
        rows = [
            [0, "test_dataset", "node", 1, 0, 1, 2, 3, -1, -1],
            [1, "test_dataset", "node", 2, 0, 1, 2, 3, -1, -1],
            [2, "test_dataset", "edge", -1, -1, -1, -1, -1, 1, 2],
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "submission.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(kaggle_harvest.CELL_TRACKING_COLUMNS)
                writer.writerows(rows)
            errors = kaggle_harvest.validate_submission_file(
                path,
                kaggle_harvest.CELL_TRACKING_COLUMNS,
                shape,
            )

        self.assertTrue(
            any("duplicate node coordinate" in error for error in errors)
        )

    def test_submission_poll_rejects_complete_status_with_scoring_error(self) -> None:
        submission = SimpleNamespace(
            ref=123,
            status="SubmissionStatus.COMPLETE",
            error_description="incorrect format",
            public_score="",
        )
        api = SimpleNamespace(
            competition_submissions=Mock(return_value=[submission])
        )

        with self.assertRaisesRegex(
            kaggle_harvest.SubmissionScoringError,
            "incorrect format",
        ):
            kaggle_harvest.wait_for_submission_result(api, self.competition, 123)

    def test_submission_poll_waits_for_a_public_score(self) -> None:
        pending = SimpleNamespace(
            ref=123,
            status="SubmissionStatus.PENDING",
            error_description="",
            public_score="",
        )
        complete = SimpleNamespace(
            ref=123,
            status="SubmissionStatus.COMPLETE",
            error_description="",
            public_score="0.901",
        )
        api = SimpleNamespace(
            competition_submissions=Mock(
                side_effect=[[pending], [complete]],
            )
        )

        with patch.object(kaggle_harvest.time, "sleep"):
            kaggle_harvest.wait_for_submission_result(
                api,
                self.competition,
                123,
            )

        self.assertEqual(api.competition_submissions.call_count, 2)

    def test_scoring_error_is_recorded_as_a_failed_submission(self) -> None:
        report = {
            "competition": self.competition.slug,
            "enabled": True,
            "auto_submit": True,
            "sort_by": "scoreDescending",
            "dry_run": False,
            "kernels_seen": 0,
            "candidates": [],
            "submitted": [],
            "failed_submissions": [],
            "skipped": [],
            "errors": [],
        }
        state = {
            "schema_version": 1,
            "submissions": [],
            "failed_submissions": [],
        }
        source_ref = f"{SOURCE_KERNEL}/25"

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            submission = folder / "submission.csv"
            submission.touch()
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
                    return_value=("example/clone", "1", source_ref),
                ),
                patch.object(kaggle_harvest, "wait_for_kernel_complete"),
                patch.object(
                    kaggle_harvest,
                    "download_output",
                    return_value=[submission],
                ),
                patch.object(
                    kaggle_harvest,
                    "validate_submission_file",
                    return_value=[],
                ),
                patch.object(kaggle_harvest, "sha256_file", return_value="abc"),
                patch.object(
                    kaggle_harvest,
                    "submit_file",
                    side_effect=kaggle_harvest.SubmissionScoringError(
                        123,
                        "complete",
                        "incorrect format",
                    ),
                ),
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

        self.assertEqual(state["submissions"], [])
        self.assertEqual(len(state["failed_submissions"]), 1)
        self.assertEqual(
            state["failed_submissions"][0]["submission_ref"],
            123,
        )
        self.assertEqual(report["submitted"], [])
        self.assertEqual(len(report["failed_submissions"]), 1)

    def test_download_output_removes_files_from_an_older_clone_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            target = (
                output_dir
                / self.competition.slug
                / "example__auto-biohub-clone"
            )
            target.mkdir(parents=True)
            (target / "submission.csv").write_text("stale", encoding="utf-8")

            with patch.object(kaggle_harvest, "run_cmd"):
                downloaded = kaggle_harvest.download_output(
                    self.competition,
                    "example/auto-biohub-clone",
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
        self.assertTrue(
            kaggle_harvest.source_kernel_already_attempted(
                state,
                self.competition.slug,
                f"{SOURCE_KERNEL}/25",
            )
        )
        self.assertFalse(
            kaggle_harvest.source_kernel_already_attempted(
                state,
                self.competition.slug,
                f"{SOURCE_KERNEL}/26",
            )
        )

    def test_failed_scoring_counts_toward_quota_and_blocks_source_retry(self) -> None:
        now = kaggle_harvest.utc_now()
        source_ref = f"{SOURCE_KERNEL}/25"
        state = {
            "submissions": [],
            "failed_submissions": [
                {
                    "competition": self.competition.slug,
                    "source_kernel_ref": source_ref,
                    "submitted_at_utc": now.isoformat(),
                }
            ],
        }

        self.assertEqual(
            kaggle_harvest.submissions_today(
                state,
                self.competition.slug,
                now,
            ),
            1,
        )
        self.assertFalse(
            kaggle_harvest.source_kernel_already_submitted(
                state,
                self.competition.slug,
                source_ref,
            )
        )
        self.assertTrue(
            kaggle_harvest.source_kernel_already_attempted(
                state,
                self.competition.slug,
                source_ref,
            )
        )

    def test_attempted_hash_in_failed_or_rejected_state_is_skipped(self) -> None:
        state = {
            "submissions": [],
            "failed_submissions": [
                {
                    "competition": self.competition.slug,
                    "sha256": "ABC123",
                }
            ],
            "rejected_sources": [
                {
                    "competition": self.competition.slug,
                    "sha256": "def456",
                }
            ],
        }

        self.assertTrue(
            kaggle_harvest.submission_hash_already_attempted(
                state,
                self.competition.slug,
                "abc123",
            )
        )
        self.assertTrue(
            kaggle_harvest.submission_hash_already_attempted(
                state,
                self.competition.slug,
                "DEF456",
            )
        )
        self.assertFalse(
            kaggle_harvest.submission_hash_already_attempted(
                state,
                self.competition.slug,
                "new-hash",
            )
        )

    def test_harvester_does_not_clone_a_failed_source_version_again(self) -> None:
        source_ref = f"{SOURCE_KERNEL}/25"
        state = {
            "submissions": [],
            "failed_submissions": [
                {
                    "competition": self.competition.slug,
                    "source_kernel_ref": source_ref,
                }
            ],
            "rejected_sources": [],
        }
        report = {
            "competition": self.competition.slug,
            "candidates": [],
            "submitted": [],
            "skipped": [],
            "errors": [],
        }

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
            ) as run_mock,
        ):
            kaggle_harvest.process_auto_copy_code_submission(
                self.competition,
                state=state,
                output_dir=Path("submissions"),
                work_dir=Path("work"),
                submit=True,
                now=kaggle_harvest.utc_now(),
                report=report,
            )

        run_mock.assert_not_called()
        self.assertIn(f"already attempted source {source_ref}", report["skipped"])

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
