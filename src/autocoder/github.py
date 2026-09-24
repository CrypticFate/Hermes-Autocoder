import time
from datetime import datetime
from threading import RLock
from urllib.parse import quote

import httpx
import jwt

from autocoder.config import Settings, secret
from autocoder.redaction import redact, register_secret


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str = "", client=None, sleep=time.sleep, config=None, clock=time.time):
        self._client = client or httpx.Client(
            base_url="https://api.github.com", timeout=30, follow_redirects=False)
        self.sleep, self._clock, self._config = sleep, clock, config
        self._token, self._expires_at = token, 0
        self._lock = RLock()
        register_secret(token)

    def _app_request(self, method, path):
        private_key = secret(self._config.private_key_secret, multiline=True)
        now = int(self._clock())
        assertion = jwt.encode({"iat": now - 60, "exp": now + 540,
                                "iss": str(self._config.app_id)}, private_key, algorithm="RS256")
        register_secret(assertion)
        return self._request(method, path, _authorization=assertion)

    def access_token(self):
        with self._lock:
            if self._config and self._config.mode == "app" and self._expires_at <= self._clock() + 60:
                result = self._app_request("POST", f"/app/installations/{self._config.installation_id}/access_tokens")
                self._token = result["token"]
                register_secret(self._token)
                self._expires_at = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
                if self._expires_at <= self._clock() + 60:
                    raise GitHubError("Installation credential expired")
            return self._token

    def authenticated_user(self):
        return self._request("GET", "/user")

    def verify_identity(self):
        if self._config is None:
            raise GitHubError("Bot identity configuration is required")
        if self._config.mode == "app":
            app = self._app_request("GET", "/app")
            if app["id"] != self._config.app_id:
                raise GitHubError("Authenticated App does not match configured app_id")
            login = app["slug"] + "[bot]"
            self.access_token()
        else:
            login = self.authenticated_user()["login"]
        if login.lower() != self._config.bot_login.lower():
            raise GitHubError("Authenticated identity does not match github.bot_login")
        return login

    def repository(self, repo):
        return self._request("GET", f"/repos/{repo}")

    def permission(self, repo, login):
        return self._request("GET", f"/repos/{repo}/collaborators/{quote(login, safe='')}/permission")

    def branch(self, repo, branch):
        return self._request("GET", f"/repos/{repo}/branches/{quote(branch, safe='')}")

    def branch_rules(self, repo, branch):
        return list(self._pages(f"/repos/{repo}/rules/branches/{quote(branch, safe='')}"))

    def label_pr(self, repo, number):
        try:
            self._request("POST", f"/repos/{repo}/labels", json={"name": "autocoder", "color": "228B22"})
        except GitHubError:
            pass
        return self._request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": ["autocoder"]})

    def delete_branch(self, repo, branch):
        if not branch.startswith("agent/"):
            raise ValueError("Only agent branches can be removed")
        return self._request("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}")

    def _request(self, method, path, _authorization=None, **kwargs):
        for attempt in range(4):
            try:
                response = self._client.request(method, path, headers={
                    "Authorization": f"Bearer {_authorization or self.access_token()}",
                    "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, **kwargs)
            except httpx.TransportError:
                # A write may have reached GitHub; caller reconciles before retrying.
                if method != "GET" or attempt == 3:
                    raise GitHubError("GitHub transport failure; reconciliation required") from None
                self.sleep(2 ** attempt)
                continue
            limited = response.status_code == 429 or (
                response.status_code == 403 and
                (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers))
            if limited or response.status_code >= 500:
                delay = max(float(response.headers.get("retry-after", 0)),
                            float(response.headers.get("x-ratelimit-reset", 0)) - time.time(), 2 ** attempt)
                if attempt == 3 or delay > 30 or method != "GET":
                    raise GitHubError(f"GitHub unavailable/rate limited ({response.status_code}); retry later")
                self.sleep(delay)
                continue
            if response.is_error:
                raise GitHubError(f"GitHub {method} {path}: HTTP {response.status_code}")
            return response.json() if response.content else None

    def _pages(self, path, _key=None, **params):
        page = 1
        while True:
            rows = self._request("GET", path, params={**params, "per_page": 100, "page": page})
            if _key:
                rows = rows[_key]
            yield from rows
            if len(rows) < 100:
                return
            page += 1

    def repositories(self, owner):
        if self._config:
            self.verify_identity()
        if self._config and self._config.mode == "app":
            rows = self._pages("/installation/repositories", _key="repositories")
        else:
            rows = self._pages("/user/repos", affiliation="owner,collaborator,organization_member", visibility="all")
        result = [r for r in rows if r["owner"]["login"].lower() == owner.lower()]
        # GitHub's size statistic lags after initial commits; branches are authoritative.
        for repo in result:
            if repo.get("size") == 0:
                branches = self._request("GET", f"/repos/{repo['full_name']}/branches", params={"per_page": 1})
                if branches:
                    repo["size"] = 1
        return result

    def pull(self, repo, number):
        return self._request("GET", f"/repos/{repo}/pulls/{number}")

    def ensure_pr(self, repo, branch, base, title, body, draft):
        if not branch.startswith("agent/"):
            raise GitHubError("Pull requests may only be opened from agent branches")
        title, body = redact(title), redact(body)
        owner = repo.split("/")[0]
        existing = self._request("GET", f"/repos/{repo}/pulls",
                                params={"state": "all", "head": f"{owner}:{branch}"})
        if existing:
            pr = existing[0]
            if pr["state"] == "open":
                updated = self._request("PATCH", f"/repos/{repo}/pulls/{pr['number']}",
                                        json={"title": title, "body": body})
                if bool(updated.get("draft")) != bool(draft) and updated.get("node_id"):
                    self.set_draft(updated["node_id"], draft)
                    updated["draft"] = draft
                return updated
            return pr
        return self._request("POST", f"/repos/{repo}/pulls",
                            json={"title": title, "head": branch, "base": base, "body": body, "draft": draft})

    def issue_comments(self, repo, number):
        return list(self._pages(f"/repos/{repo}/issues/{number}/comments"))

    def review_comments(self, repo, number):
        return list(self._pages(f"/repos/{repo}/pulls/{number}/comments"))

    def reviews(self, repo, number):
        return list(self._pages(f"/repos/{repo}/pulls/{number}/reviews"))

    def check_runs(self, repo, sha):
        return list(self._pages(f"/repos/{repo}/commits/{quote(sha, safe='')}/check-runs", _key="check_runs"))

    def check_run_annotations(self, repo, check_run_id):
        return self._request("GET", f"/repos/{repo}/check-runs/{int(check_run_id)}/annotations",
                             params={"per_page": 50})

    def comment_pr(self, repo, number, body):
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": redact(body)})

    def close_pr(self, repo, number):
        """Close a superseded agent pull request without integrating it."""
        pr = self.pull(repo, number)
        if not pr["head"]["ref"].startswith("agent/"):
            raise GitHubError("Only agent pull requests can be closed by the gatekeeper")
        return self._request("PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})

    def set_draft(self, node_id, draft):
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        result = self._request("POST", "/graphql", json={
            "query": f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) {{ pullRequest {{ id }} }} }}",
            "variables": {"id": node_id}})
        if result.get("errors"):
            raise GitHubError("Could not change pull request draft state")

    def ready_pr(self, node_id):
        self.set_draft(node_id, False)


# Compatibility import name for existing callers; the public API has explicit methods only.
GitHub = GitHubClient


def for_owner(settings: Settings, owner: str) -> GitHubClient:
    if owner.lower() not in {entry.lower() for entry in settings.owners}:
        raise GitHubError("Owner is not configured")
    config = settings.github
    token = secret(config.token_secret) if config.mode == "bot_pat" else ""
    return GitHubClient(token, config=config)
