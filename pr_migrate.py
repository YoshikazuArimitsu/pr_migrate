#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr_migrate.py - GitHub / Backlog Git 間で Pull Request を移行・同期するツール

  移行元 (source) の PR を取得し、移行先 (target) リポジトリに同じ PR を作成します。
  2 回目以降の実行では、追加・編集されたコメント、タイトル/説明の変更、
  クローズ/マージ状態を差分同期します（状態は state ファイルで管理）。

  対応する組み合わせ:  GitHub -> GitHub / GitHub -> Backlog / Backlog -> GitHub / Backlog -> Backlog

  使い方 (詳細は README.md):
    python pr_migrate.py check            接続確認
    python pr_migrate.py list             移行元 PR の一覧と移行状況
    python pr_migrate.py migrate          PR を作成（未移行のもの）＋同期
    python pr_migrate.py sync             移行済み PR のみ同期（新規作成しない）

  依存: Python 3.8+ の標準ライブラリのみ / ブランチ転送に git コマンド
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"

# ログ出力からトークン類を伏せるためのリスト
SECRETS: List[str] = []
DEBUG = False


# --------------------------------------------------------------------------- #
# ユーティリティ
# --------------------------------------------------------------------------- #
def mask(text: Any) -> str:
    s = str(text)
    for sec in SECRETS:
        if sec:
            s = s.replace(sec, "***")
    return s


def log(msg: str, level: str = "INFO") -> None:
    print(f"[{datetime.now():%H:%M:%S}] {level:<5} {mask(msg)}", flush=True)


def warn(msg: str) -> None:
    log(msg, "WARN")


def error(msg: str) -> None:
    log(msg, "ERROR")


def debug(msg: str) -> None:
    if DEBUG:
        log(msg, "DEBUG")


class ConfigError(Exception):
    pass


