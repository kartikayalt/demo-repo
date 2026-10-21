"""Sync GitHub pull request data into the Pulse PR Approval Gateway tables in Supabase.

Runs inside GitHub Actions. On a pull_request_target event it syncs that PR; on
workflow_dispatch it backfills recent PRs of the repository.

Tables written (schema SUPABASE_SCHEMA): github_owner, github_repository,
pull_request, pr_commit, pr_file, pr_activity.

Env:
  GITHUB_TOKEN        token for the GitHub REST API
  SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY   write via the Supabase REST API (preferred), or
  SUPABASE_DB_URL     Postgres connection string (Supabase pooler) for a direct connection
  SUPABASE_SCHEMA     solution schema name
  SYSTEM_USER_ID      UUID written to created_by / last_changed_by
  BACKFILL_STATE      all | open | closed (workflow_dispatch only)
  BACKFILL_MAX_PRS    max PRs to backfill (workflow_dispatch only)
  DRY_RUN=1           fetch from GitHub and print the mapped rows, no DB writes
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SCHEMA = os.environ.get("SUPABASE_SCHEMA", "solution_7df54fce_dd04_44aa_b5a3_efb38820254c")
SYSTEM_USER = os.environ.get("SYSTEM_USER_ID") or "00000000-0000-0000-0000-000000000000"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
JOB_CHECK_NAME = "pulse-pr-sync"  # our own check run, excluded from ci_status
MAX_COMMIT_STATS = 100  # per-commit +/- lines need one API call each

# ---------------------------------------------------------------- GitHub API

def gh(path, accept="application/vnd.github+json", raw=False):
    url = path if path.startswith("http") else f"{API}{path}"
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pulse-pr-sync"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                body = r.read()
                return (body.decode("utf-8", "replace") if raw else json.loads(body)), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) or (e.code == 403 and "rate limit" in e.read().decode("utf-8", "replace").lower()):
                time.sleep(2 ** attempt * 3)
                continue
            raise
    raise RuntimeError(f"GitHub API failed after retries: {url}")


def gh_paged(path, limit=None):
    items, url = [], f"{API}{path}{'&' if '?' in path else '?'}per_page=100"
    while url:
        page, headers = gh(url)
        items.extend(page)
        if limit and len(items) >= limit:
            return items[:limit]
        url = None
        for part in (headers.get("Link") or "").split(","):
            if 'rel="next"' in part:
                url = part[part.index("<") + 1:part.index(">")]
    return items


_users = {}
def user_name(login):
    if not login:
        return None
    if login not in _users:
        try:
            _users[login] = gh(f"/users/{login}")[0].get("name")
        except urllib.error.HTTPError:
            _users[login] = None
    return _users[login]

# ---------------------------------------------------------------- mapping

def ts(value):
    """GitHub ISO timestamp -> naive UTC datetime (columns are timestamp without time zone)."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


MERGEABLE = {"clean": "CLEAN", "has_hooks": "CLEAN", "unstable": "CLEAN",
             "blocked": "BLOCKED", "dirty": "DIRTY", "behind": "BEHIND"}
CHANGE = {"added": "ADDED", "removed": "REMOVED", "renamed": "RENAMED"}


def ci_status(repo, sha):
    runs = gh(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")[0].get("check_runs", [])
    runs = [r for r in runs if r.get("name") != JOB_CHECK_NAME]
    statuses = gh(f"/repos/{repo}/commits/{sha}/status")[0].get("statuses", [])
    if not runs and not statuses:
        return "UNKNOWN"
    bad = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "error"}
    if any(r.get("conclusion") in bad for r in runs) or any(s.get("state") in ("failure", "error") for s in statuses):
        return "FAILURE"
    if any(r.get("status") != "completed" for r in runs) or any(s.get("state") == "pending" for s in statuses):
        return "PENDING"
    return "SUCCESS"


def fetch_pr(repo, number):
    pr = None
    for _ in range(3):  # GitHub computes mergeability asynchronously
        pr = gh(f"/repos/{repo}/pulls/{number}")[0]
        if pr.get("mergeable_state") not in (None, "unknown") or pr["state"] != "open":
            break
        time.sleep(3)
    commits = gh_paged(f"/repos/{repo}/pulls/{number}/commits", limit=250)
    files = gh_paged(f"/repos/{repo}/pulls/{number}/files", limit=3000)
    stats = {}
    for c in commits[-MAX_COMMIT_STATS:]:
        try:
            stats[c["sha"]] = gh(f"/repos/{repo}/commits/{c['sha']}")[0].get("stats", {})
        except urllib.error.HTTPError:
            pass
    return pr, commits, files, stats


