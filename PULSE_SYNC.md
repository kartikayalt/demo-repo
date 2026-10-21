# Pulse PR Sync (GitHub Actions → Supabase)

Copies every pull request in this repository into the Pulse PR Approval Gateway tables
(schema `solution_7df54fce_dd04_44aa_b5a3_efb38820254c`, project **Metafore-Prod-2026**) so Pulse can render them.

| Table | What gets written |
|---|---|
| `github_owner` | org/user login, type, display name, avatar, repo count |
| `github_repository` | name, description, default branch, visibility, language, last push, open / closed / merged PR counts |
| `pull_request` | number, title, body, URLs, state (OPEN/CLOSED/MERGED), draft, branches, head SHA, author, commits, files, +/-, labels, CI status, mergeability, merge info, approval_status |
| `pr_commit` | every commit: SHA, message, author name/email/login/avatar, date, +/- |
| `pr_file` | every changed file: path, change type, +/-, **diff patch** (for View Diff) |
| `pr_activity` | timeline: OPENED, COMMITS_PUSHED, REOPENED, MERGED, CLOSED |

## Files to copy into your repo

```
.github/workflows/pulse-pr-sync.yml
.github/scripts/sync_pr_to_supabase.py
```

Commit both to the **default branch** (e.g. `main`) first. The workflow uses `pull_request_target`, so it always runs the
default-branch copy: a PR can't edit it to read the secret, and PRs from forks are synced too. PR code is never checked out.

## Step 1 – Allow REST access to the schema (one time, in Supabase)

The schema is already listed in the REST API's exposed schemas, but no API role has privileges on it yet.
Run this in the Supabase SQL editor for **Metafore-Prod-2026**:

```sql
GRANT USAGE ON SCHEMA solution_7df54fce_dd04_44aa_b5a3_efb38820254c TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.github_owner,
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.github_repository,
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.pull_request,
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.pr_commit,
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.pr_file,
  solution_7df54fce_dd04_44aa_b5a3_efb38820254c.pr_activity
TO service_role;
NOTIFY pgrst, 'reload schema';
```

Grant only `service_role`. These tables have Row Level Security **disabled**, so granting `anon` or `authenticated`
would let anyone with the public anon key read and edit every row.

Optional but recommended (prevents duplicate rows if two syncs ever race):

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_github_owner_login ON solution_7df54fce_dd04_44aa_b5a3_efb38820254c.github_owner (login);
CREATE UNIQUE INDEX IF NOT EXISTS ux_github_repository_full_name ON solution_7df54fce_dd04_44aa_b5a3_efb38820254c.github_repository (repo_full_name);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pull_request_repo_number ON solution_7df54fce_dd04_44aa_b5a3_efb38820254c.pull_request (github_repository_id, pr_number);
```

## Step 2 – Add repository secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | `https://bwdgtccbfkdtnvxgkiva.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase dashboard → Project Settings → API Keys → `service_role` (secret) key |

**Fallback without Step 1:** leave those two unset and add `SUPABASE_DB_URL` instead (dashboard → **Connect** →
*Session pooler* connection string, with the DB password filled in). The script then writes over a direct Postgres
connection in a single transaction.

Optional repository **variables** (same page, *Variables* tab):

| Variable | Default |
|---|---|
| `PULSE_SUPABASE_SCHEMA` | `solution_7df54fce_dd04_44aa_b5a3_efb38820254c` |
| `PULSE_SYSTEM_USER_ID` | `00000000-0000-0000-0000-000000000000` (written to `created_by` / `last_changed_by`) |

## Step 3 – Demo

1. Push a branch and open a PR → the **Pulse PR Sync** run starts within seconds (Actions tab).
2. Rows appear in Supabase; the PR shows in Pulse with `approval_status = PENDING`.
3. Push another commit → commits/files refresh, a `COMMITS_PUSHED` activity is added, and any earlier approval is
   reset to `PENDING`.
4. Merge or close → `pr_state` becomes `MERGED` / `CLOSED` with a timeline entry.

**Backfill existing PRs:** Actions → Pulse PR Sync → *Run workflow* → choose state (`all`) and max PRs. PRs that were
already closed or merged are stored with `approval_status = NOT_REQUIRED`.

## Behaviour notes

- Events: `opened`, `reopened`, `synchronize`, `ready_for_review`, `edited`, `closed`.
- Re-running a sync updates rows instead of duplicating them. Commits and files are replaced on every run, and
  activity entries are de-duplicated.
- Pulse-owned fields are never overwritten: `approval_required`, `default_merge_method` and repository `status` are set
  only when the repository is first created, and `decided_by` / `decision_comment` / `decided_at` are cleared only when
  new commits arrive after a decision.
- New repositories default to `approval_required = true`. Change it in Supabase or Pulse to stop requiring approval.
- Repository PR counts come from the GitHub search API. If that is rate-limited, the script counts the synced rows
  instead.
- Commit +/- lines are fetched for the last 100 commits of a PR. Files beyond GitHub's 3,000-file limit are skipped.
  Patches GitHub omits (binary or very large files) are flagged with `is_patch_truncated = true`.
- Approve/Reject in Pulse → merge back to GitHub is handled separately by the Camel integration (`merge_github_pull_request`).
