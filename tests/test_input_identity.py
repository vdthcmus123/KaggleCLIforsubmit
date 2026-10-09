import unittest

from scripts import kaggle_harvest as h


class InputIdentityTests(unittest.TestCase):
    def test_description_links_are_not_attached_dataset_identities(self) -> None:
        source = {
            "sourceType": "DATA_SOURCE_TYPE_DATASET",
            "description": "Based on https://www.kaggle.com/datasets/other/model).",
            "databundle": {"datasetInfo": {"datasetSlug": "/datasets/actual/weights"}},
            "sourceUrl": "/datasets/actual/weights",
            "slug": "weights",
            "mountSlug": "datasets/actual/weights",
            "versionNumber": 4,
        }
        metadata = h.metadata_sources_from_session([source])
        self.assertEqual(metadata["dataset_sources"], ["actual/weights/4"])
        candidates = h.runtime_mount_specs_from_session([source])[0]["candidates"]
        self.assertIn("/kaggle/input/datasets/actual/weights", candidates)
        self.assertNotIn("/kaggle/input/datasets/other/model", candidates)

    def test_current_competition_slug_wins_over_stale_bundle_identity(self) -> None:
        source = {
            "sourceType": "DATA_SOURCE_TYPE_COMPETITION",
            "databundle": {"competitionInfo": {
                "competitionSlug": "/competitions/old-competition-name",
            }},
            "sourceUrl": "/competitions/current-competition-name",
            "slug": "current-competition-name",
            "mountSlug": "competitions/current-competition-name",
        }
        self.assertEqual(
            h.metadata_sources_from_session([source])["competition_sources"],
            ["current-competition-name"],
        )
        candidates = h.runtime_mount_specs_from_session([source])[0]["candidates"]
        self.assertNotIn("/kaggle/input/competitions/old-competition-name", candidates)

    def test_metadata_falls_back_to_structured_identity_when_source_url_is_absent(self) -> None:
        source = {
            "sourceType": "DATA_SOURCE_TYPE_DATASET",
            "description": "See /datasets/unrelated/example",
            "databundle": {"datasetInfo": {"datasetSlug": "/datasets/actual/weights"}},
            "databundleVersion": {"databundleVersionInfo": {"versionNumber": 7}},
        }
        self.assertEqual(h.metadata_sources_from_session([source])["dataset_sources"],
                         ["actual/weights/7"])

    def test_description_alone_does_not_make_unavailable_input_usable(self) -> None:
        source = {
            "sourceType": "DATA_SOURCE_TYPE_DATASET",
            "description": "Private input. Credits: /datasets/public/other",
        }
        with self.assertRaisesRegex(h.ExactSourceUnavailableError, "Could not resolve"):
            h.metadata_sources_from_session([source])

    def test_mount_slug_resolves_input_identity(self) -> None:
        source = {"sourceType": "DATA_SOURCE_TYPE_DATASET",
                  "mountSlug": "datasets/actual/weights", "versionNumber": 2}
        self.assertEqual(h.metadata_sources_from_session([source])["dataset_sources"],
                         ["actual/weights/2"])

    def test_notebook_and_model_inputs_use_identity_urls(self) -> None:
        sources = [
            {"sourceType": "DATA_SOURCE_TYPE_NOTEBOOK", "sourceUrl": "/code/owner/infer",
             "versionNumber": 3, "description": "/code/someone/other"},
            {"sourceType": "DATA_SOURCE_TYPE_MODEL",
             "sourceUrl": "/models/owner/network/pytorch/default",
             "versionNumber": 2, "description": "/models/other/model/pytorch/v1"},
        ]
        metadata = h.metadata_sources_from_session(sources)
        self.assertEqual(metadata["kernel_sources"], ["owner/infer/3"])
        self.assertEqual(metadata["model_sources"], ["owner/network/pytorch/default/2"])


if __name__ == "__main__":
    unittest.main()
