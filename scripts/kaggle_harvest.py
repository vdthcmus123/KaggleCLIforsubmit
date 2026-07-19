#!/usr/bin/env python3
"""Scan public Kaggle kernels and optionally submit matching outputs.

This tool intentionally defaults to dry-run mode. Real submissions require the
`--submit` flag and `auto_submit = true` in the competition config.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


STATE_VERSION = 1
MAX_DISCOVERABLE_KERNEL_VERSION = 16_384
SCRIPT_VERSION_ID_RE = re.compile(
    r"scriptVersionId(?:%3D|=)(\d+)",
    re.IGNORECASE,
)
COMPETITION_ID_RE = re.compile(r"/Competitions/(\d+)", re.IGNORECASE)
EXACT_SCORE_COPY_MODE = "competition-code-score-copy"
KAGGLE_WEB_BASE = "https://www.kaggle.com"


@dataclass(frozen=True)
class Defaults:
    top_n: int = 100
    sort_by: str = "scoreDescending"
    language: str = "python"
    kernel_type: str = "all"
    output_type: str = "data"
    file_pattern: str = r"(?i).*submission.*\.csv$"
    submission_file_name: str | None = None
    required_columns: tuple[str, ...] | None = None
    max_submissions_per_run: int = 1
    max_submit_attempts_per_run: int = 1
    max_submissions_per_day: int = 2
    message_template: str = "AUTO public kernel {kernel_ref} file {file_name}"
    code_competition: bool = False
    auto_copy_notebook: bool = False
    copy_slug_prefix: str = "auto"
    notebook_wait_timeout_seconds: int = 21600
    notebook_poll_seconds: int = 60
    submission_wait_timeout_seconds: int = 1800
    submission_poll_seconds: int = 15
    require_public_score: bool = False
    kernel_session_timeout_seconds: int | None = None
    source_kernel_versions: dict[str, int] = field(default_factory=dict)
    dataset_source_versions: dict[str, int] = field(default_factory=dict)
    dataset_shapes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Competition:
    slug: str
    enabled: bool
    auto_submit: bool
    top_n: int
    sort_by: str
    language: str
    kernel_type: str
    output_type: str
    file_pattern: str
    submission_file_name: str | None
    required_columns: tuple[str, ...] | None
    max_submissions_per_run: int
    max_submit_attempts_per_run: int
    max_submissions_per_day: int
    message_template: str
    search: str | None = None
    code_competition: bool = False
    auto_copy_notebook: bool = False
    copy_slug_prefix: str = "auto"
    notebook_wait_timeout_seconds: int = 21600
    notebook_poll_seconds: int = 60
    submission_wait_timeout_seconds: int = 1800
    submission_poll_seconds: int = 15
    require_public_score: bool = False
    kernel_session_timeout_seconds: int | None = None
    source_kernel_versions: dict[str, int] = field(default_factory=dict)
    dataset_source_versions: dict[str, int] = field(default_factory=dict)
    dataset_shapes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    submit_kernel: str | None = None
    submit_kernel_version: str | None = None


class SubmissionScoringError(RuntimeError):
    def __init__(self, submission_ref: int, status: str, detail: str):
        self.submission_ref = submission_ref
        self.status = status
        self.detail = detail
        super().__init__(
            f"Kaggle submission {submission_ref} failed ({status}): {detail}"
        )


class ExactSourceUnavailableError(RuntimeError):
    """Raised when Kaggle will not expose the requested notebook source version."""


class KaggleWebClient:
    """Small client for the public web endpoints used by Kaggle's Code tab."""

    def __init__(self, session: Any | None = None) -> None:
        if session is None:
            import requests  # type: ignore[import-not-found]

            session = requests.Session()
        self.session = session
        self.referer: str | None = None

    @staticmethod
    def _headers(*, accept: str, referer: str, xsrf_token: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Origin": KAGGLE_WEB_BASE,
            "Referer": referer,
            "User-Agent": "KagglePublicSubmitter/1.0",
            "X-XSRF-TOKEN": xsrf_token,
        }

    def get_text(self, url: str) -> str:
        response = self.session.get(
            url,
            headers={"User-Agent": "KagglePublicSubmitter/1.0"},
            timeout=60,
        )
        response.raise_for_status()
        self.referer = response.url
        return response.text

    def _post(self, endpoint: str, payload: dict[str, Any], *, accept: str) -> Any:
        if self.referer is None:
            raise RuntimeError("Kaggle web client must load a public page first.")
        xsrf_token = self.session.cookies.get("XSRF-TOKEN")
        if not xsrf_token:
            raise RuntimeError("Kaggle did not issue an XSRF token.")
        response = self.session.post(
            f"{KAGGLE_WEB_BASE}/api/i/kernels.KernelsService/{endpoint}",
            json=payload,
            headers=self._headers(
                accept=accept,
                referer=self.referer,
                xsrf_token=xsrf_token,
            ),
            timeout=60,
        )
        response.raise_for_status()
        return response

    def post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._post(endpoint, payload, accept="application/json")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"Kaggle {endpoint} returned a non-object response.")
        return result

    def post_bytes(self, endpoint: str, payload: dict[str, Any]) -> bytes:
        return bytes(self._post(endpoint, payload, accept="*/*").content)


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_config(path: Path) -> list[Competition]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    defaults_raw = raw.get("defaults", {})
    defaults = Defaults(
        top_n=int(defaults_raw.get("top_n", Defaults.top_n)),
        sort_by=str(defaults_raw.get("sort_by", Defaults.sort_by)),
        language=str(defaults_raw.get("language", Defaults.language)),
        kernel_type=str(defaults_raw.get("kernel_type", Defaults.kernel_type)),
        output_type=str(defaults_raw.get("output_type", Defaults.output_type)),
        file_pattern=str(defaults_raw.get("file_pattern", Defaults.file_pattern)),
        submission_file_name=maybe_str(
            defaults_raw.get("submission_file_name", Defaults.submission_file_name)
        ),
        required_columns=maybe_str_list(
            defaults_raw.get("required_columns", Defaults.required_columns)
        ),
        max_submissions_per_run=int(
            defaults_raw.get(
                "max_submissions_per_run", Defaults.max_submissions_per_run
            )
        ),
        max_submit_attempts_per_run=int(
            defaults_raw.get(
                "max_submit_attempts_per_run",
                Defaults.max_submit_attempts_per_run,
            )
        ),
        max_submissions_per_day=int(
            defaults_raw.get(
                "max_submissions_per_day", Defaults.max_submissions_per_day
            )
        ),
        message_template=str(
            defaults_raw.get("message_template", Defaults.message_template)
        ),
        code_competition=bool(
            defaults_raw.get("code_competition", Defaults.code_competition)
        ),
        auto_copy_notebook=bool(
            defaults_raw.get("auto_copy_notebook", Defaults.auto_copy_notebook)
        ),
        copy_slug_prefix=str(
            defaults_raw.get("copy_slug_prefix", Defaults.copy_slug_prefix)
        ),
        notebook_wait_timeout_seconds=int(
            defaults_raw.get(
                "notebook_wait_timeout_seconds",
                Defaults.notebook_wait_timeout_seconds,
            )
        ),
        notebook_poll_seconds=int(
            defaults_raw.get(
                "notebook_poll_seconds",
                Defaults.notebook_poll_seconds,
            )
        ),
        submission_wait_timeout_seconds=int(
            defaults_raw.get(
                "submission_wait_timeout_seconds",
                Defaults.submission_wait_timeout_seconds,
            )
        ),
        submission_poll_seconds=int(
            defaults_raw.get(
                "submission_poll_seconds",
                Defaults.submission_poll_seconds,
            )
        ),
        require_public_score=bool(
            defaults_raw.get("require_public_score", Defaults.require_public_score)
        ),
        kernel_session_timeout_seconds=maybe_int(
            defaults_raw.get(
                "kernel_session_timeout_seconds",
                Defaults.kernel_session_timeout_seconds,
            )
        ),
        source_kernel_versions=maybe_version_map(
            defaults_raw.get("source_kernel_versions"),
            "defaults.source_kernel_versions",
        ),
        dataset_source_versions=maybe_version_map(
            defaults_raw.get("dataset_source_versions"),
            "defaults.dataset_source_versions",
        ),
        dataset_shapes=maybe_dataset_shapes(
            defaults_raw.get("dataset_shapes"),
            "defaults.dataset_shapes",
        ),
    )

    competitions = []
    for item in raw.get("competitions", []):
        slug = str(item.get("slug", "")).strip()
        if not slug:
            raise ValueError("Every [[competitions]] entry needs a slug.")
        competitions.append(
            Competition(
                slug=slug,
                enabled=bool(item.get("enabled", True)),
                auto_submit=bool(item.get("auto_submit", False)),
                top_n=int(item.get("top_n", defaults.top_n)),
                sort_by=str(item.get("sort_by", defaults.sort_by)),
                language=str(item.get("language", defaults.language)),
                kernel_type=str(item.get("kernel_type", defaults.kernel_type)),
                output_type=str(item.get("output_type", defaults.output_type)),
                file_pattern=str(item.get("file_pattern", defaults.file_pattern)),
                submission_file_name=maybe_str(
                    item.get("submission_file_name", defaults.submission_file_name)
                ),
                required_columns=maybe_str_list(
                    item.get("required_columns", defaults.required_columns)
                ),
                max_submissions_per_run=int(
                    item.get(
                        "max_submissions_per_run",
                        defaults.max_submissions_per_run,
                    )
                ),
                max_submit_attempts_per_run=int(
                    item.get(
                        "max_submit_attempts_per_run",
                        defaults.max_submit_attempts_per_run,
                    )
                ),
                max_submissions_per_day=int(
                    item.get(
                        "max_submissions_per_day",
                        defaults.max_submissions_per_day,
                    )
                ),
                message_template=str(
                    item.get("message_template", defaults.message_template)
                ),
                search=maybe_str(item.get("search")),
                code_competition=bool(
                    item.get("code_competition", defaults.code_competition)
                ),
                auto_copy_notebook=bool(
                    item.get("auto_copy_notebook", defaults.auto_copy_notebook)
                ),
                copy_slug_prefix=str(
                    item.get("copy_slug_prefix", defaults.copy_slug_prefix)
                ),
                notebook_wait_timeout_seconds=int(
                    item.get(
                        "notebook_wait_timeout_seconds",
                        defaults.notebook_wait_timeout_seconds,
                    )
                ),
                notebook_poll_seconds=int(
                    item.get("notebook_poll_seconds", defaults.notebook_poll_seconds)
                ),
                submission_wait_timeout_seconds=int(
                    item.get(
                        "submission_wait_timeout_seconds",
                        defaults.submission_wait_timeout_seconds,
                    )
                ),
                submission_poll_seconds=int(
                    item.get(
                        "submission_poll_seconds",
                        defaults.submission_poll_seconds,
                    )
                ),
                require_public_score=bool(
                    item.get("require_public_score", defaults.require_public_score)
                ),
                kernel_session_timeout_seconds=maybe_int(
                    item.get(
                        "kernel_session_timeout_seconds",
                        defaults.kernel_session_timeout_seconds,
                    )
                ),
                source_kernel_versions=maybe_version_map(
                    item.get(
                        "source_kernel_versions",
                        defaults.source_kernel_versions,
                    ),
                    f"{slug}.source_kernel_versions",
                ),
                dataset_source_versions=maybe_version_map(
                    item.get(
                        "dataset_source_versions",
                        defaults.dataset_source_versions,
                    ),
                    f"{slug}.dataset_source_versions",
                ),
                dataset_shapes=maybe_dataset_shapes(
                    item.get("dataset_shapes", defaults.dataset_shapes),
                    f"{slug}.dataset_shapes",
                ),
                submit_kernel=maybe_str(item.get("submit_kernel")),
                submit_kernel_version=maybe_str(item.get("submit_kernel_version")),
            )
        )
    return competitions


def maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text else None


def maybe_str_list(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = tuple(str(item).strip() for item in value if str(item).strip())
        return items or None
    text = str(value).strip()
    return (text,) if text else None


def unversioned_ref(ref: str) -> str:
    parts = ref.strip().strip("/").split("/")
    if len(parts) == 3 and parts[-1].isdigit():
        parts.pop()
    return "/".join(parts)


def parse_versioned_kernel_ref(ref: str) -> tuple[str, int]:
    normalized = ref.strip().strip("/")
    parts = normalized.split("/")
    if len(parts) != 3 or not all(parts[:2]) or not parts[2].isdigit():
        raise ValueError(f"Expected versioned kernel ref owner/slug/N, got {ref!r}.")
    version = int(parts[2])
    if version <= 0:
        raise ValueError(f"Kernel version must be positive, got {ref!r}.")
    return "/".join(parts[:2]), version


def maybe_version_map(value: Any, setting_name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{setting_name} must be a TOML table.")

    result: dict[str, int] = {}
    for raw_ref, raw_version in value.items():
        ref = unversioned_ref(str(raw_ref))
        if len(ref.split("/")) != 2:
            raise ValueError(
                f"{setting_name} key {raw_ref!r} must be owner/slug."
            )
        if isinstance(raw_version, bool):
            raise ValueError(
                f"{setting_name}[{raw_ref!r}] must be a positive integer."
            )
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{setting_name}[{raw_ref!r}] must be a positive integer."
            ) from exc
        if version <= 0:
            raise ValueError(
                f"{setting_name}[{raw_ref!r}] must be a positive integer."
            )
        result[ref] = version
    return result


def maybe_dataset_shapes(
    value: Any,
    setting_name: str,
) -> dict[str, tuple[int, int, int, int]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{setting_name} must be a TOML table.")

    result: dict[str, tuple[int, int, int, int]] = {}
    for raw_dataset, raw_shape in value.items():
        dataset = str(raw_dataset).strip()
        if not dataset:
            raise ValueError(f"{setting_name} contains an empty dataset name.")
        if not isinstance(raw_shape, (list, tuple)) or len(raw_shape) != 4:
            raise ValueError(
                f"{setting_name}[{raw_dataset!r}] must be [t, z, y, x]."
            )
        try:
            shape = (
                int(raw_shape[0]),
                int(raw_shape[1]),
                int(raw_shape[2]),
                int(raw_shape[3]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{setting_name}[{raw_dataset!r}] must contain four integers."
            ) from exc
        if any(size <= 0 for size in shape):
            raise ValueError(
                f"{setting_name}[{raw_dataset!r}] dimensions must be positive."
            )
        result[dataset] = shape
    return result


def with_pinned_version(ref: str, versions: dict[str, int]) -> str:
    base_ref = unversioned_ref(ref)
    version = versions.get(base_ref)
    return f"{base_ref}/{version}" if version is not None else ref


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": STATE_VERSION,
            "submissions": [],
            "failed_submissions": [],
            "rejected_sources": [],
        }
    with path.open("r", encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("schema_version") != STATE_VERSION:
        raise ValueError(f"Unsupported state schema in {path}")
    state.setdefault("submissions", [])
    state.setdefault("failed_submissions", [])
    state.setdefault("rejected_sources", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_path.replace(path)


def run_cmd(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(shlex.quote(part) for part in args)
    print(f"+ {printable}")
    return subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def run_json(args: list[str]) -> Any:
    proc = run_cmd(args, capture=True)
    stdout = proc.stdout.strip()
    if not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        rows = parse_csv(stdout)
        if rows:
            return rows
        print(stdout)
        raise


def parse_csv(text: str) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(text.splitlines())
        if not reader.fieldnames:
            return []
        return [dict(row) for row in reader]
    except csv.Error:
        return []


def get_first(row: dict[str, Any], keys: list[str]) -> str | None:
    normalized = {str(k).lower().replace(" ", ""): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(key.lower().replace(" ", ""))
        if value not in (None, ""):
            return str(value)
    return None


def object_value(value: Any, names: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return None
    for name in names:
        item = getattr(value, name, None)
        if item is not None:
            return item
    return None


def kernel_metadata_row(metadata: Any) -> dict[str, Any]:
    fields = {
        "ref": ("ref",),
        "title": ("title",),
        "author": ("author",),
        "lastRunTime": ("last_run_time", "lastRunTime"),
        "totalVotes": ("total_votes", "totalVotes"),
        "currentVersionNumber": (
            "current_version_number",
            "currentVersionNumber",
        ),
        "kernelId": ("id",),
    }
    row: dict[str, Any] = {}
    for output_name, names in fields.items():
        value = object_value(metadata, names)
        if isinstance(value, datetime):
            value = value.isoformat()
        if value is not None:
            row[output_name] = value
    return row


def competition_id_from_html(html: str) -> int:
    match = COMPETITION_ID_RE.search(html)
    if not match:
        raise ValueError("Kaggle competition page did not expose its numeric ID.")
    return int(match.group(1))


def competition_code_list_request(
    comp: Competition,
    competition_id: int,
) -> dict[str, Any]:
    if comp.sort_by != "scoreDescending":
        raise ValueError(
            "Exact Competition Code selection requires sort_by='scoreDescending'."
        )
    return {
        "kernelFilterCriteria": {
            "search": comp.search or "",
            "listRequest": {
                "competitionId": competition_id,
                "sortBy": "SCORE_DESCENDING",
                "pageSize": min(max(comp.top_n, 1), 100),
                "group": "EVERYONE",
                "page": 1,
                "modelIds": [],
                "modelInstanceIds": [],
                "excludeKernelIds": [],
                "tagIds": "",
                "excludeResultsFilesOutputs": False,
                "wantOutputFiles": False,
                "excludeNonAccessedDatasources": True,
            },
        },
        "detailFilterCriteria": {
            "deletedAccessBehavior": "RETURN_NOTHING",
            "unauthorizedAccessBehavior": "RETURN_NOTHING",
            "excludeResultsFilesOutputs": False,
            "wantOutputFiles": False,
            "kernelIds": [],
            "outputFileTypes": [],
            "includeInvalidDataSources": False,
        },
        "readMask": "pinnedKernels",
    }


def list_competition_code_kernels(
    comp: Competition,
    *,
    web_client: KaggleWebClient | None = None,
) -> list[dict[str, Any]]:
    client = web_client or KaggleWebClient()
    page_url = f"{KAGGLE_WEB_BASE}/competitions/{comp.slug}/code"
    html = client.get_text(page_url)
    competition_id = competition_id_from_html(html)
    print(
        "+ Kaggle Competition Code list "
        f"{comp.slug} --sort-by Public-Score-descending --unpinned"
    )
    result = client.post_json(
        "ListKernels",
        competition_code_list_request(comp, competition_id),
    )
    raw_kernels = result.get("kernels") or []
    if not isinstance(raw_kernels, list):
        raise ValueError("Kaggle Competition Code response has invalid kernels data.")

    rows: list[dict[str, Any]] = []
    for raw in raw_kernels:
        if not isinstance(raw, dict):
            continue
        script_url = maybe_str(raw.get("scriptUrl"))
        match = re.search(r"/code/([^/\s]+/[^/\s?#]+)", script_url or "")
        if not match:
            continue
        row = dict(raw)
        row["ref"] = match.group(1)
        row["kernelId"] = raw.get("id")
        rows.append(row)
    return rows


def list_kernels(comp: Competition) -> list[dict[str, Any]]:
    if comp.code_competition and comp.auto_copy_notebook:
        return list_competition_code_kernels(comp)

    args = [
        "kaggle",
        "kernels",
        "list",
        "--competition",
        comp.slug,
        "--page-size",
        str(comp.top_n),
        "--sort-by",
        comp.sort_by,
        "--format",
        "json",
    ]
    if comp.search:
        args += ["--search", comp.search]
    if comp.language != "all":
        args += ["--language", comp.language]
    if comp.kernel_type != "all":
        args += ["--kernel-type", comp.kernel_type]
    if comp.output_type != "all":
        args += ["--output-type", comp.output_type]

    printable = " ".join(shlex.quote(part) for part in args)
    print(f"+ {printable}")
    data = kaggle_api().kernels_list(
        page=1,
        page_size=comp.top_n,
        competition=comp.slug,
        search=comp.search,
        language=comp.language,
        kernel_type=comp.kernel_type,
        output_type=comp.output_type,
        sort_by=comp.sort_by,
    )
    return [kernel_metadata_row(item) for item in (data or []) if item is not None]


def current_kernel_version(row: dict[str, Any]) -> int | None:
    raw = get_first(
        row,
        ["currentVersionNumber", "current_version_number", "versionNumber"],
    )
    if raw is None:
        return None
    version = int(raw)
    if version <= 0:
        return None
    return version


def fetch_kernel_metadata(kernel: str, *, api: Any | None = None) -> dict[str, Any]:
    from kagglesdk.kernels.types.kernels_api_service import (  # type: ignore[import-not-found]
        ApiGetKernelRequest,
    )

    base_ref = unversioned_ref(kernel)
    parts = base_ref.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid kernel ref: {kernel}")

    request = ApiGetKernelRequest()
    request.user_name, request.kernel_slug = parts
    api_client = api or kaggle_api()
    print(f"+ Kaggle GetKernel metadata {base_ref}")
    with api_client.build_kaggle_client() as client:
        response = client.kernels.kernels_api_client.get_kernel(request)

    metadata = getattr(response, "metadata", None)
    if metadata is None:
        raise RuntimeError(f"Kaggle GetKernel returned no metadata for {base_ref}.")
    return kernel_metadata_row(metadata)


def notebook_page_url(kernel: str, version: int | None = None) -> str:
    parts = unversioned_ref(kernel).split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid kernel ref: {kernel}")
    owner, slug = (quote(part, safe="") for part in parts)
    url = f"https://www.kaggle.com/code/{owner}/{slug}"
    if version is not None:
        if version <= 0:
            raise ValueError(f"Invalid kernel version: {version}")
        url += f"/versions/{version}"
    return url


def fetch_public_html(url: str, timeout_seconds: int = 30) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "KagglePublicSubmitter/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code != 429 and exc.code < 500:
                break
        except (TimeoutError, URLError) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"Could not read public Kaggle page: {url}") from last_error


def script_version_id_from_html(html: str) -> int:
    ids = {int(match) for match in SCRIPT_VERSION_ID_RE.findall(html)}
    if not ids:
        raise ValueError("Kaggle page did not expose a scriptVersionId.")
    if len(ids) != 1:
        raise ValueError(f"Kaggle page exposed ambiguous scriptVersionIds: {ids}")
    return ids.pop()


def resolve_best_score_version(
    kernel: str,
    current_version: int | None,
    *,
    fetch_html: Callable[[str], str] | None = None,
) -> tuple[int, int]:
    if current_version is not None and current_version <= 0:
        current_version = None

    fetch = fetch_html or fetch_public_html
    target_session_id = script_version_id_from_html(
        fetch(notebook_page_url(kernel))
    )
    cache: dict[int, int] = {}
    failed_versions: set[int] = set()

    def session_id(version: int) -> int:
        if version not in cache:
            cache[version] = script_version_id_from_html(
                fetch(notebook_page_url(kernel, version))
            )
        return cache[version]

    if current_version is None:
        probe = 1
        while probe <= MAX_DISCOVERABLE_KERNEL_VERSION:
            try:
                candidate_id = session_id(probe)
            except Exception:  # noqa: BLE001 - the probe can be above latest.
                failed_versions.add(probe)
                current_version = probe - 1
                break
            if candidate_id == target_session_id:
                return probe, target_session_id
            if candidate_id > target_session_id:
                current_version = probe
                break
            probe *= 2
        else:
            raise RuntimeError(
                f"Could not discover a version bound for {kernel} below "
                f"V{MAX_DISCOVERABLE_KERNEL_VERSION}."
            )

    if current_version <= 0:
        raise RuntimeError(f"Could not discover any published version of {kernel}.")

    low, high = 1, current_version
    while low <= high:
        middle = (low + high) // 2
        try:
            candidate_id = session_id(middle)
        except Exception:  # noqa: BLE001 - fall back to a hole-tolerant scan.
            failed_versions.add(middle)
            break
        if candidate_id == target_session_id:
            return middle, target_session_id
        if candidate_id < target_session_id:
            low = middle + 1
        else:
            high = middle - 1

    for version in range(current_version, 0, -1):
        if cache.get(version) == target_session_id:
            return version, target_session_id
        if version in cache or version in failed_versions:
            continue
        try:
            if session_id(version) == target_session_id:
                return version, target_session_id
        except Exception:  # noqa: BLE001 - one deleted version must not hide others.
            failed_versions.add(version)

    raise RuntimeError(
        f"Could not map best-score scriptVersionId {target_session_id} to any "
        f"published version of {kernel}; refusing to use latest."
    )


def required_positive_int(value: Any, field_name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Kaggle did not expose a valid {field_name}.") from exc
    if result <= 0:
        raise ValueError(f"Kaggle did not expose a valid {field_name}.")
    return result


def resolve_competition_code_version(
    row: dict[str, Any],
    *,
    web_client: KaggleWebClient | None = None,
) -> tuple[int, int]:
    ref = kernel_ref(row)
    if not ref:
        raise ValueError("Could not find kernel ref in Competition Code row.")
    kernel_id = required_positive_int(row.get("kernelId"), "kernelId")
    session_id = required_positive_int(
        row.get("scriptVersionId"),
        "score-linked scriptVersionId",
    )

    client = web_client or KaggleWebClient()
    client.get_text(notebook_page_url(ref))
    response = client.post_json(
        "ListKernelVersions",
        {"kernelId": kernel_id, "sortOption": "VERSION_ID"},
    )
    items = response.get("items") or []
    if not isinstance(items, list):
        raise ValueError("Kaggle returned invalid notebook version data.")
    for item in items:
        if not isinstance(item, dict):
            continue
        run = item.get("run") or {}
        version = item.get("version") or {}
        if int(run.get("id") or 0) != session_id:
            continue
        version_number = required_positive_int(
            version.get("versionNumber"),
            "score-linked versionNumber",
        )
        return version_number, session_id

    raise RuntimeError(
        f"Kaggle's score card points to session {session_id}, but that session "
        f"was not found in the public version history for {ref}."
    )


def resolved_source_kernel(
    comp: Competition,
    row: dict[str, Any],
    *,
    fetch_html: Callable[[str], str] | None = None,
    fetch_metadata: Callable[[str], dict[str, Any]] | None = None,
) -> tuple[str, str, int | None]:
    ref = kernel_ref(row)
    if not ref:
        raise ValueError("Could not find kernel ref.")
    base_ref = unversioned_ref(ref)
    configured_version = comp.source_kernel_versions.get(base_ref)
    if configured_version is not None:
        return f"{base_ref}/{configured_version}", "configured", None

    if row.get("scriptVersionId") is not None:
        version, session_id = resolve_competition_code_version(row)
        score = row.get("bestPublicScore")
        print(
            f"Selected first unpinned Public Score notebook: {base_ref} -> "
            f"V{version} (scriptVersionId {session_id}, score {score})"
        )
        return f"{base_ref}/{version}", "competition-code-score", session_id

    current_version = current_kernel_version(row)
    if current_version is None:
        metadata_reader = fetch_metadata or fetch_kernel_metadata
        try:
            current_version = current_kernel_version(metadata_reader(base_ref))
        except Exception as exc:  # noqa: BLE001 - public HTML is the final fallback.
            print(
                f"Could not read current version metadata for {base_ref}: "
                f"{error_text(exc)}"
            )
        if current_version is None:
            print(
                f"Kaggle did not expose currentVersionNumber for {base_ref}; "
                "discovering the version range from public pages."
            )

    version, session_id = resolve_best_score_version(
        base_ref,
        current_version,
        fetch_html=fetch_html,
    )
    print(
        f"Resolved best-score notebook: {base_ref} -> V{version} "
        f"(scriptVersionId {session_id})"
    )
    return f"{base_ref}/{version}", "best-score", session_id


def kernel_ref(row: dict[str, Any]) -> str | None:
    ref = get_first(row, ["ref", "kernelRef", "kernel", "id"])
    if ref and "/" in ref:
        return ref
    url = get_first(row, ["url", "link"])
    if url:
        match = re.search(r"/code/([^/\s]+/[^/\s?#]+)", url)
        if match:
            return match.group(1)
    return ref


def list_kernel_files(kernel: str) -> list[dict[str, Any]]:
    data = run_json(
        [
            "kaggle",
            "kernels",
            "files",
            kernel,
            "--page-size",
            "200",
            "--format",
            "json",
        ]
    )
    if isinstance(data, dict):
        data = data.get("files", data.get("rows", data.get("items", [])))
    if not isinstance(data, list):
        raise ValueError(f"Unexpected kaggle kernels files output for {kernel}.")
    return [row for row in data if isinstance(row, dict)]


def output_file_name(row: dict[str, Any]) -> str | None:
    return get_first(row, ["name", "fileName", "filename", "path"])


def already_submitted(
    state: dict[str, Any],
    competition: str,
    kernel: str,
    file_name: str,
    kernel_version: str | None = None,
) -> bool:
    for sub in state.get("submissions", []):
        if sub.get("competition") != competition:
            continue
        if sub.get("kernel_ref") != kernel:
            continue
        if sub.get("file_name") != file_name:
            continue
        if kernel_version is not None and str(sub.get("kernel_version", "")) != str(
            kernel_version
        ):
            continue
        return True
    return False


def source_kernel_already_submitted(
    state: dict[str, Any],
    competition: str,
    source_ref: str,
) -> bool:
    return any(
        sub.get("competition") == competition
        and sub.get("source_kernel_ref") == source_ref
        for sub in state.get("submissions", [])
    )


def source_kernel_already_attempted(
    state: dict[str, Any],
    competition: str,
    source_ref: str,
    *,
    execution_mode: str | None = None,
) -> bool:
    attempts = [
        *state.get("submissions", []),
        *state.get("failed_submissions", []),
        *state.get("rejected_sources", []),
    ]
    return any(
        item.get("competition") == competition
        and item.get("source_kernel_ref") == source_ref
        and (
            execution_mode is None
            or item.get("execution_mode") == execution_mode
            or (
                item.get("execution_mode") is None
                and execution_mode != EXACT_SCORE_COPY_MODE
            )
        )
        for item in attempts
    )


def submission_hash_already_attempted(
    state: dict[str, Any],
    competition: str,
    file_hash: str,
    *,
    execution_mode: str | None = None,
) -> bool:
    if not file_hash:
        return False
    attempts = [
        *state.get("submissions", []),
        *state.get("failed_submissions", []),
        *state.get("rejected_sources", []),
    ]
    return any(
        item.get("competition") == competition
        and str(item.get("sha256", "")).lower() == file_hash.lower()
        and (
            execution_mode is None
            or item.get("execution_mode") == execution_mode
            or (
                item.get("execution_mode") is None
                and execution_mode != EXACT_SCORE_COPY_MODE
            )
        )
        for item in attempts
    )


def submissions_today(state: dict[str, Any], competition: str, now: datetime) -> int:
    today = now.date().isoformat()
    count = 0
    attempts = [
        *state.get("submissions", []),
        *state.get("failed_submissions", []),
    ]
    for sub in attempts:
        if sub.get("competition") != competition:
            continue
        submitted_at = str(sub.get("submitted_at_utc", ""))
        if submitted_at.startswith(today):
            count += 1
    return count


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


CELL_TRACKING_COLUMNS = (
    "id",
    "dataset",
    "row_type",
    "node_id",
    "t",
    "z",
    "y",
    "x",
    "source_id",
    "target_id",
)
CELL_TRACKING_INTEGER_COLUMNS = (
    "id",
    "node_id",
    "t",
    "z",
    "y",
    "x",
    "source_id",
    "target_id",
)


def validate_cell_tracking_rows(
    path: Path,
    dataset_shapes: dict[str, tuple[int, int, int, int]],
) -> list[str]:
    issue_counts: dict[str, int] = {}
    issue_examples: dict[str, str] = {}
    actual_datasets: set[str] = set()
    node_ids: dict[str, set[int]] = defaultdict(set)
    node_coordinates: dict[str, set[tuple[int, int, int, int]]] = defaultdict(set)
    edges: list[tuple[int, str, int, int]] = []
    row_count = 0

    def issue(code: str, detail: str) -> None:
        issue_counts[code] = issue_counts.get(code, 0) + 1
        issue_examples.setdefault(code, detail)

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for index, row in enumerate(reader):
                row_count += 1
                csv_line = index + 2
                if None in row:
                    issue("extra fields", f"CSV line {csv_line}")

                dataset = (row.get("dataset") or "").strip()
                if not dataset:
                    issue("empty dataset", f"CSV line {csv_line}")
                else:
                    actual_datasets.add(dataset)

                values: dict[str, int] = {}
                integers_valid = True
                for column in CELL_TRACKING_INTEGER_COLUMNS:
                    raw = (row.get(column) or "").strip()
                    try:
                        value = int(raw)
                    except (TypeError, ValueError):
                        issue(
                            f"invalid integer {column}",
                            f"CSV line {csv_line}: {raw!r}",
                        )
                        integers_valid = False
                        continue
                    if raw != str(value):
                        issue(
                            f"non-canonical integer {column}",
                            f"CSV line {csv_line}: {raw!r}",
                        )
                    values[column] = value

                if not integers_valid:
                    continue
                if values["id"] != index:
                    issue(
                        "non-consecutive id",
                        f"CSV line {csv_line}: expected {index}, got {values['id']}",
                    )

                row_type = (row.get("row_type") or "").strip()
                if row_type == "node":
                    if values["node_id"] < 0:
                        issue(
                            "negative node_id",
                            f"CSV line {csv_line}: {values['node_id']}",
                        )
                    if values["node_id"] in node_ids[dataset]:
                        issue(
                            "duplicate node_id",
                            f"CSV line {csv_line}: {dataset}/{values['node_id']}",
                        )
                    node_ids[dataset].add(values["node_id"])

                    for column in ("t", "z", "y", "x"):
                        if values[column] < 0:
                            issue(
                                f"negative node coordinate {column}",
                                f"CSV line {csv_line}: {values[column]}",
                            )
                    shape = dataset_shapes.get(dataset)
                    if shape is not None:
                        for column, size in zip(("t", "z", "y", "x"), shape):
                            if values[column] >= size:
                                issue(
                                    f"node coordinate {column} out of bounds",
                                    f"CSV line {csv_line}: {values[column]} >= {size}",
                                )
                    coordinate = tuple(
                        values[column] for column in ("t", "z", "y", "x")
                    )
                    if coordinate in node_coordinates[dataset]:
                        issue(
                            "duplicate node coordinate",
                            f"CSV line {csv_line}: {dataset}/{coordinate}",
                        )
                    node_coordinates[dataset].add(coordinate)
                    if values["source_id"] != -1 or values["target_id"] != -1:
                        issue(
                            "node source/target must be -1",
                            f"CSV line {csv_line}",
                        )
                elif row_type == "edge":
                    if any(
                        values[column] != -1
                        for column in ("node_id", "t", "z", "y", "x")
                    ):
                        issue(
                            "edge node fields must be -1",
                            f"CSV line {csv_line}",
                        )
                    if values["source_id"] < 0 or values["target_id"] < 0:
                        issue(
                            "negative edge endpoint",
                            f"CSV line {csv_line}: "
                            f"{values['source_id']}->{values['target_id']}",
                        )
                    edges.append(
                        (
                            csv_line,
                            dataset,
                            values["source_id"],
                            values["target_id"],
                        )
                    )
                else:
                    issue(
                        "invalid row_type",
                        f"CSV line {csv_line}: {row_type!r}",
                    )
    except (UnicodeDecodeError, csv.Error) as exc:
        return [f"could not parse CSV rows: {exc}"]

    if row_count == 0:
        issue("no data rows", "CSV only contains a header")

    for csv_line, dataset, source_id, target_id in edges:
        if source_id not in node_ids[dataset]:
            issue(
                "edge source_id not found",
                f"CSV line {csv_line}: {dataset}/{source_id}",
            )
        if target_id not in node_ids[dataset]:
            issue(
                "edge target_id not found",
                f"CSV line {csv_line}: {dataset}/{target_id}",
            )

    if dataset_shapes:
        expected_datasets = set(dataset_shapes)
        for dataset in sorted(expected_datasets - actual_datasets):
            issue("missing test dataset", dataset)
        for dataset in sorted(actual_datasets - expected_datasets):
            issue("unexpected test dataset", dataset)

    return [
        f"{code}: {count}; first occurrence: {issue_examples[code]}"
        for code, count in issue_counts.items()
    ]


def validate_submission_file(
    path: Path,
    required_columns: tuple[str, ...] | None,
    dataset_shapes: dict[str, tuple[int, int, int, int]] | None = None,
) -> list[str]:
    if not required_columns:
        return []
    if path.suffix.lower() != ".csv":
        return [f"expected .csv, got {path.name}"]

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), None)
    except UnicodeDecodeError as exc:
        return [f"could not read CSV as UTF-8: {exc}"]
    except csv.Error as exc:
        return [f"could not parse CSV header: {exc}"]

    if header is None:
        return ["CSV is empty"]

    actual = tuple(column.strip() for column in header)
    if actual != required_columns:
        return [
            "CSV header mismatch: expected "
            f"{','.join(required_columns)}; got {','.join(actual)}"
        ]
    if actual == CELL_TRACKING_COLUMNS:
        return validate_cell_tracking_rows(path, dataset_shapes or {})
    return []


def find_downloaded_files(folder: Path, pattern: re.Pattern[str]) -> list[Path]:
    matches = []
    for path in folder.rglob("*"):
        if path.is_file() and pattern.search(path.name):
            matches.append(path)
    return sorted(matches)


def download_output(comp: Competition, kernel: str, out_dir: Path) -> list[Path]:
    target = out_dir / comp.slug / kernel.replace("/", "__")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "kaggle",
            "kernels",
            "output",
            kernel,
            "-p",
            str(target),
            "-o",
            "--file-pattern",
            comp.file_pattern,
            "--page-size",
            "200",
        ],
        capture=True,
    )
    return find_downloaded_files(target, re.compile(comp.file_pattern))


def wait_for_submission_result(api: Any, comp: Competition, submission_ref: int) -> None:
    timeout = max(comp.submission_wait_timeout_seconds, 1)
    poll = max(comp.submission_poll_seconds, 1)
    deadline = time.monotonic() + timeout
    last_update: tuple[str, str, str] | None = None
    last_lookup_error = ""

    while True:
        try:
            submissions = api.competition_submissions(
                comp.slug,
                page_size=100,
            ) or []
            submission = next(
                (
                    item
                    for item in submissions
                    if item is not None
                    and int(object_value(item, ("ref",)) or 0) == submission_ref
                ),
                None,
            )
            last_lookup_error = ""
        except Exception as exc:  # noqa: BLE001 - retry transient API failures.
            submission = None
            last_lookup_error = exception_details(exc)
            print(
                f"Could not query submission {submission_ref}; retrying: "
                f"{last_lookup_error}"
            )

        if submission is not None:
            status = normalize_status(object_value(submission, ("status",)))
            scoring_error = maybe_str(
                object_value(
                    submission,
                    ("error_description", "errorDescription"),
                )
            )
            public_score = maybe_str(
                object_value(submission, ("public_score", "publicScore"))
            )
            update = (status, scoring_error or "", public_score or "")
            if update != last_update:
                print(
                    f"submission {submission_ref}: status={status or 'unknown'} "
                    f"public_score={public_score or '-'}"
                )
                last_update = update

            if scoring_error:
                raise SubmissionScoringError(
                    submission_ref,
                    status or "scoring-error",
                    scoring_error,
                )
            if status == "error":
                raise SubmissionScoringError(
                    submission_ref,
                    status,
                    "Kaggle reported a submission error without details.",
                )
            if status == "complete" and (
                not comp.require_public_score or public_score is not None
            ):
                return

        if time.monotonic() >= deadline:
            detail = f"timed out after {timeout}s waiting for final scoring"
            if last_lookup_error:
                detail += f"; last API error: {last_lookup_error}"
            raise SubmissionScoringError(
                submission_ref,
                (last_update or ("unknown", "", ""))[0],
                detail,
            )
        time.sleep(poll)


def submit_file(
    comp: Competition,
    kernel: str,
    file_path: Path,
    message: str,
    *,
    kernel_version: str | None = None,
) -> int:
    try:
        from kaggle import api  # type: ignore[import-not-found]

        api.authenticate()
        if comp.code_competition:
            submit_kernel = comp.submit_kernel or kernel
            submit_kernel_version = comp.submit_kernel_version or kernel_version
            if not submit_kernel or not submit_kernel_version:
                raise ValueError(
                    f"{comp.slug} is a notebook-only competition. Submit needs "
                    "an owned Kaggle notebook ref and version."
                )
            result = api.competition_submit_code(
                Path(file_path).name,
                message,
                comp.slug,
                submit_kernel,
                int(submit_kernel_version),
                quiet=True,
            )
        else:
            result = api.competition_submit(
                str(file_path),
                message,
                comp.slug,
                quiet=True,
            )
    except Exception as exc:  # noqa: BLE001 - preserve Kaggle response details.
        details = exception_details(exc)
        if details:
            raise RuntimeError(details) from exc
        raise

    result_message = getattr(result, "message", None)
    if result_message:
        print(result_message)
    submission_ref = int(getattr(result, "ref", 0) or 0)
    if submission_ref <= 0:
        raise RuntimeError("Kaggle accepted the request but returned no submission ID.")
    wait_for_submission_result(api, comp, submission_ref)
    return submission_ref


def error_text(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return cmd_error(exc)
    return exception_details(exc)


def exception_details(exc: Exception) -> str:
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        reason = getattr(response, "reason", None)
        url = getattr(response, "url", None)
        if status or reason:
            parts.append(f"HTTP status: {status} {reason}".strip())
        if url:
            parts.append(f"URL: {url}")
        text = getattr(response, "text", "") or ""
        if text:
            parts.append(f"Response body: {text[:4000]}")
    return "\n".join(part for part in parts if part)


def is_rate_limited_error(text: str) -> bool:
    return "429" in text or "Too Many Requests" in text


def make_message(comp: Competition, kernel: str, file_name: str, sha256: str) -> str:
    message = comp.message_template.format(
        competition=comp.slug,
        kernel_ref=kernel,
        file_name=file_name,
        sha256=sha256,
        short_sha=sha256[:12],
    )
    return message[:200]


def remember_failed_submission(
    state: dict[str, Any],
    *,
    comp: Competition,
    kernel_ref: str,
    file_name: str,
    file_hash: str,
    message: str,
    error: SubmissionScoringError,
    kernel_version: str | None = None,
    source_kernel_ref: str | None = None,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "competition": comp.slug,
        "kernel_ref": kernel_ref,
        "file_name": file_name,
        "sha256": file_hash,
        "submitted_at_utc": utc_now().isoformat(),
        "message": message,
        "submission_ref": error.submission_ref,
        "scoring_status": error.status,
        "scoring_error": error.detail,
    }
    if kernel_version is not None:
        record["kernel_version"] = kernel_version
    if source_kernel_ref is not None:
        record["source_kernel_ref"] = source_kernel_ref
    if execution_mode is not None:
        record["execution_mode"] = execution_mode
    state.setdefault("failed_submissions", []).append(record)
    return record


def remember_rejected_source(
    state: dict[str, Any],
    *,
    comp: Competition,
    source_kernel_ref: str,
    reason: str,
    file_name: str | None = None,
    file_hash: str | None = None,
    execution_mode: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "competition": comp.slug,
        "source_kernel_ref": source_kernel_ref,
        "reason": reason,
        "rejected_at_utc": utc_now().isoformat(),
    }
    if file_name:
        record["file_name"] = file_name
    if file_hash:
        record["sha256"] = file_hash
    if execution_mode is not None:
        record["execution_mode"] = execution_mode
    state.setdefault("rejected_sources", []).append(record)
    return record


def slugify_text(text: str, *, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if len(slug) <= max_len:
        return slug or "auto-kernel"
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]
    prefix = slug[: max_len - len(digest) - 1].rstrip("-")
    return f"{prefix}-{digest}"


def kaggle_api() -> Any:
    from kaggle import api  # type: ignore[import-not-found]

    api.authenticate()
    return api


def kaggle_username(api: Any | None = None) -> str:
    for name in ("KAGGLE_USERNAME", "KAGGLE_USER"):
        value = maybe_str(os.environ.get(name))
        if value:
            return value

    token = maybe_str(os.environ.get("KAGGLE_API_TOKEN"))
    if token:
        try:
            parsed = json.loads(token)
            username = maybe_str(parsed.get("username"))
            if username:
                return username
        except json.JSONDecodeError:
            pass

    api = api or kaggle_api()
    username = maybe_str(getattr(api, "config_values", {}).get("username"))
    if username:
        return username
    raise ValueError(
        "Could not determine Kaggle username. Set KAGGLE_USERNAME or use a "
        "KAGGLE_API_TOKEN JSON value that contains username/key."
    )


def owned_kernel_ref(comp: Competition, public_ref: str, owner: str) -> str:
    source_owner, source_slug = public_ref.split("/", 1)
    base = f"{comp.copy_slug_prefix}-{comp.slug}-{source_owner}-{source_slug}"
    return f"{owner}/{slugify_text(base)}"


def rewrite_kernel_metadata(
    folder: Path,
    *,
    comp: Competition,
    public_ref: str,
    target_ref: str,
) -> None:
    metadata_path = folder / "kernel-metadata.json"
    with metadata_path.open("r", encoding="utf-8") as fh:
        metadata = json.load(fh)

    metadata["id"] = target_ref
    metadata.pop("id_no", None)
    metadata["title"] = target_ref.split("/", 1)[1]
    metadata["is_private"] = True

    competition_sources = metadata.get("competition_sources") or []
    if comp.slug not in competition_sources:
        competition_sources.append(comp.slug)
    metadata["competition_sources"] = competition_sources

    dataset_sources = metadata.get("dataset_sources") or []
    pinned_dataset_sources = []
    for source in dataset_sources:
        source_ref = str(source)
        pinned_ref = with_pinned_version(
            source_ref,
            comp.dataset_source_versions,
        )
        if pinned_ref != source_ref:
            print(f"Pinned dataset source: {source_ref} -> {pinned_ref}")
        pinned_dataset_sources.append(pinned_ref)
    metadata["dataset_sources"] = pinned_dataset_sources
    metadata.setdefault("kernel_sources", [])
    metadata.setdefault("model_sources", [])
    metadata["source_kernel"] = public_ref

    with metadata_path.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)
        fh.write("\n")


def invalid_push_sources(result: Any) -> list[str]:
    checks = (
        ("dataset", "invalidDatasetSources", "invalid_dataset_sources"),
        ("kernel", "invalidKernelSources", "invalid_kernel_sources"),
        ("model", "invalidModelSources", "invalid_model_sources"),
        (
            "competition",
            "invalidCompetitionSources",
            "invalid_competition_sources",
        ),
    )
    invalid = []
    for source_type, camel_name, snake_name in checks:
        values = getattr(result, camel_name, None)
        if values is None:
            values = getattr(result, snake_name, None)
        if values:
            invalid.extend(f"{source_type}:{value}" for value in values)
    return invalid


def nested_dict(value: Any, *keys: str) -> dict[str, Any]:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def resource_ref_from_path(value: Any, resource: str, parts: int) -> str | None:
    if isinstance(value, dict):
        for item in value.values():
            found = resource_ref_from_path(item, resource, parts)
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = resource_ref_from_path(item, resource, parts)
            if found:
                return found
        return None
    if not isinstance(value, str):
        return None
    pattern = rf"/{re.escape(resource)}/" + "/".join(
        r"([^/\s?#]+)" for _ in range(parts)
    )
    match = re.search(pattern, value)
    return "/".join(match.groups()) if match else None


def metadata_sources_from_session(
    data_sources: list[dict[str, Any]],
) -> dict[str, list[str]]:
    result = {
        "competition_sources": [],
        "dataset_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    for source in data_sources:
        source_type = str(source.get("sourceType") or "").upper()
        version_info = nested_dict(
            source,
            "databundleVersion",
            "databundleVersionInfo",
        )
        version_number = maybe_int(version_info.get("versionNumber"))
        pinned = bool(source.get("isPinned"))

        if "COMPETITION" in source_type:
            ref = resource_ref_from_path(source, "competitions", 1)
            if not ref:
                ref = maybe_str(source.get("slug"))
            target = "competition_sources"
        elif "DATASET" in source_type:
            ref = resource_ref_from_path(source, "datasets", 2)
            target = "dataset_sources"
        elif any(name in source_type for name in ("NOTEBOOK", "KERNEL", "SCRIPT")):
            ref = resource_ref_from_path(source, "code", 2)
            target = "kernel_sources"
        elif "MODEL" in source_type:
            ref = resource_ref_from_path(source, "models", 4)
            target = "model_sources"
        else:
            raise ExactSourceUnavailableError(
                f"Unsupported Kaggle input type {source_type or '<missing>'!r}."
            )

        if not ref:
            raise ExactSourceUnavailableError(
                f"Could not resolve exact {source_type} input {source.get('name')!r}."
            )
        if pinned and version_number is not None and target != "competition_sources":
            ref = f"{ref}/{version_number}"
        result[target].append(ref)
    return result


def machine_shape_for_accelerator(accelerator: str) -> str | None:
    shapes = {
        "NVIDIA_TESLA_K80": "NvidiaTeslaK80",
        "NVIDIA_TESLA_P100": "NvidiaTeslaP100",
        "NVIDIA_TESLA_T4": "NvidiaTeslaT4",
        "NVIDIA_TESLA_V100": "NvidiaTeslaV100",
        "NVIDIA_A100": "NvidiaA100",
        "NVIDIA_L4": "NvidiaL4",
        "NVIDIA_L4_X1": "NvidiaL4",
        "NVIDIA_H100": "NvidiaH100",
    }
    return shapes.get(accelerator.upper())


def download_exact_public_kernel_source(
    source_ref: str,
    source_session_id: int,
    destination: Path,
    *,
    web_client: KaggleWebClient | None = None,
) -> None:
    base_ref, requested_version = parse_versioned_kernel_ref(source_ref)
    client = web_client or KaggleWebClient()
    client.get_text(notebook_page_url(base_ref, requested_version))
    response = client.post_json(
        "GetKernelVersion",
        {"kernelSessionId": source_session_id, "getDataSources": True},
    )
    version_and_run = response.get("kernelVersionAndRun") or {}
    if not isinstance(version_and_run, dict):
        raise ExactSourceUnavailableError("Kaggle returned invalid version metadata.")
    version = version_and_run.get("version") or {}
    run = version_and_run.get("run") or {}
    blob = version_and_run.get("blob") or {}
    if int(run.get("id") or 0) != source_session_id:
        raise ExactSourceUnavailableError(
            f"Kaggle returned a different session for {source_ref}."
        )
    if int(version.get("versionNumber") or 0) != requested_version:
        raise ExactSourceUnavailableError(
            f"Kaggle returned a different version for {source_ref}."
        )

    settings = blob.get("settings") or {}
    if not isinstance(settings, dict):
        raise ExactSourceUnavailableError(
            f"Kaggle did not expose execution settings for {source_ref}."
        )
    source_type = str(settings.get("sourceType") or run.get("sourceType") or "")
    is_notebook = "NOTEBOOK" in source_type.upper()
    code_file = "source.ipynb" if is_notebook else "source.py"
    source = client.post_bytes(
        "GetKernelSessionSource",
        {"kernelSessionId": source_session_id},
    )
    if not source:
        raise ExactSourceUnavailableError(
            f"Kaggle returned empty source for {source_ref}."
        )
    if is_notebook:
        try:
            notebook = json.loads(source.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExactSourceUnavailableError(
                f"Kaggle returned invalid notebook JSON for {source_ref}."
            ) from exc
        if not isinstance(notebook, dict) or "cells" not in notebook:
            raise ExactSourceUnavailableError(
                f"Kaggle returned invalid notebook JSON for {source_ref}."
            )

    data_response = client.post_json(
        "GetKernelSessionDataSources",
        {"kernelSessionId": source_session_id},
    )
    raw_data_sources = data_response.get("dataSources") or []
    if not isinstance(raw_data_sources, list) or not all(
        isinstance(item, dict) for item in raw_data_sources
    ):
        raise ExactSourceUnavailableError(
            f"Kaggle returned invalid input metadata for {source_ref}."
        )
    sources = metadata_sources_from_session(raw_data_sources)

    accelerator = str(settings.get("accelerator") or "NONE").upper()
    run_info = run.get("runInfo") or {}
    metadata: dict[str, Any] = {
        "id": base_ref,
        "title": str(run.get("title") or base_ref.split("/", 1)[1]),
        "code_file": code_file,
        "language": "python",
        "kernel_type": "notebook" if is_notebook else "script",
        "is_private": False,
        "enable_gpu": "NVIDIA" in accelerator or accelerator == "GPU",
        "enable_tpu": "TPU" in accelerator,
        "enable_internet": bool(settings.get("isInternetEnabled", False)),
        **sources,
    }
    docker_digest = maybe_str(run_info.get("dockerImageDigest"))
    if docker_digest:
        metadata["docker_image"] = (
            "gcr.io/kaggle-private-byod/python@sha256:" + docker_digest
        )
    machine_shape = machine_shape_for_accelerator(accelerator)
    if machine_shape:
        metadata["machine_shape"] = machine_shape

    destination.mkdir(parents=True, exist_ok=True)
    (destination / code_file).write_bytes(source)
    with (destination / "kernel-metadata.json").open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, sort_keys=True)
        fh.write("\n")


def pull_public_kernel_source(
    api: Any,
    source_ref: str,
    destination: Path,
) -> str:
    base_ref, requested_version = parse_versioned_kernel_ref(source_ref)
    current_version: int | None = None
    try:
        current_version = current_kernel_version(
            fetch_kernel_metadata(base_ref, api=api)
        )
    except Exception as exc:  # noqa: BLE001 - exact pull remains a valid fallback.
        print(
            f"Could not verify the current source version for {base_ref}: "
            f"{error_text(exc)}"
        )

    pull_ref = base_ref if current_version == requested_version else source_ref
    print(f"+ kaggle kernels pull {pull_ref} -p {destination} -m")
    try:
        api.kernels_pull(
            pull_ref,
            path=str(destination),
            metadata=True,
            quiet=True,
        )
    except Exception as exc:
        if pull_ref == source_ref:
            current_detail = (
                f"; Kaggle currently exposes V{current_version}"
                if current_version is not None
                else ""
            )
            raise ExactSourceUnavailableError(
                f"Kaggle would not provide source for exact version "
                f"{source_ref}{current_detail}. Refusing to run a different "
                "version or relay an old output."
            ) from exc
        raise

    if pull_ref == base_ref:
        after_version = current_kernel_version(
            fetch_kernel_metadata(base_ref, api=api)
        )
        if after_version != requested_version:
            raise ExactSourceUnavailableError(
                f"{base_ref} changed from requested V{requested_version} to "
                f"V{after_version or 'unknown'} while its source was downloaded."
            )
    return pull_ref


def copy_and_run_public_kernel(
    comp: Competition,
    source_ref: str,
    *,
    work_dir: Path,
    source_session_id: int | None = None,
) -> tuple[str, str, str]:
    base_ref, _ = parse_versioned_kernel_ref(source_ref)
    api = kaggle_api()
    owner = kaggle_username(api)
    target_ref = owned_kernel_ref(comp, base_ref, owner)
    local_dir = work_dir / comp.slug / target_ref.replace("/", "__")
    if local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    if source_session_id is None:
        pull_public_kernel_source(api, source_ref, local_dir)
    else:
        print(
            "+ Kaggle Copy & Edit exact score session "
            f"{source_ref} (scriptVersionId {source_session_id})"
        )
        download_exact_public_kernel_source(
            source_ref,
            source_session_id,
            local_dir,
        )
    rewrite_kernel_metadata(
        local_dir,
        comp=comp,
        public_ref=source_ref,
        target_ref=target_ref,
    )

    timeout = comp.kernel_session_timeout_seconds
    print(f"+ kaggle kernels push -p {local_dir} # full source from {source_ref}")
    result = api.kernels_push(str(local_dir), timeout=timeout)
    error = maybe_str(getattr(result, "error", None))
    if error:
        raise RuntimeError(f"Kernel push error: {error}")
    invalid_sources = invalid_push_sources(result)
    if invalid_sources:
        raise RuntimeError(
            "Kaggle rejected pinned kernel inputs: " + ", ".join(invalid_sources)
        )

    version = getattr(result, "versionNumber", None)
    if version is None:
        version = getattr(result, "version_number", None)
    if version is None:
        raise RuntimeError("Kaggle did not return a kernel version number.")

    url = maybe_str(getattr(result, "url", None))
    if url:
        print(f"Kernel version {version} successfully pushed. Progress: {url}")
    else:
        print(f"Kernel version {version} successfully pushed.")
    return target_ref, str(version), source_ref


def normalize_status(status: Any) -> str:
    text = str(status or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    text = text.replace("_", "-")
    for prefix in ("kernel-session-status-", "session-status-"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def kernel_log_tail(api: Any, kernel_ref: str, max_chars: int = 12000) -> str:
    try:
        log = str(api.kernels_logs(kernel_ref) or "").strip()
    except Exception as exc:  # noqa: BLE001 - preserve the original run failure.
        return f"Could not retrieve Kaggle kernel log: {exception_details(exc)}"

    if not log:
        return "Kaggle returned an empty kernel log."
    if len(log) > max_chars:
        return "... (log truncated) ...\n" + log[-max_chars:]
    return log


def wait_for_kernel_complete(
    kernel_ref: str,
    *,
    timeout_seconds: int,
    poll_seconds: int,
) -> str:
    api = kaggle_api()
    deadline = time.monotonic() + timeout_seconds
    done = {"complete", "completed", "success", "succeeded"}
    failed = {"cancelled", "canceled", "error", "failed", "failure"}

    while True:
        response = api.kernels_status(kernel_ref)
        status = normalize_status(getattr(response, "status", ""))
        failure = maybe_str(getattr(response, "failure_message", None))
        print(f"{kernel_ref} status: {status}")

        if status in done or any(status.endswith(f"-{item}") for item in done):
            return status
        if failure or status in failed or any(
            status.endswith(f"-{item}") for item in failed
        ):
            detail = f": {failure}" if failure else ""
            log_tail = kernel_log_tail(api, kernel_ref)
            print("--- Kaggle kernel log tail ---")
            print(log_tail)
            print("--- End Kaggle kernel log tail ---")
            raise RuntimeError(
                f"Kernel run failed with status {status}{detail}\n"
                f"Kaggle kernel log tail:\n{log_tail}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for {kernel_ref} after {timeout_seconds} seconds."
            )
        time.sleep(max(poll_seconds, 5))


def append_summary(summary_path: str | None, lines: list[str]) -> None:
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def write_run_log(log_dir: Path, report: dict[str, Any]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    path = log_dir / f"run-{stamp}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")


def versioned_kernel_ref(kernel: str, version: str | None) -> str:
    if not version:
        return kernel
    return f"{kernel}/{version}"


def process_configured_code_submission(
    comp: Competition,
    *,
    state: dict[str, Any],
    output_dir: Path,
    submit: bool,
    now: datetime,
    report: dict[str, Any],
) -> dict[str, Any]:
    if not comp.submit_kernel:
        raise ValueError("process_configured_code_submission requires submit_kernel")

    if submit and not comp.submit_kernel_version:
        report["errors"].append(
            {
                "kernel": comp.submit_kernel,
                "error": (
                    "code_competition=true requires submit_kernel_version. "
                    "Run/copy the notebook on Kaggle, then set the version number."
                ),
            }
        )
        return report

    pattern = re.compile(comp.file_pattern)
    output_ref = versioned_kernel_ref(comp.submit_kernel, comp.submit_kernel_version)
    report["kernels_seen"] = 1

    already_today = submissions_today(state, comp.slug, now)
    remaining_today = max(comp.max_submissions_per_day - already_today, 0)
    remaining_run = min(comp.max_submissions_per_run, remaining_today)
    remaining_attempts = max(comp.max_submit_attempts_per_run, 0)

    if submit and remaining_run <= 0:
        report["skipped"].append("submission limit reached")
        return report
    if submit and remaining_attempts <= 0:
        report["skipped"].append("submit attempt limit reached")
        return report

    try:
        files = list_kernel_files(output_ref)
    except subprocess.CalledProcessError as exc:
        report["errors"].append({"kernel": output_ref, "error": cmd_error(exc)})
        return report

    names = [name for name in (output_file_name(item) for item in files) if name]
    matches = [name for name in names if pattern.search(Path(name).name)]
    report["candidates"].append({"kernel": output_ref, "matching_files": matches})

    if not matches:
        report["skipped"].append(
            f"configured submit_kernel has no file matching {comp.file_pattern}"
        )
        return report

    for file_name in matches:
        if already_submitted(
            state,
            comp.slug,
            comp.submit_kernel,
            file_name,
            comp.submit_kernel_version,
        ):
            report["skipped"].append(f"already submitted {output_ref}:{file_name}")
            continue

        if not submit:
            report["skipped"].append(
                f"dry-run would submit owned notebook {output_ref}:{file_name}"
            )
            continue

        file_hash = ""
        message = ""
        try:
            downloaded = download_output(comp, output_ref, output_dir)
            selected = select_downloaded_file(downloaded, file_name)
            if selected is None:
                raise FileNotFoundError(f"Downloaded output did not contain {file_name}")
            validation_errors = validate_submission_file(
                selected,
                comp.required_columns,
                comp.dataset_shapes,
            )
            if validation_errors:
                report["skipped"].append(
                    "invalid submission format "
                    f"{output_ref}:{file_name}: {'; '.join(validation_errors)}"
                )
                continue
            file_hash = sha256_file(selected)
            message = make_message(comp, comp.submit_kernel, Path(file_name).name, file_hash)
            remaining_attempts -= 1
            submission_ref = submit_file(
                comp,
                comp.submit_kernel,
                selected,
                message,
            )
            record = {
                "competition": comp.slug,
                "kernel_ref": comp.submit_kernel,
                "kernel_version": comp.submit_kernel_version,
                "file_name": file_name,
                "sha256": file_hash,
                "submitted_at_utc": utc_now().isoformat(),
                "message": message,
                "submission_ref": submission_ref,
                "scoring_status": "complete",
            }
            state["submissions"].append(record)
            report["submitted"].append(record)
            remaining_run -= 1
        except Exception as exc:  # noqa: BLE001 - keep the run moving.
            err = error_text(exc)
            report["errors"].append({"kernel": output_ref, "file": file_name, "error": err})
            if isinstance(exc, SubmissionScoringError):
                failed_record = remember_failed_submission(
                    state,
                    comp=comp,
                    kernel_ref=comp.submit_kernel,
                    kernel_version=comp.submit_kernel_version,
                    file_name=file_name,
                    file_hash=file_hash,
                    message=message,
                    error=exc,
                )
                report.setdefault("failed_submissions", []).append(failed_record)
                remaining_run -= 1

        if remaining_run <= 0 or remaining_attempts <= 0:
            break

    return report


def process_auto_copy_code_submission(
    comp: Competition,
    *,
    state: dict[str, Any],
    output_dir: Path,
    work_dir: Path,
    submit: bool,
    now: datetime,
    report: dict[str, Any],
) -> dict[str, Any]:
    pattern = re.compile(comp.file_pattern)
    already_today = submissions_today(state, comp.slug, now)
    remaining_today = max(comp.max_submissions_per_day - already_today, 0)
    remaining_run = min(comp.max_submissions_per_run, remaining_today)
    remaining_attempts = max(comp.max_submit_attempts_per_run, 0)

    if submit and remaining_run <= 0:
        report["skipped"].append("submission limit reached")
        return report
    if submit and remaining_attempts <= 0:
        report["skipped"].append("submit attempt limit reached")
        return report

    kernels = list_kernels(comp)
    report["kernels_seen"] = len(kernels)

    # Match the Competition Code page: pinned notebooks are separate, and only
    # the first unpinned result in Public Score order is eligible for this run.
    for row in kernels[:1]:
        if submit and (remaining_run <= 0 or remaining_attempts <= 0):
            break

        ref = kernel_ref(row)
        if not ref:
            report["errors"].append({"row": row, "error": "could not find kernel ref"})
            continue

        try:
            source_ref, version_resolution, source_session_id = (
                resolved_source_kernel(comp, row)
            )
        except Exception as exc:  # noqa: BLE001 - report an unverifiable top result.
            report["errors"].append(
                {
                    "kernel": ref,
                    "stage": "resolve best-score version",
                    "error": error_text(exc),
                }
            )
            continue

        execution_mode = (
            EXACT_SCORE_COPY_MODE
            if version_resolution == "competition-code-score"
            else "full-source-clone"
        )
        candidate_report = {
            "kernel": ref,
            "source_kernel_ref": source_ref,
            "version_resolution": version_resolution,
            "source_session_id": source_session_id,
            "execution_mode": execution_mode,
            "file_pattern": comp.file_pattern,
            "matching_files": [],
        }
        report["candidates"].append(candidate_report)

        if source_kernel_already_attempted(
            state,
            comp.slug,
            source_ref,
            execution_mode=execution_mode,
        ):
            report["skipped"].append(f"already attempted source {source_ref}")
            continue

        if not submit:
            report["skipped"].append(
                f"dry-run would clone and run {source_ref}, then submit files matching "
                f"{comp.file_pattern}"
            )
            continue

        remaining_attempts -= 1
        try:
            copy_kwargs: dict[str, Any] = {"work_dir": work_dir}
            if version_resolution == "competition-code-score":
                copy_kwargs["source_session_id"] = source_session_id
            owned_ref, owned_version, launched_source_ref = (
                copy_and_run_public_kernel(comp, source_ref, **copy_kwargs)
            )
            wait_for_kernel_complete(
                owned_ref,
                timeout_seconds=comp.notebook_wait_timeout_seconds,
                poll_seconds=comp.notebook_poll_seconds,
            )
            downloaded = download_output(comp, owned_ref, output_dir)
        except ExactSourceUnavailableError as exc:
            reason = error_text(exc)
            rejected = remember_rejected_source(
                state,
                comp=comp,
                source_kernel_ref=source_ref,
                reason=reason,
                execution_mode=execution_mode,
            )
            report.setdefault("rejected_sources", []).append(rejected)
            report["skipped"].append(
                f"rejected source {source_ref}: {reason}"
            )
            continue
        except Exception as exc:  # noqa: BLE001 - report the top notebook failure.
            report["errors"].append(
                {
                    "kernel": source_ref,
                    "stage": "clone and run notebook",
                    "error": error_text(exc),
                }
            )
            continue

        notebook_outputs = sorted(
            (path for path in downloaded if pattern.search(path.name)),
            key=lambda path: (
                path.name.lower() != "submission.csv",
                path.name.lower(),
            ),
        )
        if not notebook_outputs:
            reason = "cloned notebook produced no matching submission file"
            rejected = remember_rejected_source(
                state,
                comp=comp,
                source_kernel_ref=launched_source_ref,
                reason=reason,
                execution_mode=execution_mode,
            )
            report.setdefault("rejected_sources", []).append(rejected)
            report["skipped"].append(
                f"rejected source {launched_source_ref}: {reason}"
            )
            continue
        candidate_report["matching_files"] = [
            path.name for path in notebook_outputs
        ]

        if comp.submission_file_name:
            native_outputs = [
                path
                for path in notebook_outputs
                if path.name == comp.submission_file_name
            ]
            if not native_outputs:
                reason = (
                    f"required native output {comp.submission_file_name!r} missing; "
                    "refusing to rename an alternate artifact"
                )
                rejected = remember_rejected_source(
                    state,
                    comp=comp,
                    source_kernel_ref=launched_source_ref,
                    reason=reason,
                    execution_mode=execution_mode,
                )
                report.setdefault("rejected_sources", []).append(rejected)
                report["skipped"].append(
                    f"rejected source {launched_source_ref}: {reason}"
                )
                continue
            notebook_outputs = native_outputs[:1]

        for selected in notebook_outputs:
            source_file_name = selected.name
            submission_path = selected
            submission_kernel_ref = owned_ref
            submission_kernel_version = owned_version
            file_hash = ""
            message = ""
            try:
                file_hash = sha256_file(submission_path)
                validation_errors = validate_submission_file(
                    submission_path,
                    comp.required_columns,
                    comp.dataset_shapes,
                )
                if validation_errors:
                    report["skipped"].append(
                        "invalid submission format "
                        f"{owned_ref}/{owned_version}:{source_file_name}: "
                        f"{'; '.join(validation_errors)}"
                    )
                    if comp.submission_file_name:
                        rejected = remember_rejected_source(
                            state,
                            comp=comp,
                            source_kernel_ref=launched_source_ref,
                            file_name=source_file_name,
                            file_hash=file_hash,
                            reason="; ".join(validation_errors),
                            execution_mode=execution_mode,
                        )
                        report.setdefault("rejected_sources", []).append(rejected)
                        break
                    continue

                if submission_hash_already_attempted(
                    state,
                    comp.slug,
                    file_hash,
                    execution_mode=execution_mode,
                ):
                    reason = f"submission hash already attempted: {file_hash}"
                    report["skipped"].append(
                        f"rejected source {launched_source_ref}: {reason}"
                    )
                    if comp.submission_file_name:
                        rejected = remember_rejected_source(
                            state,
                            comp=comp,
                            source_kernel_ref=launched_source_ref,
                            file_name=source_file_name,
                            file_hash=file_hash,
                            reason=reason,
                            execution_mode=execution_mode,
                        )
                        report.setdefault("rejected_sources", []).append(rejected)
                        break
                    continue

                message = make_message(
                    comp,
                    launched_source_ref,
                    source_file_name,
                    file_hash,
                )
                submission_ref = submit_file(
                    comp,
                    submission_kernel_ref,
                    submission_path,
                    message,
                    kernel_version=submission_kernel_version,
                )
                record = {
                    "competition": comp.slug,
                    "kernel_ref": submission_kernel_ref,
                    "kernel_version": submission_kernel_version,
                    "source_kernel_ref": launched_source_ref,
                    "source_file_name": source_file_name,
                    "file_name": submission_path.name,
                    "sha256": file_hash,
                    "submitted_at_utc": utc_now().isoformat(),
                    "message": message,
                    "submission_ref": submission_ref,
                    "scoring_status": "complete",
                    "execution_mode": execution_mode,
                }
                state["submissions"].append(record)
                report["submitted"].append(record)
                remaining_run -= 1
            except Exception as exc:  # noqa: BLE001 - keep the run moving.
                err = error_text(exc)
                report["errors"].append(
                    {
                        "kernel": launched_source_ref,
                        "file": source_file_name,
                        "stage": "submit cloned notebook output",
                        "error": err,
                    }
                )
                if isinstance(exc, SubmissionScoringError):
                    failed_record = remember_failed_submission(
                        state,
                        comp=comp,
                        kernel_ref=submission_kernel_ref,
                        kernel_version=submission_kernel_version,
                        source_kernel_ref=launched_source_ref,
                        file_name=submission_path.name,
                        file_hash=file_hash,
                        message=message,
                        error=exc,
                        execution_mode=execution_mode,
                    )
                    report.setdefault("failed_submissions", []).append(
                        failed_record
                    )
                    remaining_run -= 1

            if remaining_run <= 0:
                break

        if remaining_run <= 0 or remaining_attempts <= 0:
            break

    return report


def process_competition(
    comp: Competition,
    *,
    state: dict[str, Any],
    output_dir: Path,
    work_dir: Path,
    submit: bool,
    now: datetime,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "competition": comp.slug,
        "enabled": comp.enabled,
        "auto_submit": comp.auto_submit,
        "sort_by": comp.sort_by,
        "dry_run": not submit,
        "kernels_seen": 0,
        "candidates": [],
        "submitted": [],
        "failed_submissions": [],
        "rejected_sources": [],
        "skipped": [],
        "errors": [],
    }
    if not comp.enabled:
        report["skipped"].append("competition disabled")
        return report

    if submit and not comp.auto_submit:
        report["skipped"].append("submit requested but auto_submit=false")
        return report

    if comp.code_competition and comp.auto_copy_notebook:
        return process_auto_copy_code_submission(
            comp,
            state=state,
            output_dir=output_dir,
            work_dir=work_dir,
            submit=submit,
            now=now,
            report=report,
        )

    if comp.code_competition and comp.submit_kernel:
        return process_configured_code_submission(
            comp,
            state=state,
            output_dir=output_dir,
            submit=submit,
            now=now,
            report=report,
        )

    if submit and comp.code_competition and not comp.submit_kernel:
        report["errors"].append(
            {
                "error": (
                    "This competition only accepts notebook submissions, and Kaggle "
                    "does not allow submitting someone else's notebook session. "
                    "Copy/fork the selected public notebook into your Kaggle account, "
                    "then set submit_kernel and submit_kernel_version in config.toml."
                )
            }
        )
        return report

    pattern = re.compile(comp.file_pattern)
    already_today = submissions_today(state, comp.slug, now)
    remaining_today = max(comp.max_submissions_per_day - already_today, 0)
    remaining_run = min(comp.max_submissions_per_run, remaining_today)
    remaining_attempts = max(comp.max_submit_attempts_per_run, 0)
    rate_limited = False

    kernels = list_kernels(comp)
    report["kernels_seen"] = len(kernels)

    for row in kernels:
        if remaining_run <= 0 and submit:
            report["skipped"].append("submission limit reached")
            break
        if remaining_attempts <= 0 and submit:
            report["skipped"].append("submit attempt limit reached")
            break

        ref = kernel_ref(row)
        if not ref:
            report["errors"].append({"row": row, "error": "could not find kernel ref"})
            continue

        try:
            files = list_kernel_files(ref)
        except subprocess.CalledProcessError as exc:
            err = cmd_error(exc)
            report["errors"].append({"kernel": ref, "error": err})
            if is_rate_limited_error(err):
                rate_limited = True
                report["skipped"].append("Kaggle API rate limit reached")
                break
            continue

        names = [name for name in (output_file_name(item) for item in files) if name]
        matches = [name for name in names if pattern.search(Path(name).name)]
        report["candidates"].append({"kernel": ref, "matching_files": matches})

        for file_name in matches:
            if already_submitted(state, comp.slug, ref, file_name):
                report["skipped"].append(f"already submitted {ref}:{file_name}")
                continue

            if not submit:
                if comp.code_competition:
                    report["skipped"].append(
                        f"dry-run found public notebook candidate to fork {ref}:{file_name}"
                    )
                else:
                    report["skipped"].append(f"dry-run would submit {ref}:{file_name}")
                continue

            file_hash = ""
            message = ""
            try:
                downloaded = download_output(comp, ref, output_dir)
                selected = select_downloaded_file(downloaded, file_name)
                if selected is None:
                    raise FileNotFoundError(
                        f"Downloaded output did not contain {file_name}"
                    )
                validation_errors = validate_submission_file(
                    selected,
                    comp.required_columns,
                    comp.dataset_shapes,
                )
                if validation_errors:
                    report["skipped"].append(
                        "invalid submission format "
                        f"{ref}:{file_name}: {'; '.join(validation_errors)}"
                    )
                    continue
                file_hash = sha256_file(selected)
                message = make_message(comp, ref, Path(file_name).name, file_hash)
                remaining_attempts -= 1
                submission_ref = submit_file(comp, ref, selected, message)
                record = {
                    "competition": comp.slug,
                    "kernel_ref": ref,
                    "file_name": file_name,
                    "sha256": file_hash,
                    "submitted_at_utc": utc_now().isoformat(),
                    "message": message,
                    "submission_ref": submission_ref,
                    "scoring_status": "complete",
                }
                state["submissions"].append(record)
                report["submitted"].append(record)
                remaining_run -= 1
            except Exception as exc:  # noqa: BLE001 - keep the run moving.
                err = error_text(exc)
                report["errors"].append({"kernel": ref, "file": file_name, "error": err})
                if isinstance(exc, SubmissionScoringError):
                    failed_record = remember_failed_submission(
                        state,
                        comp=comp,
                        kernel_ref=ref,
                        file_name=file_name,
                        file_hash=file_hash,
                        message=message,
                        error=exc,
                    )
                    report.setdefault("failed_submissions", []).append(
                        failed_record
                    )
                    remaining_run -= 1
                if is_rate_limited_error(err):
                    rate_limited = True

            if remaining_run <= 0 or remaining_attempts <= 0 or rate_limited:
                break
        if rate_limited:
            break

    return report


def select_downloaded_file(files: list[Path], expected_name: str) -> Path | None:
    expected_base = Path(expected_name).name
    for path in files:
        if path.name == expected_base:
            return path
    return files[0] if files else None


def cmd_error(exc: subprocess.CalledProcessError) -> str:
    parts = []
    if exc.stdout:
        parts.append(exc.stdout.strip())
    if exc.stderr:
        parts.append(exc.stderr.strip())
    return "\n".join(parts) or str(exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("--state", default=Path("state/harvester-state.json"), type=Path)
    parser.add_argument("--output-dir", default=Path("submissions"), type=Path)
    parser.add_argument("--work-dir", default=Path("work/kernels"), type=Path)
    parser.add_argument("--log-dir", default=Path("logs"), type=Path)
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit. Also requires auto_submit=true for each competition.",
    )
    parser.add_argument(
        "--ci-summary",
        action="store_true",
        help="Write a short Markdown summary to GITHUB_STEP_SUMMARY.",
    )
    return parser.parse_args()


def failed_submission_competitions(
    reports: list[dict[str, Any]],
    *,
    submit: bool,
) -> list[str]:
    return [
        str(report.get("competition", ""))
        for report in reports
        if submit
        and report.get("enabled", True)
        and report.get("auto_submit", True)
        and not report.get("submitted")
        and "submission limit reached" not in report.get("skipped", [])
    ]


def main() -> int:
    args = parse_args()
    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    competitions = load_config(args.config)
    state = load_state(args.state)
    now = utc_now()
    reports = []

    for comp in competitions:
        print(f"\n== {comp.slug} ==")
        try:
            report = process_competition(
                comp,
                state=state,
                output_dir=args.output_dir,
                work_dir=args.work_dir,
                submit=args.submit,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - report all configured competitions.
            report = {
                "competition": comp.slug,
                "enabled": comp.enabled,
                "dry_run": not args.submit,
                "errors": [str(exc)],
            }
        reports.append(report)
        print(json.dumps(report, indent=2, sort_keys=True))

    if args.submit:
        save_state(args.state, state)

    run_report = {
        "started_at_utc": now.isoformat(),
        "finished_at_utc": utc_now().isoformat(),
        "submit": args.submit,
        "reports": reports,
    }
    write_run_log(args.log_dir, run_report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY") if args.ci_summary else None
    append_summary(summary_path, markdown_summary(run_report))
    failed = failed_submission_competitions(reports, submit=args.submit)
    if failed:
        print(
            "No submission was created for: " + ", ".join(failed),
            file=sys.stderr,
        )
        return 1
    return 0


def markdown_summary(run_report: dict[str, Any]) -> list[str]:
    lines = [
        "## Kaggle harvest summary",
        "",
        f"- Mode: {'submit' if run_report['submit'] else 'dry-run'}",
        f"- Started: {run_report['started_at_utc']}",
        f"- Finished: {run_report['finished_at_utc']}",
        "",
        "| Competition | Kernels | Candidates | Submitted | Errors |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for report in run_report["reports"]:
        candidates = sum(
            len(item.get("matching_files", [])) for item in report.get("candidates", [])
        )
        lines.append(
            "| {competition} | {kernels} | {candidates} | {submitted} | {errors} |".format(
                competition=report.get("competition", ""),
                kernels=report.get("kernels_seen", 0),
                candidates=candidates,
                submitted=len(report.get("submitted", [])),
                errors=len(report.get("errors", [])),
            )
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
