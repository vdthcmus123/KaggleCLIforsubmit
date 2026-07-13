import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import kaggle_harvest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_KERNEL = "pilkwang/biohub-cell-tracking-blend-preprocessings"
DEEPCENTER_DATASET = "pilkwang/biohub-deepcenter-unet3d-center-prior-v1"


class VersionPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.competition = kaggle_harvest.load_config(ROOT / "config.toml")[0]

    def test_biohub_config_pins_scored_environment(self) -> None:
        self.assertEqual(
            self.competition.source_kernel_versions[SOURCE_KERNEL],
            25,
        )
        self.assertEqual(
            self.competition.dataset_source_versions[DEEPCENTER_DATASET],
            4,
        )

    def test_existing_dataset_version_is_replaced(self) -> None:
        actual = kaggle_harvest.with_pinned_version(
            f"{DEEPCENTER_DATASET}/5",
            self.competition.dataset_source_versions,
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

            kaggle_harvest.rewrite_kernel_metadata(
                folder,
                comp=self.competition,
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
