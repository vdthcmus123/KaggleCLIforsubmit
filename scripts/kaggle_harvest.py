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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STATE_VERSION = 1


@dataclass(frozen=True)
class Defaults:
    top_n: int = 100
    sort_by: str = "scoreDescending"
    language: str = "python"
    kernel_type: str = "all"
    output_type: str = "data"
    file_pattern: str = r"(?i).*submission.*\.csv$"
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
    kernel_session_timeout_seconds: int | None = None
    source_kernel_versions: dict[str, int] = field(default_factory=dict)
    dataset_source_versions: dict[str, int] = field(default_factory=dict)


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
    kernel_session_timeout_seconds: int | None = None
    source_kernel_versions: dict[str, int] = field(default_factory=dict)
    dataset_source_versions: dict[str, int] = field(default_factory=dict)
    submit_kernel: str | None = None
    submit_kernel_version: str | None = None


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


def with_pinned_version(ref: str, versions: dict[str, int]) -> str:
    base_ref = unversioned_ref(ref)
    version = versions.get(base_ref)
    return f"{base_ref}/{version}" if version is not None else ref


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": STATE_VERSION, "submissions": []}
    with path.open("r", encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("schema_version") != STATE_VERSION:
        raise ValueError(f"Unsupported state schema in {path}")
    state.setdefault("submissions", [])
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


def list_kernels(comp: Competition) -> list[dict[str, Any]]:
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

    data = run_json(args)
    if isinstance(data, dict):
        data = data.get("rows", data.get("items", []))
    if not isinstance(data, list):
        raise ValueError("Unexpected kaggle kernels list output.")
    return [row for row in data if isinstance(row, dict)]


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


def submissions_today(state: dict[str, Any], competition: str, now: datetime) -> int:
    today = now.date().isoformat()
    count = 0
    for sub in state.get("submissions", []):
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


def validate_submission_file(
    path: Path, required_columns: tuple[str, ...] | None
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
    return []


def find_downloaded_files(folder: Path, pattern: re.Pattern[str]) -> list[Path]:
    matches = []
    for path in folder.rglob("*"):
        if path.is_file() and pattern.search(path.name):
            matches.append(path)
    return sorted(matches)


def download_output(comp: Competition, kernel: str, out_dir: Path) -> list[Path]:
    target = out_dir / comp.slug / kernel.replace("/", "__")
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


def submit_file(
    comp: Competition,
    kernel: str,
    file_path: Path,
    message: str,
    *,
    kernel_version: str | None = None,
) -> None:
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


def error_text(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return cmd_error(exc)
    return str(exc)


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


def copy_and_run_public_kernel(
    comp: Competition,
    public_ref: str,
    *,
    work_dir: Path,
) -> tuple[str, str, str]:
    api = kaggle_api()
    owner = kaggle_username(api)
    target_ref = owned_kernel_ref(comp, public_ref, owner)
    source_ref = with_pinned_version(public_ref, comp.source_kernel_versions)
    if source_ref != public_ref:
        print(f"Pinned source notebook: {public_ref} -> {source_ref}")
    local_dir = work_dir / comp.slug / target_ref.replace("/", "__")
    if local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)

    print(f"+ kaggle kernels pull {source_ref} -p {local_dir} -m")
    api.kernels_pull(source_ref, path=str(local_dir), metadata=True, quiet=True)
    rewrite_kernel_metadata(
        local_dir,
        comp=comp,
        public_ref=source_ref,
        target_ref=target_ref,
    )

    timeout = comp.kernel_session_timeout_seconds
    print(f"+ kaggle kernels push -p {local_dir}")
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

        try:
            downloaded = download_output(comp, output_ref, output_dir)
            selected = select_downloaded_file(downloaded, file_name)
            if selected is None:
                raise FileNotFoundError(f"Downloaded output did not contain {file_name}")
            validation_errors = validate_submission_file(selected, comp.required_columns)
            if validation_errors:
                report["skipped"].append(
                    "invalid submission format "
                    f"{output_ref}:{file_name}: {'; '.join(validation_errors)}"
                )
                continue
            file_hash = sha256_file(selected)
            message = make_message(comp, comp.submit_kernel, Path(file_name).name, file_hash)
            remaining_attempts -= 1
            submit_file(comp, comp.submit_kernel, selected, message)
            record = {
                "competition": comp.slug,
                "kernel_ref": comp.submit_kernel,
                "kernel_version": comp.submit_kernel_version,
                "file_name": file_name,
                "sha256": file_hash,
                "submitted_at_utc": utc_now().isoformat(),
                "message": message,
            }
            state["submissions"].append(record)
            report["submitted"].append(record)
            remaining_run -= 1
        except Exception as exc:  # noqa: BLE001 - keep the run moving.
            err = error_text(exc)
            report["errors"].append({"kernel": output_ref, "file": file_name, "error": err})

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

    for row in kernels:
        if submit and (remaining_run <= 0 or remaining_attempts <= 0):
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
                report["skipped"].append("Kaggle API rate limit reached")
                break
            continue

        names = [name for name in (output_file_name(item) for item in files) if name]
        matches = [name for name in names if pattern.search(Path(name).name)]
        report["candidates"].append({"kernel": ref, "matching_files": matches})
        if not matches:
            continue

        for file_name in matches:
            if not submit:
                report["skipped"].append(
                    f"dry-run would copy, run, and submit {ref}:{file_name}"
                )
                continue

            remaining_attempts -= 1
            try:
                owned_ref, owned_version, source_ref = copy_and_run_public_kernel(
                    comp,
                    ref,
                    work_dir=work_dir,
                )
                wait_for_kernel_complete(
                    owned_ref,
                    timeout_seconds=comp.notebook_wait_timeout_seconds,
                    poll_seconds=comp.notebook_poll_seconds,
                )
                downloaded = download_output(comp, owned_ref, output_dir)
                selected = select_downloaded_file(downloaded, file_name)
                if selected is None:
                    raise FileNotFoundError(
                        f"Downloaded output did not contain {file_name}"
                    )
                validation_errors = validate_submission_file(
                    selected, comp.required_columns
                )
                if validation_errors:
                    report["skipped"].append(
                        "invalid submission format "
                        f"{owned_ref}/{owned_version}:{file_name}: "
                        f"{'; '.join(validation_errors)}"
                    )
                    continue
                file_hash = sha256_file(selected)
                message = make_message(
                    comp,
                    source_ref,
                    Path(file_name).name,
                    file_hash,
                )
                submit_file(
                    comp,
                    owned_ref,
                    selected,
                    message,
                    kernel_version=owned_version,
                )
                record = {
                    "competition": comp.slug,
                    "kernel_ref": owned_ref,
                    "kernel_version": owned_version,
                    "source_kernel_ref": source_ref,
                    "file_name": file_name,
                    "sha256": file_hash,
                    "submitted_at_utc": utc_now().isoformat(),
                    "message": message,
                }
                state["submissions"].append(record)
                report["submitted"].append(record)
                remaining_run -= 1
            except Exception as exc:  # noqa: BLE001 - keep the run moving.
                err = error_text(exc)
                report["errors"].append({"kernel": ref, "file": file_name, "error": err})

            if remaining_run <= 0 or remaining_attempts <= 0:
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

            try:
                downloaded = download_output(comp, ref, output_dir)
                selected = select_downloaded_file(downloaded, file_name)
                if selected is None:
                    raise FileNotFoundError(
                        f"Downloaded output did not contain {file_name}"
                    )
                validation_errors = validate_submission_file(
                    selected, comp.required_columns
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
                submit_file(comp, ref, selected, message)
                record = {
                    "competition": comp.slug,
                    "kernel_ref": ref,
                    "file_name": file_name,
                    "sha256": file_hash,
                    "submitted_at_utc": utc_now().isoformat(),
                    "message": message,
                }
                state["submissions"].append(record)
                report["submitted"].append(record)
                remaining_run -= 1
            except Exception as exc:  # noqa: BLE001 - keep the run moving.
                err = error_text(exc)
                report["errors"].append({"kernel": ref, "file": file_name, "error": err})
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