def map_owner(repo_json):
    o = repo_json["owner"]
    return dict(login=o["login"], owner_type="ORGANIZATION" if o.get("type") == "Organization" else "USER",
                display_name=user_name(o["login"]) or o["login"], avatar_url=o.get("avatar_url"),
                html_url=o.get("html_url"))


def map_repo(r):
    return dict(repo_name=r["name"], repo_full_name=r["full_name"], description=r.get("description"),
                default_branch=r.get("default_branch") or "main",
                visibility=(r.get("visibility") or ("private" if r.get("private") else "public")).upper(),
                primary_language=r.get("language"), html_url=r.get("html_url"), last_pushed_at=ts(r.get("pushed_at")))


def map_pr(repo, pr, run_url):
    state = "MERGED" if pr.get("merged") or pr.get("merged_at") else ("OPEN" if pr["state"] == "open" else "CLOSED")
    return dict(
        pr_number=pr["number"], title=pr["title"][:500], description=pr.get("body"), pr_url=pr["html_url"],
        diff_url=pr.get("diff_url"), pr_state=state, is_draft=bool(pr.get("draft")),
        head_branch=pr["head"]["ref"], base_branch=pr["base"]["ref"], head_sha=pr["head"]["sha"],
        author_login=pr["user"]["login"], author_name=user_name(pr["user"]["login"]),
        author_avatar_url=pr["user"].get("avatar_url"),
        commit_count=pr.get("commits") or 0, changed_files=pr.get("changed_files") or 0,
        additions=pr.get("additions") or 0, deletions=pr.get("deletions") or 0,
        labels=",".join(l["name"] for l in pr.get("labels", []))[:500] or None,
        ci_status=ci_status(repo, pr["head"]["sha"]),
        mergeable_state=MERGEABLE.get(pr.get("mergeable_state"), "UNKNOWN"),
        merged_by=(pr.get("merged_by") or {}).get("login"), merge_commit_sha=pr.get("merge_commit_sha") if state == "MERGED" else None,
        merged_at=ts(pr.get("merged_at")), closed_at=ts(pr.get("closed_at")) if state == "CLOSED" else None,
        workflow_run_url=run_url, pr_created_at=ts(pr.get("created_at")), pr_updated_at=ts(pr.get("updated_at")),
    )


def map_commits(repo, commits, stats):
    rows = []
    for c in commits:
        a, s = c["commit"]["author"] or {}, stats.get(c["sha"], {})
        rows.append(dict(commit_sha=c["sha"], short_sha=c["sha"][:7], message=c["commit"]["message"],
                         author_name=a.get("name") or (c.get("author") or {}).get("login") or "unknown",
                         author_email=a.get("email"), author_login=(c.get("author") or {}).get("login"),
                         author_avatar_url=(c.get("author") or {}).get("avatar_url"),
                         committed_at=ts(a.get("date")) or now(), additions=s.get("additions", 0),
                         deletions=s.get("deletions", 0), commit_url=c.get("html_url")))
    return rows


def map_files(files):
    return [dict(file_path=f["filename"][:500], previous_file_path=f.get("previous_filename"),
                 change_type=CHANGE.get(f["status"], "MODIFIED"), additions=f.get("additions", 0),
                 deletions=f.get("deletions", 0), patch=f.get("patch"),
                 is_patch_truncated="patch" not in f and f.get("changes", 0) > 0, blob_url=f.get("blob_url"))
            for f in files]


def activities(pr_row, event_action, sender, before_sha):
    """Timeline entries; deduplicated on (type, activity_at) when written."""
    out = [("OPENED", pr_row["author_login"], None, pr_row["pr_created_at"])]
    if pr_row["pr_state"] == "MERGED":
        out.append(("MERGED", pr_row["merged_by"] or sender, f"Merged into {pr_row['base_branch']} ({(pr_row['merge_commit_sha'] or '')[:7]})", pr_row["merged_at"]))
    elif pr_row["pr_state"] == "CLOSED":
        out.append(("CLOSED", sender or pr_row["author_login"], "Closed without merge", pr_row["closed_at"]))
    if event_action == "synchronize":
        out.append(("COMMITS_PUSHED", sender, f"Head moved {(before_sha or '')[:7]} -> {pr_row['head_sha'][:7]}", now()))
    elif event_action == "reopened":
        out.append(("REOPENED", sender, None, now()))
    elif event_action == "ready_for_review":
        out.append(("COMMITS_PUSHED", sender, "Marked ready for review", now()))
    return [a for a in out if a[3]]

# ---------------------------------------------------------------- storage
# Two interchangeable stores with the same small interface:
#   RestStore  - Supabase REST API (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)
#   PgStore    - direct Postgres connection (SUPABASE_DB_URL), single transaction

