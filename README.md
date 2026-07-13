# Kaggle Public Kernel Submitter

A small GitHub Actions-friendly tool that scans public Kaggle kernels for one
or more competitions, finds matching submission files, and optionally submits
them with the Kaggle CLI/API. For notebook-only competitions, it can copy the
selected public notebook into your Kaggle account, run that owned copy, and
submit the resulting notebook version.

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
- `work/` - temporary pulled notebook source copies, ignored by git
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
- at most 1 submission per scheduled run and 2 submissions per day

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
source_kernel_versions = { "pilkwang/biohub-cell-tracking-blend-preprocessings" = 25 }
dataset_source_versions = { "pilkwang/biohub-deepcenter-unet3d-center-prior-v1" = 4 }
```

Before pushing, the main values to review are:

- `sort_by = "scoreDescending"` - scan public kernels from highest score downward
- `kernel_type = "notebook"` - Biohub only accepts submissions from notebooks
- `code_competition = true` - submit an owned Kaggle notebook version instead of uploading the CSV directly
- `auto_copy_notebook = true` - pull the selected public notebook, push it as a private notebook under your Kaggle account, wait for it to finish, then submit that version
- `source_kernel_versions` - reproduce the scored public notebook version instead of silently pulling its newest edit
- `dataset_source_versions` - reproduce exact input dataset versions; Biohub pins DeepCenter V4 because its checkpoint is epoch 400
- `notebook_wait_timeout_seconds = 21600` - wait up to 6 hours for the copied notebook run
- `top_n = 100` - how many public kernels to scan each run; Kaggle caps this page at 100
- `file_pattern = "(?i).*submission.*\\.csv$"` - Biohub requires a submission CSV
- `required_columns = [...]` - skip downloaded CSVs whose header does not match Biohub's required submission format
- `max_submissions_per_run = 1` - submit at most one new file each run
- `max_submit_attempts_per_run = 3` - if a copied notebook fails, continue downward through the score-sorted list, up to three attempts per run
- `max_submissions_per_day = 2` - local guardrail; also respect Kaggle's own limit

For Biohub, Kaggle will not let one account submit another user's notebook
session directly. The workflow avoids that permission error by creating a fresh
private notebook copy in the Kaggle account represented by `KAGGLE_API_TOKEN`,
then submitting the returned kernel version number.

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