class MigrateError(Exception):
    pass


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def parse_time(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_TZ_OFFSET = 9


def fmt_time(s: Optional[str]) -> str:
    dt = parse_time(s)
    if not dt:
        return s or "-"
    tz = timezone(timedelta(hours=_TZ_OFFSET))
    sign = "+" if _TZ_OFFSET >= 0 else "-"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") + f" (UTC{sign}{abs(_TZ_OFFSET):g})"


def truncate(text: str, limit: int = 60000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n…（文字数上限のため以下省略）"


def load_dotenv(paths: List[Path]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for p in paths:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def resolve_secret(cfg: dict, key: str, env_key: str, default_env: str,
                   dotenv: Dict[str, str], label: str) -> str:
    val = cfg.get(key)
    if not val:
        name = cfg.get(env_key) or default_env
        val = os.environ.get(name) or dotenv.get(name)
        if not val:
            raise ConfigError(f"{label}: 認証情報がありません。環境変数 / .env の {name} を設定してください")
    SECRETS.append(val)
    return val


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, method: str, url: str, body: str):
        self.status = status
        self.body = body
        super().__init__(mask(f"HTTP {status} {method} {url.split('?')[0]}: {body[:800]}"))


class Http:
    def __init__(self, headers: Optional[dict] = None, base_params: Optional[dict] = None):
        self.headers = headers or {}
        self.base_params = base_params or {}

    @staticmethod
    def _retry_wait(headers, attempt: int) -> float:
        ra = headers.get("Retry-After") if headers else None
        if ra and ra.isdigit():
            return min(int(ra) + 1, 900)
        reset = None
        if headers:
            reset = headers.get("X-RateLimit-Reset") or headers.get("X-Ratelimit-Reset")
        if reset and reset.isdigit():
            return min(max(int(reset) - time.time() + 2, 1), 900)
        return min(2 ** attempt * 2, 120)

    def request(self, method: str, url: str, params=None, json_body=None, form=None):
        query: List[Tuple[str, Any]] = list(self.base_params.items())
        if params:
            query.extend(params.items() if isinstance(params, dict) else params)
        if query:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
        data = None
        headers = dict(self.headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        for attempt in range(6):
            debug(f"{method} {url}")
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    body = json.loads(raw.decode("utf-8")) if raw.strip() else None
                    return body, resp.headers
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                limited = e.code in (429, 502, 503, 504) or (
                    e.code == 403 and ("rate limit" in text.lower() or "abuse" in text.lower()))
                if limited and attempt < 5:
                    wait = self._retry_wait(e.headers, attempt)
                    warn(f"HTTP {e.code}（レート制限など）: {wait:.0f} 秒待って再試行します")
                    time.sleep(wait)
                    continue
                raise ApiError(e.code, method, url, text) from None
            except urllib.error.URLError as e:
                if attempt < 5:
                    warn(f"通信エラー（{e.reason}）: 再試行します")
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise MigrateError("HTTP リクエストが失敗しました")


# --------------------------------------------------------------------------- #
# データモデル
# --------------------------------------------------------------------------- #
@dataclass
class PR:
    number: int
    title: str
    body: str
    state: str                 # open / closed / merged
    base: str
    head: str
    base_sha: Optional[str]
    head_sha: Optional[str]
    author: str
    created: str
    url: str
    author_id: Optional[str] = None
    draft: bool = False
    merged_at: Optional[str] = None
    closed_at: Optional[str] = None
    assignee: Optional[str] = None
    issue_key: Optional[str] = None
    is_fork: bool = False


@dataclass
class Comment:
    id: str                    # プロバイダ別の接頭辞付き ID (ic/rv/rc/bc)
    kind: str                  # issue / review / review_comment
    author: str
    created: str
    body: str
    url: Optional[str] = None
    review_state: Optional[str] = None
    path: Optional[str] = None
    line: Optional[int] = None
    start_line: Optional[int] = None
    side: Optional[str] = None
    start_side: Optional[str] = None
    commit_id: Optional[str] = None
    original_line: Optional[int] = None
    original_commit_id: Optional[str] = None
    diff_hunk: Optional[str] = None
    in_reply_to: Optional[str] = None


class Fmt:
    """移行先の記法 (markdown / backlog) に合わせた最低限の整形"""

    def __init__(self, kind: str = "markdown"):
        if kind not in ("markdown", "backlog"):
            raise ConfigError(f"text_format は markdown か backlog を指定してください: {kind}")
        self.kind = kind

    def bold(self, s: str) -> str:
        return f"**{s}**" if self.kind == "markdown" else f"''{s}''"

    def code(self, s: str) -> str:
        return f"`{s}`" if self.kind == "markdown" else s

    def link(self, label: str, url: str) -> str:
        return f"[{label}]({url})" if self.kind == "markdown" else f"[[{label}>{url}]]"

    def code_block(self, text: str, lang: str = "") -> str:
        if self.kind == "markdown":
            return f"```{lang}\n{text}\n```"
        return "{code}\n" + text + "\n{/code}"

    def quote(self, text: str) -> str:
        return "\n".join("> " + ln for ln in text.splitlines())

    def hr(self) -> str:
        return "---" if self.kind == "markdown" else "----"


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def _next_link(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    for part in link.split(","):
        m = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


class GitHubProvider:
    kind = "github"
    supports_inline = True
    can_close = True

    def __init__(self, cfg: dict, dotenv: Dict[str, str], label: str):
        repo = cfg.get("repo") or ""
        if "/" not in repo:
            raise ConfigError(f"{label}.repo は 'owner/repo' 形式で指定してください")
        self.owner, self.name = repo.split("/", 1)
        self.api = (cfg.get("api_url") or "https://api.github.com").rstrip("/")
        default_web = "https://github.com" if self.api == "https://api.github.com" \
            else re.sub(r"/api/v3$", "", self.api)
        self.web = (cfg.get("web_url") or default_web).rstrip("/")
        self.token = resolve_secret(cfg, "token", "token_env", "GITHUB_TOKEN", dotenv, label)
        self.http = Http({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"pr-migrate/{VERSION}",
        })
        self.fmt = Fmt("markdown")
        self._git_url = cfg.get("git_url")
        if self._git_url:
            SECRETS.extend(re.findall(r"//[^:/@]+:([^@]+)@", self._git_url))

    def describe(self) -> str:
        return f"GitHub {self.owner}/{self.name}"

    def _url(self, path: str = "") -> str:
        return f"{self.api}/repos/{self.owner}/{self.name}{path}"

    def _paged(self, path: str, params: Optional[dict] = None):
        url: Optional[str] = self._url(path)
        p = dict(params or {})
        p["per_page"] = 100
        first = True
        while url:
            data, headers = self.http.request("GET", url, params=p if first else None)
            first = False
            for item in data or []:
                yield item
            url = _next_link(headers.get("Link"))

    def git_url(self) -> str:
        if self._git_url:
            return self._git_url
        u = urllib.parse.urlparse(self.web)
        return f"{u.scheme}://x-access-token:{self.token}@{u.netloc}/{self.owner}/{self.name}.git"

    def check(self) -> str:
        data, _ = self.http.request("GET", self._url())
        perms = data.get("permissions") or {}
        return f"{data.get('full_name')} (push権限: {'あり' if perms.get('push') else 'なし'})"

    def _pr(self, p: dict) -> PR:
        state = "merged" if p.get("merged_at") else p.get("state", "open")
        head_repo = (p.get("head") or {}).get("repo") or {}
        base_repo = (p.get("base") or {}).get("repo") or {}
        is_fork = (not head_repo) or head_repo.get("full_name") != base_repo.get("full_name")
        return PR(
            number=p["number"], title=p.get("title") or "", body=p.get("body") or "",
            state=state, base=p["base"]["ref"], head=p["head"]["ref"],
            base_sha=p["base"].get("sha"), head_sha=p["head"].get("sha"),
            author=(p.get("user") or {}).get("login") or "ghost",
            author_id=(p.get("user") or {}).get("login"),
            created=p.get("created_at") or "", url=p.get("html_url") or "",
            draft=bool(p.get("draft")), merged_at=p.get("merged_at"), closed_at=p.get("closed_at"),
            assignee=(p.get("assignee") or {}).get("login"), is_fork=is_fork,
        )

    def list_prs(self, state: str = "all") -> List[PR]:
        api_state = {"open": "open", "closed": "closed", "merged": "closed", "all": "all"}[state]
        prs = [self._pr(p) for p in self._paged("/pulls", {"state": api_state, "sort": "created",
                                                            "direction": "asc"})]
        if state == "merged":
            prs = [p for p in prs if p.state == "merged"]
        elif state == "closed":
            prs = [p for p in prs if p.state in ("closed", "merged")]
        return sorted(prs, key=lambda p: p.number)

    def get_pr(self, number: int) -> PR:
        data, _ = self.http.request("GET", self._url(f"/pulls/{number}"))
        return self._pr(data)

    def list_comments(self, number: int) -> List[Comment]:
        out: List[Comment] = []
        for c in self._paged(f"/issues/{number}/comments"):
            out.append(Comment(id=f"ic{c['id']}", kind="issue",
                               author=(c.get("user") or {}).get("login") or "ghost",
                               created=c.get("created_at") or "", body=c.get("body") or "",
                               url=c.get("html_url")))
        for r in self._paged(f"/pulls/{number}/reviews"):
            st = r.get("state")
            if st == "PENDING":
                continue
            if not (r.get("body") or st in ("APPROVED", "CHANGES_REQUESTED")):
                continue  # インラインコメントのみのレビューは本体を省略
            out.append(Comment(id=f"rv{r['id']}", kind="review",
                               author=(r.get("user") or {}).get("login") or "ghost",
                               created=r.get("submitted_at") or "", body=r.get("body") or "",
                               url=r.get("html_url"), review_state=st))
        for c in self._paged(f"/pulls/{number}/comments"):
            out.append(Comment(
                id=f"rc{c['id']}", kind="review_comment",
                author=(c.get("user") or {}).get("login") or "ghost",
                created=c.get("created_at") or "", body=c.get("body") or "", url=c.get("html_url"),
                path=c.get("path"), line=c.get("line"), start_line=c.get("start_line"),
                side=c.get("side"), start_side=c.get("start_side"), commit_id=c.get("commit_id"),
                original_line=c.get("original_line"), original_commit_id=c.get("original_commit_id"),
                diff_hunk=c.get("diff_hunk"),
                in_reply_to=f"rc{c['in_reply_to_id']}" if c.get("in_reply_to_id") else None,
            ))
        out.sort(key=lambda x: (x.created, x.id))
        return out

    # ---- 書き込み系 ----
    def create_pr(self, title: str, body: str, base: str, head: str, draft: bool = False):
        payload = {"title": title, "body": body, "base": base, "head": head, "draft": draft}
        try:
            data, _ = self.http.request("POST", self._url("/pulls"), json_body=payload)
        except ApiError as e:
            if draft and e.status == 422 and "draft" in e.body.lower():
                payload["draft"] = False
                data, _ = self.http.request("POST", self._url("/pulls"), json_body=payload)
            else:
                raise
        return data["number"], data["html_url"]

    def find_open_pr(self, base: str, head: str):
        for p in self._paged("/pulls", {"state": "open", "head": f"{self.owner}:{head}", "base": base}):
            return p["number"], p["html_url"]
        return None

    def pr_web_url(self, number: int) -> str:
        return f"{self.web}/{self.owner}/{self.name}/pull/{number}"

    def update_pr(self, number: int, title: Optional[str] = None, body: Optional[str] = None) -> None:
        payload = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        self.http.request("PATCH", self._url(f"/pulls/{number}"), json_body=payload)

    def set_state(self, number: int, open_: bool) -> None:
        self.http.request("PATCH", self._url(f"/pulls/{number}"),
                          json_body={"state": "open" if open_ else "closed"})

    def add_comment(self, number: int, body: str) -> str:
        data, _ = self.http.request("POST", self._url(f"/issues/{number}/comments"),
                                    json_body={"body": body})
        return f"ic{data['id']}"

    def add_review_comment(self, number: int, c: Comment, body: str,
                           reply_to: Optional[str] = None) -> str:
        if reply_to:
            rid = reply_to[2:]
            data, _ = self.http.request("POST", self._url(f"/pulls/{number}/comments/{rid}/replies"),
                                        json_body={"body": body})
            return f"rc{data['id']}"
        if c.line is not None and c.commit_id:
            commit, line = c.commit_id, c.line
        else:
            commit, line = c.original_commit_id, c.original_line
        if not (commit and line and c.path):
            raise MigrateError("インラインコメントの位置情報が不足しています")
        payload: Dict[str, Any] = {"body": body, "commit_id": commit, "path": c.path,
                                   "line": line, "side": c.side or "RIGHT"}
        if c.start_line and c.start_line != line:
            payload["start_line"] = c.start_line
            payload["start_side"] = c.start_side or payload["side"]
        data, _ = self.http.request("POST", self._url(f"/pulls/{number}/comments"), json_body=payload)
        return f"rc{data['id']}"

    def update_comment(self, number: int, target_id: str, body: str) -> None:
        cid = target_id[2:]
        if target_id.startswith("rc"):
            self.http.request("PATCH", self._url(f"/pulls/comments/{cid}"), json_body={"body": body})
        else:
            self.http.request("PATCH", self._url(f"/issues/comments/{cid}"), json_body={"body": body})

    def assign(self, number: int, user: str) -> None:
        self.http.request("POST", self._url(f"/issues/{number}/assignees"),
                          json_body={"assignees": [user]})


# --------------------------------------------------------------------------- #
# Backlog Git
# --------------------------------------------------------------------------- #
class BacklogProvider:
    kind = "backlog"
    supports_inline = False   # Backlog API ではインラインコメントを作成できない
    can_close = False         # Backlog API では PR のクローズ/マージができない

    def __init__(self, cfg: dict, dotenv: Dict[str, str], label: str):
        self.space = (cfg.get("space_url") or "").rstrip("/")
        if not self.space.startswith("http"):
            raise ConfigError(f"{label}.space_url を指定してください（例: https://xxx.backlog.com）")
        self.project = cfg.get("project") or ""
        self.repo = cfg.get("repo") or ""
        if not self.project or not self.repo:
            raise ConfigError(f"{label}.project と {label}.repo を指定してください")
        self.key = resolve_secret(cfg, "api_key", "api_key_env", "BACKLOG_API_KEY", dotenv, label)
        self.http = Http({"User-Agent": f"pr-migrate/{VERSION}"}, base_params={"apiKey": self.key})
        self.fmt = Fmt(cfg.get("text_format", "markdown"))
        self._git_url = cfg.get("git_url")
        if self._git_url:
            SECRETS.extend(re.findall(r"//[^:/@]+:([^@]+)@", self._git_url))
        self._users: Optional[Dict[str, int]] = None

    def describe(self) -> str:
        return f"Backlog {self.space} {self.project}/{self.repo}"

    def _url(self, path: str = "") -> str:
        q = urllib.parse.quote
        return f"{self.space}/api/v2/projects/{q(self.project)}/git/repositories/{q(self.repo)}{path}"

    def pr_web_url(self, number: int) -> str:
        return f"{self.space}/git/{self.project}/{self.repo}/pullRequests/{number}"

    def git_url(self) -> str:
        return self._git_url or f"{self.space}/git/{self.project}/{self.repo}.git"

    def check(self) -> str:
        data, _ = self.http.request("GET", self._url())
        return f"{self.project}/{data.get('name')}"

    def _pr(self, p: dict) -> PR:
        sid = (p.get("status") or {}).get("id")
        state = {1: "open", 2: "closed", 3: "merged"}.get(sid, "open")
        cu = p.get("createdUser") or {}
        return PR(
            number=p["number"], title=p.get("summary") or "", body=p.get("description") or "",
            state=state, base=p.get("base") or "", head=p.get("branch") or "",
            base_sha=p.get("baseCommit"), head_sha=p.get("branchCommit"),
            author=cu.get("name") or cu.get("userId") or "unknown", author_id=cu.get("userId"),
            created=p.get("created") or "", url=self.pr_web_url(p["number"]),
            merged_at=p.get("mergeAt"), closed_at=p.get("closeAt"),
            assignee=(p.get("assignee") or {}).get("userId"),
            issue_key=(p.get("issue") or {}).get("issueKey"),
        )

    def list_prs(self, state: str = "all") -> List[PR]:
        status = {"open": [1], "closed": [2, 3], "merged": [3], "all": []}[state]
        items: List[dict] = []
        offset = 0
        while True:
            params = [("count", 100), ("offset", offset)] + [("statusId[]", s) for s in status]
            data, _ = self.http.request("GET", self._url("/pullRequests"), params=params)
            items.extend(data or [])
            if not data or len(data) < 100:
                break
            offset += 100
        return sorted((self._pr(p) for p in items), key=lambda p: p.number)

    def get_pr(self, number: int) -> PR:
        data, _ = self.http.request("GET", self._url(f"/pullRequests/{number}"))
        return self._pr(data)

    def list_comments(self, number: int) -> List[Comment]:
        out: List[Comment] = []
        seen = set()
        min_id = None
        while True:
            params: List[Tuple[str, Any]] = [("count", 100), ("order", "asc")]
            if min_id is not None:
                params.append(("minId", min_id))
            data, _ = self.http.request("GET", self._url(f"/pullRequests/{number}/comments"),
                                        params=params)
            data = data or []
            added = 0
            for c in data:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                added += 1
                content = (c.get("content") or "").strip()
                if not content:
                    continue  # 変更履歴のみ（状態変更など）のコメントは対象外
                cu = c.get("createdUser") or {}
                out.append(Comment(id=f"bc{c['id']}", kind="issue",
                                   author=cu.get("name") or cu.get("userId") or "unknown",
                                   created=c.get("created") or "", body=c.get("content") or "",
                                   url=f"{self.pr_web_url(number)}#comment-{c['id']}"))
            if len(data) < 100 or added == 0:
                break
            min_id = data[-1]["id"]
        out.sort(key=lambda x: (x.created, x.id))
        return out

    # ---- 書き込み系 ----
    def create_pr(self, title: str, body: str, base: str, head: str, draft: bool = False):
        form = {"summary": title, "description": body or "（説明なし）", "base": base, "branch": head}
        data, _ = self.http.request("POST", self._url("/pullRequests"), form=form)
        return data["number"], self.pr_web_url(data["number"])

    def find_open_pr(self, base: str, head: str):
        for p in self.list_prs("open"):
            if p.base == base and p.head == head:
                return p.number, p.url
        return None

    def update_pr(self, number: int, title: Optional[str] = None, body: Optional[str] = None) -> None:
        form = {}
        if title is not None:
            form["summary"] = title
        if body is not None:
            form["description"] = body or "（説明なし）"
        self.http.request("PATCH", self._url(f"/pullRequests/{number}"), form=form)

    def set_state(self, number: int, open_: bool) -> None:
        raise MigrateError("Backlog API では PR の状態を変更できません")

    def add_comment(self, number: int, body: str) -> str:
        data, _ = self.http.request("POST", self._url(f"/pullRequests/{number}/comments"),
                                    form={"content": body})
        return f"bc{data['id']}"

    def add_review_comment(self, number: int, c: Comment, body: str, reply_to=None) -> str:
        raise MigrateError("Backlog はインラインコメント非対応")

    def update_comment(self, number: int, target_id: str, body: str) -> None:
        self.http.request("PATCH", self._url(f"/pullRequests/{number}/comments/{target_id[2:]}"),
                          form={"content": body})

    def assign(self, number: int, user: str) -> None:
        if self._users is None:
            q = urllib.parse.quote
            data, _ = self.http.request("GET", f"{self.space}/api/v2/projects/{q(self.project)}/users")
            self._users = {u.get("userId"): u["id"] for u in data or [] if u.get("userId")}
            self._users.update({u.get("name"): u["id"] for u in data or [] if u.get("name")})
        uid = self._users.get(user)
        if uid is None:
            raise MigrateError(f"Backlog のプロジェクトにユーザー {user} が見つかりません")
        self.http.request("PATCH", self._url(f"/pullRequests/{number}"), form={"assigneeId": uid})


def make_provider(cfg: dict, dotenv: Dict[str, str], label: str):
    t = (cfg or {}).get("type")
    if t == "github":
        return GitHubProvider(cfg, dotenv, label)
    if t == "backlog":
        return BacklogProvider(cfg, dotenv, label)
    raise ConfigError(f"{label}.type は 'github' か 'backlog' を指定してください")


# --------------------------------------------------------------------------- #
# git（ブランチ・コミットの転送）
# --------------------------------------------------------------------------- #
class GitMirror:
    def __init__(self, path: Path, src_url: str, dst_url: str, dry_run: bool):
        self.path = path
        self.src_url = src_url
        self.dst_url = dst_url
        self.dry_run = dry_run
        self._fetched = False
        self._dst_heads: Optional[Dict[str, str]] = None

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", f"--git-dir={self.path}", *args]
        debug("$ " + " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise MigrateError(f"git {args[0]} 失敗: {mask(r.stderr.strip())}")
        return r

    def prepare(self) -> None:
        if not (self.path / "HEAD").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self._git("init", "--bare", "-q")
        if self._fetched:
            return
        log("移行元リポジトリを取得しています（初回は時間がかかります）...")
        self._git("fetch", "--prune", "--no-tags", "-q", self.src_url, "+refs/heads/*:refs/src/heads/*")
        # GitHub などは refs/pull/N/head でマージ・削除済みブランチのコミットも取得できる
        self._git("fetch", "--no-tags", "-q", self.src_url, "+refs/pull/*/head:refs/src/pull/*",
                  check=False)
        self._fetched = True

    def has_commit(self, sha: str) -> bool:
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def ensure_commit(self, sha: Optional[str]) -> bool:
        if not sha:
            return False
        if self.has_commit(sha):
            return True
        self._git("fetch", "--no-tags", "-q", self.src_url, sha, check=False)
        return self.has_commit(sha)

    def rev_parse(self, ref: str) -> Optional[str]:
        r = self._git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def target_heads(self) -> Dict[str, str]:
        if self._dst_heads is None:
            r = self._git("ls-remote", "--heads", self.dst_url)
            heads: Dict[str, str] = {}
            for line in r.stdout.splitlines():
                if "\t" in line:
                    sha, ref = line.split("\t", 1)
                    heads[ref[len("refs/heads/"):]] = sha
            self._dst_heads = heads
        return self._dst_heads

    def push(self, sha: str, branch: str, force: bool = False) -> bool:
        if self.dry_run:
            log(f"[dry-run] git push {sha[:10]} -> {branch}{' (force)' if force else ''}")
            self.target_heads()[branch] = sha
            return True
        spec = f"{'+' if force else ''}{sha}:refs/heads/{branch}"
        r = self._git("push", "-q", self.dst_url, spec, check=False)
        if r.returncode != 0:
            warn(f"git push 失敗 ({branch}): {r.stderr.strip()}")
            return False
        log(f"ブランチを転送しました: {branch} ({sha[:10]})")
        self.target_heads()[branch] = sha
        return True


# --------------------------------------------------------------------------- #
# 状態ファイル
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 1, "prs": {}}
        self.prs: Dict[str, dict] = self.data.setdefault("prs", {})

    def save(self) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# 移行処理本体
# --------------------------------------------------------------------------- #
DEFAULT_OPTIONS: Dict[str, Any] = {
    "push_branches": True,          # git でブランチ/コミットを移行先へ転送する
    "closed_pr_strategy": "snapshot",  # クローズ/マージ済み PR: snapshot | skip | as-is
    "branch_prefix": "pr-migrate/",  # スナップショット等で作るブランチの接頭辞
    "add_header": True,              # 本文先頭に移行元情報を付ける
    "title_prefix": "",
    "sync_description": True,        # タイトル/本文の変更を同期
    "sync_comments": True,
    "sync_comment_edits": True,      # 編集されたコメントを更新
    "inline_review_comments": True,  # GitHub->GitHub でインラインコメントとして再現
    "sync_state": True,              # クローズ/マージを移行先へ反映（GitHub 移行先のみ）
    "state_change_comment": True,    # 状態変化時に注記コメントを投稿
    "sync_assignee": False,
    "user_map": {},                  # 移行元ユーザー名 -> 移行先ユーザー名
    "timezone_offset_hours": 9,
    "write_interval_sec": 1.0,       # 書き込み API の間隔（レート制限対策）
    "work_dir": ".work",
    "state_file": None,
}

STATE_LABEL = {"open": "オープン", "closed": "クローズ（未マージ）", "merged": "マージ済み"}
REVIEW_LABEL = {
    "APPROVED": "が承認しました",
    "CHANGES_REQUESTED": "が変更をリクエストしました",
    "COMMENTED": "がレビューしました",
    "DISMISSED": "のレビュー（取り下げ済み）",
}


class Migrator:
    def __init__(self, src, dst, opts: dict, state: State, args, git: Optional[GitMirror]):
        self.src = src
        self.dst = dst
        self.o = opts
        self.state = state
        self.args = args
        self.git = git
        self._warned_close = False

    # ---- 共通 ----
    def _w(self, desc: str, fn, *a, fake: Any = "dry", **kw):
        """書き込み API 呼び出し（dry-run 時はログのみ）"""
        if self.args.dry_run:
            log(f"[dry-run] {desc}")
            return fake
        res = fn(*a, **kw)
        time.sleep(float(self.o["write_interval_sec"]))
        return res

    def user_label(self, name: str) -> str:
        return self.o["user_map"].get(name, name)

    # ---- ブランチ ----
    def target_branches(self, pr: PR) -> Tuple[str, str, bool]:
        prefix = self.o["branch_prefix"]
        snapshot = pr.state != "open" and self.o["closed_pr_strategy"] == "snapshot"
        heads = self.git.target_heads() if self.git else {}
        head = pr.head
        if pr.is_fork:
            head = f"{prefix}pr-{pr.number}/{pr.head}"
        elif pr.state != "open" and head in heads and pr.head_sha and heads[head] != pr.head_sha:
            head = f"{prefix}pr-{pr.number}/head"   # 同名ブランチが別内容で存在する
        base = f"{prefix}pr-{pr.number}/base" if snapshot else pr.base
        return base, head, snapshot

    def push_branches(self, pr: PR, base: str, head: str, snapshot: bool, creating: bool) -> None:
        g = self.git
        assert g is not None
        g.prepare()
        head_sha = pr.head_sha or g.rev_parse(f"refs/src/heads/{pr.head}")
        if not g.ensure_commit(head_sha):
            raise MigrateError(f"head のコミットを取得できません ({pr.head} {head_sha or ''})")
        heads = g.target_heads()

        if creating:
            if snapshot:
                base_sha = pr.base_sha
                if not g.ensure_commit(base_sha):
                    raise MigrateError(f"base のコミットを取得できません ({pr.base} {base_sha or ''})")
                if heads.get(base) != base_sha and not g.push(base_sha, base, force=True):
                    raise MigrateError(f"base ブランチ {base} を作成できません")
            elif base not in heads:
                base_sha = g.rev_parse(f"refs/src/heads/{pr.base}") or pr.base_sha
                if not g.ensure_commit(base_sha) or not g.push(base_sha, base):
                    raise MigrateError(f"移行先に base ブランチ {base} がなく、作成もできません")

        cur = heads.get(head)
        if cur == head_sha:
            return
        if not g.push(head_sha, head, force=bool(self.args.force_push)):
            if cur is None:
                raise MigrateError(f"head ブランチ {head} を作成できません")
            warn(f"#{pr.number}: head ブランチ {head} を更新できませんでした（履歴が異なる場合は --force-push）")

    # ---- 本文 ----
    def render_title(self, pr: PR) -> str:
        return f"{self.o['title_prefix']}{pr.title}"

    def render_body(self, pr: PR, base: str, head: str, snapshot: bool) -> str:
        body = pr.body or ""
        if not self.o["add_header"]:
            return truncate(body)
        f = self.dst.fmt
        lines = [
            f"{f.bold('移行元 PR:')} {f.link(f'#{pr.number}', pr.url)} （{self.src.describe()}）",
            f"- 作成者: {self.user_label(pr.author)} / 作成日時: {fmt_time(pr.created)}",
            f"- ブランチ: {f.code(pr.head)} → {f.code(pr.base)}",
        ]
        st = STATE_LABEL.get(pr.state, pr.state)
        when = pr.merged_at if pr.state == "merged" else pr.closed_at if pr.state == "closed" else None
        lines.append(f"- 状態: {st}" + (f"（{fmt_time(when)}）" if when else ""))
        if pr.issue_key:
            lines.append(f"- 関連課題: {pr.issue_key}")
        if snapshot:
            lines.append(f"- 差分再現のため、当時のベース {f.code(pr.base)} を {f.code(base)} として作成しています")
        if head != pr.head:
            lines.append(f"- head ブランチは {f.code(head)} として作成しています")
        return truncate("\n".join(lines) + f"\n\n{f.hr()}\n\n" + body)

    def render_comment(self, c: Comment, inline: bool) -> str:
        f = self.dst.fmt
        who = f.bold(self.user_label(c.author))
        when = fmt_time(c.created)
        if c.kind == "review":
            head = f"{who} {REVIEW_LABEL.get(c.review_state or '', 'のレビュー')}（{when}）"
        elif c.kind == "review_comment":
            head = f"{who} のレビューコメント（{when}）"
        else:
            head = f"{who} のコメント（{when}）"
        if c.url:
            head += f" {f.link('元コメント', c.url)}"
        parts = [f.quote(head)]
        if c.kind == "review_comment" and not inline:
            ln = c.line or c.original_line
            sl = c.start_line
            loc = f"{c.path}" + (f" L{sl}-{ln}" if sl and sl != ln else f" L{ln}" if ln else "")
            parts.append(f"ファイル: {f.code(loc)}" + ("（スレッドへの返信）" if c.in_reply_to else ""))
            if c.diff_hunk and not c.in_reply_to:
                hunk = "\n".join(c.diff_hunk.splitlines()[-8:])
                parts.append(f.code_block(hunk, "diff"))
        if c.body:
            parts.append(c.body)
        return truncate("\n\n".join(parts))

    # ---- 1 PR の処理 ----
    def process(self, pr: PR) -> str:
        key = str(pr.number)
        entry = self.state.prs.get(key)
        if entry is None:
            if self.args.command == "sync":
                return "skip"
            if pr.state != "open" and self.o["closed_pr_strategy"] == "skip":
                debug(f"#{pr.number}: {pr.state} のためスキップ")
                return "skip"
            entry = self.create(pr)
            result = "created"
        else:
            result = "synced"
            if self.git and pr.state == "open" and not self.args.no_push:
                self.push_branches(pr, entry["base_branch"], entry["head_branch"], False, creating=False)
            self.sync_meta(pr, entry)

        if self.o["sync_comments"] and not self.args.no_comments:
            self.sync_comments(pr, entry)
        self.sync_state(pr, entry)
        self.state.save()
        return result

    def create(self, pr: PR) -> dict:
        base, head, snapshot = self.target_branches(pr)
        log(f"#{pr.number} [{pr.state}] {pr.title}  ({head} -> {base})")
        if self.git and not self.args.no_push:
            self.push_branches(pr, base, head, snapshot, creating=True)

        title = self.render_title(pr)
        body = self.render_body(pr, base, head, snapshot)
        existing = None if self.args.dry_run and snapshot else self.dst.find_open_pr(base, head)
        if existing:
            number, url = existing
            log(f"  移行先に同じブランチの PR が既にあるため紐付けます: #{number}")
        else:
            number, url = self._w(
                f"PR 作成 '{title}' ({head} -> {base})", self.dst.create_pr, title, body, base, head,
                draft=pr.draft, fake=(None, "(dry-run)"))
            if number is not None:
                log(f"  PR を作成しました: #{number} {url}")

        entry = {
            "target_number": number, "target_url": url,
            "base_branch": base, "head_branch": head, "snapshot": snapshot,
            "meta_hash": sha1(pr.title + "\0" + pr.body),
            "src_state": "open", "dst_state": "open", "comments": {},
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if not self.args.dry_run:
            self.state.prs[str(pr.number)] = entry
            self.state.save()

        if self.o["sync_assignee"] and pr.assignee and number is not None:
            user = self.user_label(pr.assignee)
            try:
                self._w(f"担当者設定 {user}", self.dst.assign, number, user)
            except Exception as e:  # noqa: BLE001
                warn(f"#{pr.number}: 担当者を設定できませんでした: {e}")
        return entry

    def sync_meta(self, pr: PR, entry: dict) -> None:
        h = sha1(pr.title + "\0" + pr.body)
        if not self.o["sync_description"] or entry.get("meta_hash") == h:
            return
        body = self.render_body(pr, entry["base_branch"], entry["head_branch"], entry.get("snapshot", False))
        self._w(f"#{entry['target_number']} タイトル/本文を更新", self.dst.update_pr,
                entry["target_number"], title=self.render_title(pr), body=body)
        entry["meta_hash"] = h
        log(f"#{pr.number}: タイトル/本文の変更を反映しました")

    def sync_comments(self, pr: PR, entry: dict) -> None:
        number = entry["target_number"]
        cmap: Dict[str, dict] = entry.setdefault("comments", {})
        new = upd = 0
        for c in self.src.list_comments(pr.number):
            h = sha1((c.review_state or "") + "\0" + c.body)
            rec = cmap.get(c.id)
            if rec is None:
                tid = self.post_comment(number, c, cmap)
                cmap[c.id] = {"target_id": tid, "hash": h}
                new += 1
                if new % 20 == 0:
                    self.state.save()
            elif rec["hash"] != h and self.o["sync_comment_edits"] and rec.get("target_id") != "dry":
                body = self.render_comment(c, inline=rec["target_id"].startswith("rc"))
                self._w(f"コメント更新 #{number} {rec['target_id']}", self.dst.update_comment,
                        number, rec["target_id"], body)
                rec["hash"] = h
                upd += 1
        if new or upd:
            log(f"#{pr.number}: コメント 追加 {new} 件 / 更新 {upd} 件")

    def post_comment(self, number, c: Comment, cmap: Dict[str, dict]) -> str:
        want_inline = (c.kind == "review_comment" and self.dst.supports_inline
                       and self.src.kind == "github" and self.o["inline_review_comments"])
        if want_inline:
            parent = (cmap.get(c.in_reply_to) or {}).get("target_id") if c.in_reply_to else None
            if not c.in_reply_to or (parent and parent.startswith("rc")):
                body = self.render_comment(c, inline=True)
                try:
                    return self._w(f"インラインコメント投稿 #{number} {c.path}", self.dst.add_review_comment,
                                   number, c, body, reply_to=parent)
                except (ApiError, MigrateError) as e:
                    warn(f"インラインコメントとして投稿できないため通常コメントにします: {e}")
        body = self.render_comment(c, inline=False)
        return self._w(f"コメント投稿 #{number} ({c.kind} by {c.author})", self.dst.add_comment, number, body)

    def sync_state(self, pr: PR, entry: dict) -> None:
        entry["src_state"] = pr.state
        if not self.o["sync_state"]:
            return
        number = entry["target_number"]
        dst_state = entry.get("dst_state", "open")
        f = self.dst.fmt
        if pr.state in ("closed", "merged") and dst_state == "open":
            note = f"note_{pr.state}"
            if self.o["state_change_comment"] and not entry.get(note):
                when = pr.merged_at if pr.state == "merged" else pr.closed_at
                text = f.quote(f"移行元 PR は {STATE_LABEL[pr.state]} になりました（{fmt_time(when)}）")
                self._w(f"状態注記コメント #{number}", self.dst.add_comment, number, text)
                entry[note] = True
            if self.dst.can_close:
                self._w(f"PR クローズ #{number}", self.dst.set_state, number, False)
                entry["dst_state"] = "closed"
                log(f"#{pr.number}: 移行先 PR #{number} をクローズしました")
            elif not self._warned_close:
                warn("Backlog は API で PR をクローズできません。クローズ/マージ済み PR は手動でクローズしてください")
                self._warned_close = True
        elif pr.state == "open" and dst_state == "closed" and self.dst.can_close:
            self._w(f"PR 再オープン #{number}", self.dst.set_state, number, True)
            entry["dst_state"] = "open"
            entry.pop("note_closed", None)
            if self.o["state_change_comment"]:
                self._w(f"状態注記コメント #{number}", self.dst.add_comment, number,
                        f.quote("移行元 PR が再オープンされました"))
            log(f"#{pr.number}: 移行先 PR #{number} を再オープンしました")

    def run(self, prs: List[PR]) -> Counter:
        stats: Counter = Counter()
        for pr in prs:
            try:
                stats[self.process(pr)] += 1
            except Exception as e:  # noqa: BLE001
                stats["error"] += 1
                error(f"#{pr.number}: {e}")
                if DEBUG:
                    traceback.print_exc()
                self.state.save()
        return stats


# --------------------------------------------------------------------------- #
# 設定・CLI
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"設定ファイルが見つかりません: {path}（config.example.json をコピーして作成してください）")
    text = path.read_text(encoding="utf-8-sig")
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"設定ファイルの JSON が不正です: {e}") from None


def select_prs(src, args, state: State) -> List[PR]:
    if args.pr:
        return [src.get_pr(n) for n in args.pr]
    st = "all" if args.command == "sync" else args.state
    prs = src.list_prs(st)
    if args.command == "sync":
        prs = [p for p in prs if str(p.number) in state.prs]
    if args.since:
        since = parse_time(args.since if "T" in args.since else args.since + "T00:00:00+09:00")
        prs = [p for p in prs if (parse_time(p.created) or since) >= since]
    if args.limit:
        prs = prs[: args.limit]
    return prs


def cmd_check(src, dst, opts, git: Optional[GitMirror]) -> int:
    ok = True
    for label, p in (("移行元", src), ("移行先", dst)):
        try:
            log(f"{label}: {p.describe()} -> OK {p.check()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            error(f"{label}: {p.describe()} -> {e}")
    if git:
        for label, url in (("移行元 git", git.src_url), ("移行先 git", git.dst_url)):
            r = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0:
                log(f"{label}: OK（ブランチ {len(r.stdout.splitlines())} 件）")
            else:
                ok = False
                error(f"{label}: {mask(r.stderr.strip())}")
    return 0 if ok else 1


def cmd_list(src, args, state: State) -> int:
    prs = select_prs(src, args, state)
    print(f"{'#':>5}  {'状態':<6} {'移行先':<8} タイトル  (head -> base)")
    for p in prs:
        e = state.prs.get(str(p.number))
        mapped = f"#{e['target_number']}" if e else "-"
        print(f"{p.number:>5}  {p.state:<6} {mapped:<8} {p.title}  ({p.head} -> {p.base})")
    print(f"計 {len(prs)} 件（移行済み {sum(1 for p in prs if str(p.number) in state.prs)} 件）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.json", help="設定ファイル（既定: config.json）")
    common.add_argument("--debug", action="store_true", help="詳細ログ")

    sel = argparse.ArgumentParser(add_help=False)
    sel.add_argument("--pr", type=int, nargs="+", help="対象の PR 番号（複数可）")
    sel.add_argument("--state", choices=["open", "closed", "merged", "all"], default="all",
                     help="対象とする移行元 PR の状態（既定: all）")
    sel.add_argument("--since", help="この日付以降に作成された PR のみ（例: 2024-01-01）")
    sel.add_argument("--limit", type=int, help="処理する最大件数")

    run = argparse.ArgumentParser(add_help=False)
    run.add_argument("--dry-run", action="store_true", help="書き込みせずに実行内容だけ表示")
    run.add_argument("--no-push", action="store_true", help="git によるブランチ転送をしない")
    run.add_argument("--force-push", action="store_true", help="head ブランチを強制更新する")
    run.add_argument("--no-comments", action="store_true", help="コメントを同期しない")
    run.add_argument("--watch", type=int, metavar="SEC", help="SEC 秒ごとに繰り返し実行")

    ap = argparse.ArgumentParser(description="GitHub / Backlog Git 間の PR 移行・同期ツール")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check", parents=[common], help="API・git の接続確認")
    sub.add_parser("list", parents=[common, sel], help="移行元 PR の一覧と移行状況を表示")
    sub.add_parser("migrate", parents=[common, sel, run], help="未移行 PR を作成し、移行済み PR を同期")
    sub.add_parser("sync", parents=[common, sel, run], help="移行済み PR のみ同期（新規作成しない）")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    global DEBUG, _TZ_OFFSET
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    args = build_parser().parse_args(argv)
    DEBUG = args.debug
    for k, v in (("dry_run", False), ("no_push", False), ("force_push", False),
                 ("no_comments", False), ("watch", None)):
        if not hasattr(args, k):
            setattr(args, k, v)

    try:
        cfg_path = Path(args.config).resolve()
        cfg = load_config(cfg_path)
        base_dir = cfg_path.parent
        dotenv = load_dotenv([base_dir / ".env", Path(__file__).resolve().parent / ".env"])
        opts = dict(DEFAULT_OPTIONS)
        opts.update(cfg.get("options") or {})
        if opts["closed_pr_strategy"] not in ("snapshot", "skip", "as-is"):
            raise ConfigError("options.closed_pr_strategy は snapshot / skip / as-is のいずれかです")
        _TZ_OFFSET = opts["timezone_offset_hours"]

        src = make_provider(cfg.get("source"), dotenv, "source")
        dst = make_provider(cfg.get("target"), dotenv, "target")

        state_path = Path(opts["state_file"]) if opts["state_file"] else \
            base_dir / "state" / f"{cfg_path.stem}.state.json"
        if not state_path.is_absolute():
            state_path = base_dir / state_path
        state = State(state_path, dry_run=args.dry_run)
        pair = {"source": src.describe(), "target": dst.describe()}
        if state.data.get("pair") and state.data["pair"] != pair:
            raise ConfigError(f"状態ファイル {state_path} は別の移行元/移行先のものです: {state.data['pair']}")
        state.data["pair"] = pair

        git = None
        if opts["push_branches"] and args.command in ("check", "migrate", "sync") and not args.no_push:
            if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
                raise ConfigError("git コマンドが見つかりません（push_branches=false で無効化できます）")
            work = Path(opts["work_dir"])
            if not work.is_absolute():
                work = base_dir / work
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{src.describe()}__{dst.describe()}")[:120]
            git = GitMirror(work / f"{name}.git", src.git_url(), dst.git_url(), args.dry_run)
    except ConfigError as e:
        error(str(e))
        return 2

    if args.command == "check":
        return cmd_check(src, dst, opts, git)
    if args.command == "list":
        return cmd_list(src, args, state)

    log(f"{src.describe()}  ->  {dst.describe()}" + ("  [dry-run]" if args.dry_run else ""))
    migrator = Migrator(src, dst, opts, state, args, git)
    while True:
        try:
            prs = select_prs(src, args, state)
            log(f"対象 PR: {len(prs)} 件")
            stats = migrator.run(prs)
            log("完了: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())) if stats else "完了: 対象なし")
            code = 1 if stats.get("error") else 0
        except Exception as e:  # noqa: BLE001
            error(str(e))
            if DEBUG:
                traceback.print_exc()
            code = 1
        if not args.watch:
            return code
        if git:
            git._fetched = False
            git._dst_heads = None
        log(f"{args.watch} 秒後に再実行します（Ctrl+C で終了）")
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断しました")
        sys.exit(130)
