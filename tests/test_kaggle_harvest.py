import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
