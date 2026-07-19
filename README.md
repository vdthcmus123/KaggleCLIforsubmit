# Kaggle Public Kernel Submitter

A small GitHub Actions-friendly tool that scans public Kaggle kernels for one
or more competitions, finds matching submission files, and optionally submits
them with the Kaggle CLI/API. For notebook-only competitions, it can copy the
selected public notebook source into a private notebook in your Kaggle account,
run the full notebook, validate its new output, and submit that owned version.

The default mode is dry-run. Real submissions require both:

1. `auto_submit = true` for the competition in `config.toml`
2. `--submit` on the command line, or the matching GitHub Actions control

This double lock is intentional. Kaggle competitions can have different rules,
license expectations, and daily submission limits.

## Files

- `scripts/kaggle_harvest.py` - the harvester
- `config.example.toml` - example multi-competition config
- `.github/workflows/kaggle-harvest.yml` - scheduled/manual GitHub Actions workflow
- `state/harvester-state.json` - tracked state, created by the tool after real submissions
- `submissions/` - downloaded kernel outputs, ignored by git
- `work/` - temporary cloned notebook sources, ignored by git
- `logs/` - local run logs, ignored by git and uploaded as workflow artifacts

## Local setup

Install Python 3.11+ and dependencies:

```bash
python -m pip install -r requirements.txt
```

Authenticate with Kaggle:

```bash
kaggle auth login
```

Or set `KAGGLE_API_TOKEN` from your Kaggle API settings.

Copy the example config:

```bash
cp config.example.toml config.toml
```

Edit `config.toml`:

```toml
[[competitions]]
slug = "your-competition-slug"
enabled = true
auto_submit = false
top_n = 100
sort_by = "scoreDescending"
file_pattern = "(?i).*submission.*\\.csv$"
code_competition = false
max_submissions_per_run = 1
max_submit_attempts_per_run = 1
max_submissions_per_day = 2
```

Dry-run:

```bash
python scripts/kaggle_harvest.py --config config.toml
```

Real submit:

```bash
python scripts/kaggle_harvest.py --config config.toml --submit
```

Remember: real submit still requires `auto_submit = true` for that competition.

## GitHub Actions setup

1. Create a new private GitHub repository.
2. Push these files to it.
3. In Kaggle, go to your account API settings and generate a new token.
4. In GitHub, open the repo settings:
   `Settings -> Secrets and variables -> Actions -> New repository secret`
5. Add a secret named `KAGGLE_API_TOKEN` with the token value from Kaggle.
6. Rename `config.example.toml` to `config.toml`, then edit the competition list.
7. Commit and push.

The workflow is configured with:

- manual runs via `workflow_dispatch`
- scheduled real submits at 08:00 and 20:00 Vietnam time
- at most 1 submission per run and a local guardrail of 5 submissions per day

GitHub Actions cron uses UTC, so the workflow contains:

```yaml
- cron: "0 1 * * *" # 08:00 Vietnam time
- cron: "0 13 * * *" # 20:00 Vietnam time
```

Manual workflow runs still default to dry-run. Set `submit=true` only when you
want an extra manual real submission.

## Biohub config

This copy already includes `config.toml` for:

```toml
slug = "biohub-cell-tracking-during-development"
enabled = true
auto_submit = true
code_competition = true
auto_copy_notebook = true
source_kernel_versions = {}
dataset_source_versions = {}
```

Before pushing, the main values to review are:

- `sort_by = "scoreDescending"` - scan public kernels from highest score downward
- `kernel_type = "notebook"` - Biohub only accepts submissions from notebooks
- `code_competition = true` - submit an owned Kaggle notebook version instead of uploading the CSV directly
- `auto_copy_notebook = true` - clone the selected public notebook's full source into a private notebook, preserve its Kaggle inputs and accelerator settings, run it, validate the newly generated output, and submit the owned version
- `source_kernel_versions` - optional manual override for exceptional notebooks; leave empty for automatic best-score version resolution
- `dataset_source_versions` - optional manual override for an input dataset version when a public notebook does not pin it itself
- `notebook_wait_timeout_seconds = 21600` - maximum wait for the cloned notebook run
- `submission_wait_timeout_seconds = 1800` - wait for Kaggle's asynchronous scorer before recording success
- `submission_poll_seconds = 15` - scorer status polling interval
- `require_public_score = true` - Biohub is successful only after a public score is present
- `top_n = 100` - how many public kernels to scan each run; Kaggle caps this page at 100
- `file_pattern = "(?i).*submission.*\\.csv$"` - Biohub requires a submission CSV
- `submission_file_name = "submission.csv"` - require the public notebook version to produce that exact native output; alternate artifacts are never renamed for submission
- `required_columns = [...]` - validate the full Biohub node/edge schema, integer values, IDs, edge references, and duplicate node coordinates
- `dataset_shapes` - require all four test datasets and enforce their `[t, z, y, x]` coordinate bounds
- `max_submissions_per_run = 1` - submit at most one new file each run
- `max_submit_attempts_per_run = 10` - if a cloned notebook is rejected or fails, continue downward through the score-sorted list, up to ten new notebook versions per run
- `max_submissions_per_day = 5` - Biohub's daily submission limit and the local guardrail

For Biohub, Kaggle will not let one account submit another user's notebook
session directly. The workflow avoids that permission error by downloading the
public source, rewriting its metadata for a private notebook owned by the
`KAGGLE_API_TOKEN` account, running the complete notebook, and submitting the
returned owned kernel version number. It never submits an output-only relay.

For every score-sorted candidate, the workflow reads the public notebook's
best-score `scriptVersionId`, maps it back to an exact `/versions/N` URL, and
uses `owner/slug/N` as the immutable candidate identity. Before cloning, the
workflow verifies that downloaded source is that same version. Kaggle commonly
allows source download for the current public version but denies historical
source with `403`; such a candidate is skipped instead of running a different
version or relaying old output. If the mapping cannot be verified, the candidate
is also skipped. Because Kaggle's kernel-list response can omit
`currentVersionNumber` (the SDK then reports `0`), the workflow obtains it from
the per-kernel metadata endpoint and can discover a safe version bound from the
public version pages if that endpoint also omits it.

After a source version is submitted, rejected by Kaggle's scorer, or rejected
by local validation, later runs skip that exact `owner/slug/N` and continue
down the score-sorted list. A newly scored version of the same notebook is
treated as a new candidate. Previously attempted SHA-256 hashes are also
skipped, so forks that publish identical bad output do not consume another
submission slot.

For Biohub, only the source version's native `submission.csv` is eligible.
If that file is absent or invalid, the entire source version is recorded as
rejected; an apparently cleaner side artifact such as `submission_clean.csv`
is not renamed and submitted. API acceptance alone is not treated as success:
the workflow waits for final scoring, checks `error_description`, and records
failed scoring attempts separately so they still count toward the daily limit.

## Notes

- The tool does not decide whether a public kernel is allowed by a competition.
  Read the competition rules before enabling real submit.
- The state file records previous submissions. For copied notebook submissions,
  each pushed Kaggle notebook version is tracked separately.
- `scoreDescending` follows Kaggle's kernel-list score order. For competitions
  where a lower metric is better, change this to `scoreAscending`.
- If another competition accepts `.zip` submissions, change `file_pattern` and
  remove or update `required_columns` for that competition.
- GitHub scheduled workflows run on the default branch and use UTC cron.
- GitHub scheduled workflows can be delayed by platform load, so runs may start a
  little after the exact minute.