def _jsonable(row):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


class RestStore:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                        "Accept-Profile": SCHEMA, "Content-Profile": SCHEMA}

    def _call(self, method, table, params=None, body=None, prefer=None):
        url = self.base + table + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {**self.headers, **({"Prefer": prefer} if prefer else {})}
        data = json.dumps(body, default=str).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=30) as r:
                text = r.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Supabase {method} {table} failed ({e.code}): {e.read().decode('utf-8', 'replace')}") from None

    @staticmethod
    def _filters(match):
        return {k: f"eq.{v.isoformat() if isinstance(v, datetime) else v}" for k, v in match.items()}

    def find(self, table, match, cols):
        rows = self._call("GET", table, {**self._filters(match), "select": ",".join(cols), "order": "created_at", "limit": 1})
        return rows[0] if rows else None

    def insert(self, table, row, pk):
        return self._call("POST", table, {"select": pk}, [_jsonable(row)], prefer="return=representation")[0][pk]

    def update(self, table, row, match):
        self._call("PATCH", table, self._filters(match), _jsonable(row), prefer="return=minimal")

    def delete(self, table, match):
        self._call("DELETE", table, self._filters(match), prefer="return=minimal")

    def count(self, table, match):
        rows = self._call("GET", table, {**self._filters(match), "select": "count"})
        return rows[0]["count"]


class PgStore:
    def __init__(self, conn):
        from psycopg2 import sql
        self.sql, self.cur = sql, conn.cursor()

    def _t(self, name):
        return self.sql.Identifier(SCHEMA, name)

    def _where(self, match):
        return self.sql.SQL(" AND ").join(self.sql.SQL("{} = %s").format(self.sql.Identifier(k)) for k in match)

    def find(self, table, match, cols):
        self.cur.execute(self.sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY created_at LIMIT 1").format(
            self.sql.SQL(",").join(map(self.sql.Identifier, cols)), self._t(table), self._where(match)), list(match.values()))
        row = self.cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def insert(self, table, row, pk):
        cols = list(row)
        self.cur.execute(self.sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            self._t(table), self.sql.SQL(",").join(map(self.sql.Identifier, cols)),
            self.sql.SQL(",").join(self.sql.Placeholder() * len(cols)), self.sql.Identifier(pk)), [row[c] for c in cols])
        return self.cur.fetchone()[0]

    def update(self, table, row, match):
        self.cur.execute(self.sql.SQL("UPDATE {} SET {} WHERE {}").format(
            self._t(table), self.sql.SQL(",").join(self.sql.SQL("{} = %s").format(self.sql.Identifier(c)) for c in row),
            self._where(match)), [*row.values(), *match.values()])

    def delete(self, table, match):
        self.cur.execute(self.sql.SQL("DELETE FROM {} WHERE {}").format(self._t(table), self._where(match)), list(match.values()))

    def count(self, table, match):
        self.cur.execute(self.sql.SQL("SELECT count(*) FROM {} WHERE {}").format(self._t(table), self._where(match)), list(match.values()))
        return self.cur.fetchone()[0]


def insert(db, table, row):
    return db.insert(table, {**row, "created_by": SYSTEM_USER, "last_changed_by": SYSTEM_USER}, f"{table}_id")


def update(db, table, row, pk_value):
    db.update(table, {**row, "last_changed_by": SYSTEM_USER}, {f"{table}_id": pk_value})


def upsert(db, table, match, row, insert_only=None):
    """No unique constraints exist, so look up by natural key, then update or insert."""
    found = db.find(table, match, [f"{table}_id"])
    if found:
        update(db, table, row, found[f"{table}_id"])
        return found[f"{table}_id"]
    return insert(db, table, {**match, **row, **(insert_only or {})})


def repo_counts(db, repo, repo_id):
    q = lambda s: gh(f"/search/issues?q={urllib.parse.quote(f'repo:{repo} is:pr {s}')}&per_page=1")[0]["total_count"]
    try:
        return dict(open_pr_count=q("is:open"), closed_pr_count=q("is:closed is:unmerged"), merged_pr_count=q("is:merged"))
    except urllib.error.HTTPError:  # search API rate-limited: count synced rows instead
        return {f"{s.lower()}_pr_count": db.count("pull_request", {"github_repository_id": repo_id, "pr_state": s})
                for s in ("OPEN", "CLOSED", "MERGED")}


def sync(db, repo_json, prs, event_action=None, sender=None, before_sha=None, run_url=None):
    repo = repo_json["full_name"]
    sync_at = now()

    owner = map_owner(repo_json)
    owner_id = upsert(db, "github_owner", {"login": owner.pop("login")}, {**owner, "last_synced_at": sync_at},
                      insert_only={"status": "ACTIVE", "repository_count": 0})
    # approval_required, default_merge_method and status are admin settings: set on insert only.
    repo_id = upsert(db, "github_repository", {"repo_full_name": repo},
                     {**map_repo(repo_json), "github_owner_id": owner_id, "last_synced_at": sync_at},
                     insert_only={"approval_required": True, "default_merge_method": "MERGE", "status": "ACTIVE",
                                  "open_pr_count": 0, "closed_pr_count": 0, "merged_pr_count": 0})
    approval_required = db.find("github_repository", {"github_repository_id": repo_id}, ["approval_required"])["approval_required"]

    for number in prs:
        pr, commits, files, stats = fetch_pr(repo, number)
        row = map_pr(repo, pr, run_url)
        existing = db.find("pull_request", {"github_repository_id": repo_id, "pr_number": number},
                           ["pull_request_id", "head_sha", "approval_status"])
        if existing is None:
            # PRs already closed/merged before the gateway existed never needed approval.
            row["approval_status"] = "PENDING" if approval_required and row["pr_state"] == "OPEN" else "NOT_REQUIRED"
            pr_id = insert(db, "pull_request", {"github_repository_id": repo_id, **row})
        else:
            pr_id, old_status = existing["pull_request_id"], existing["approval_status"]
            reset = (existing["head_sha"] != row["head_sha"] and old_status in ("APPROVED", "REJECTED", "MERGE_FAILED")) \
                or (event_action == "reopened" and old_status == "REJECTED")
            if reset and approval_required and row["pr_state"] == "OPEN":
                row.update(approval_status="PENDING", decided_by=None, decision_comment=None, decided_at=None)
            update(db, "pull_request", row, pr_id)

        for table, rows in (("pr_commit", map_commits(repo, commits, stats)), ("pr_file", map_files(files))):
            db.delete(table, {"pull_request_id": pr_id})
            for r in rows:
                insert(db, table, {"pull_request_id": pr_id, **r})

        for kind, actor, comment, at in activities(row, event_action, sender, before_sha):
            if not db.find("pr_activity", {"pull_request_id": pr_id, "activity_type": kind, "activity_at": at}, ["pr_activity_id"]):
                insert(db, "pr_activity", dict(pull_request_id=pr_id, activity_type=kind, activity_source="GITHUB",
                                               actor=actor or "github", comment=comment, activity_at=at))
        print(f"synced {repo}#{number}: state={row['pr_state']} approval={row.get('approval_status', existing and existing['approval_status'])} "
              f"commits={len(commits)} files={len(files)}")

    update(db, "github_repository", repo_counts(db, repo, repo_id), repo_id)
    update(db, "github_owner", {"repository_count": db.count("github_repository", {"github_owner_id": owner_id})}, owner_id)

# ---------------------------------------------------------------- entry point

def main():
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = json.load(f)
    repo_json = gh(f"/repos/{event['repository']['full_name']}")[0]
    run_url = f"{SERVER}/{repo_json['full_name']}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"

    if event_name.startswith("pull_request"):
        prs, action = [event["pull_request"]["number"]], event.get("action")
        sender, before = (event.get("sender") or {}).get("login"), event.get("before")
    else:
        state = os.environ.get("BACKFILL_STATE") or "all"
        limit = int(os.environ.get("BACKFILL_MAX_PRS") or 100)
        prs = [p["number"] for p in gh_paged(f"/repos/{repo_json['full_name']}/pulls?state={state}&sort=created&direction=desc", limit)]
        action = sender = before = None
        print(f"backfilling {len(prs)} PRs ({state})")

    if DRY_RUN:
        print(json.dumps({"owner": map_owner(repo_json), "repository": map_repo(repo_json)}, default=str, indent=2))
        for n in prs:
            pr, commits, files, stats = fetch_pr(repo_json["full_name"], n)
            row = map_pr(repo_json["full_name"], pr, run_url)
            print(json.dumps({"pull_request": row, "commits": map_commits(repo_json["full_name"], commits, stats)[:3],
                              "files": [{**f, "patch": (f["patch"] or "")[:200]} for f in map_files(files)[:3]],
                              "activities": activities(row, action, sender, before)}, default=str, indent=2))
        return

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("writing via Supabase REST API")
        sync(RestStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]), repo_json, prs, action, sender, before, run_url)
        return
    import psycopg2
    print("writing via direct Postgres connection")
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=15, application_name="pulse-pr-sync")
    try:
        with conn:  # one transaction: all or nothing
            sync(PgStore(conn), repo_json, prs, action, sender, before, run_url)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface a clean error in the Actions log
        print(f"::error::Pulse PR sync failed: {e}", file=sys.stderr)
        raise
