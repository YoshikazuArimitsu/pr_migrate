#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pr_migrate.py - GitHub / Backlog Git の 2 つのリポジトリ間で Pull Request を双方向に同期するツール

  リポジトリ a / b のどちらかで作られた PR を、もう片方に「ミラー PR」として作成し、
  以降は次の内容を同期します（対応関係は state ファイルで管理）。

    - ミラー PR のタイトル先頭に同期元リポジトリのラベルを付ける（例: "[BL] ログイン修正"）
    - タイトル・本文の変更（同期元 → ミラー）
    - コメントの追加・編集（双方向）
    - head ブランチのコミット（双方向。片側だけ進んだ場合にもう片方へ反映）
    - クローズ / マージ / 再オープン（双方向。片方が閉じたらもう片方も閉じる）

  対応する組み合わせ:  GitHub ⇄ GitHub / GitHub ⇄ Backlog / Backlog ⇄ Backlog
  ※ Backlog の API では PR をクローズできないため、Backlog 側はコメントで通知し手動クローズになります。

  使い方 (詳細は README.md):
    python pr_migrate.py check            接続確認
    python pr_migrate.py list             両リポジトリの PR と対応状況
    python pr_migrate.py sync             未同期 PR のミラー作成＋同期（migrate も同じ）
    python pr_migrate.py sync --no-create 既存の対応だけ同期

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

VERSION = "2.0.0"

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
        repo = (cfg.get("repo") or "").strip()
        # "https://github.com/owner/repo.git" や "owner/repo.git" のような指定も受け付ける
        repo = re.sub(r"^(https?://[^/]+/|git@[^:]+:)", "", repo)
        repo = re.sub(r"(\.git)?/*$", "", repo)
        if repo.count("/") != 1:
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
    """両リポジトリのブランチを refs/<a|b>/heads/* に取得し、相互に push する作業用 bare リポジトリ"""

    def __init__(self, path: Path, urls: Dict[str, str], dry_run: bool):
        self.path = path
        self.urls = urls
        self.dry_run = dry_run
        self._fetched = False
        self._heads: Dict[str, Dict[str, str]] = {}

    def reset(self) -> None:
        self._fetched = False
        self._heads = {}

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", f"--git-dir={self.path}", *args]
        debug("$ " + mask(" ".join(cmd)))
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
        log("git: 両リポジトリの最新を取得しています（初回は時間がかかります）...")
        for side, url in self.urls.items():
            self._git("fetch", "--prune", "--no-tags", "-q", url, f"+refs/heads/*:refs/{side}/heads/*")
            # GitHub は refs/pull/N/head でマージ・削除済みブランチやフォークのコミットも取得できる
            self._git("fetch", "--no-tags", "-q", url, f"+refs/pull/*/head:refs/{side}/pull/*", check=False)
        self._fetched = True
        self._heads = {}

    def heads(self, side: str) -> Dict[str, str]:
        """side のリモートにある現在のブランチ -> コミット"""
        if side not in self._heads:
            self.prepare()
            r = self._git("for-each-ref", "--format=%(objectname) %(refname)", f"refs/{side}/heads/")
            prefix = f"refs/{side}/heads/"
            heads: Dict[str, str] = {}
            for line in r.stdout.splitlines():
                sha, ref = line.split(" ", 1)
                heads[ref[len(prefix):]] = sha
            self._heads[side] = heads
        return self._heads[side]

    def has_commit(self, sha: str) -> bool:
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def ensure_commit(self, sha: Optional[str]) -> bool:
        if not sha:
            return False
        if self.has_commit(sha):
            return True
        for url in self.urls.values():
            self._git("fetch", "--no-tags", "-q", url, sha, check=False)
            if self.has_commit(sha):
                return True
        return False

    def is_ancestor(self, old: str, new: str) -> bool:
        if not (self.has_commit(old) and self.has_commit(new)):
            return False
        return self._git("merge-base", "--is-ancestor", old, new, check=False).returncode == 0

    def push(self, side: str, sha: str, branch: str, force: bool = False) -> bool:
        if self.dry_run:
            log(f"[dry-run] git push {side}: {sha[:10]} -> {branch}{' (force)' if force else ''}")
            self.heads(side)[branch] = sha
            return True
        spec = f"{'+' if force else ''}{sha}:refs/heads/{branch}"
        r = self._git("push", "-q", self.urls[side], spec, check=False)
        if r.returncode != 0:
            warn(f"git push 失敗 ({side}:{branch}): {mask(r.stderr.strip())}")
            return False
        log(f"ブランチを転送しました: {side}:{branch} ({sha[:10]})")
        self.heads(side)[branch] = sha
        return True


# --------------------------------------------------------------------------- #
# 状態ファイル
# --------------------------------------------------------------------------- #
class State:
    """
    links: { "<同期元 a|b>#<番号>": {
        "origin": "a",
        "a": {"number": 1, "url": ..., "state": "open"},
        "b": {"number": 5, "url": ..., "state": "open", "base": ..., "head": ...},   # ミラー側はブランチ名も保持
        "snapshot": false, "meta_hash": ..., "head_sha": ...,
        "comments": { "a:ic123": {"copy": "b:bc456", "hash": ...}, ... } } }
    """

    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 2, "links": {}}
        if self.data.get("version", 1) < 2:
            self._upgrade_v1()
        self.links: Dict[str, dict] = self.data.setdefault("links", {})

    def _upgrade_v1(self) -> None:
        """片方向移行版 (v1) の状態ファイルを変換。source -> a, target -> b"""
        old = self.data
        links: Dict[str, dict] = {}
        for num, e in (old.get("prs") or {}).items():
            if e.get("target_number") is None:
                continue
            links[f"a#{num}"] = {
                "origin": "a",
                "a": {"number": int(num), "url": None, "state": e.get("src_state", "open")},
                "b": {"number": e["target_number"], "url": e.get("target_url"),
                      "state": e.get("dst_state", "open"),
                      "base": e.get("base_branch"), "head": e.get("head_branch")},
                "snapshot": e.get("snapshot", False),
                "meta_hash": None,   # 次回、ラベル付きタイトルに更新させる
                "head_sha": None,
                "comments": {f"a:{cid}": {"copy": f"b:{r['target_id']}", "hash": r.get("hash")}
                             for cid, r in (e.get("comments") or {}).items()
                             if r.get("target_id") and r.get("target_id") != "dry"},
                "created_at": e.get("created_at"),
            }
        pair = old.get("pair") or {}
        self.data = {"version": 2, "links": links,
                     "pair": {"a": pair.get("source"), "b": pair.get("target")} if pair else None,
                     "upgraded_from_v1": True}
        log(f"旧形式の状態ファイルを変換しました（対応 {len(links)} 件。移行元 = a, 移行先 = b）")

    def find(self, side: str, number: int) -> Optional[Tuple[str, dict]]:
        for k, e in self.links.items():
            if (e.get(side) or {}).get("number") == number:
                return k, e
        return None

    def save(self) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# 同期処理本体
# --------------------------------------------------------------------------- #
DEFAULT_OPTIONS: Dict[str, Any] = {
    "directions": "both",            # ミラーを新規作成する向き: both | a_to_b | b_to_a
    "title_label_format": "[{label}] ",  # ミラー PR のタイトル先頭に付ける文字列
    "push_branches": True,           # git でブランチ/コミットを転送する
    "sync_branches_both_ways": True,  # ミラー側の head ブランチに push されたコミットを同期元へ戻す
    "closed_pr_strategy": "skip",    # 既にクローズ/マージ済みの PR: skip | snapshot | as-is
    "branch_prefix": "pr-sync/",     # スナップショット等で作るブランチの接頭辞
    "add_header": True,              # ミラー PR 本文の先頭に同期元情報を付ける
    "sync_description": True,        # タイトル/本文の変更を同期元 -> ミラーに反映
    "sync_comments": True,
    "sync_comment_edits": True,      # 編集されたコメントを更新
    "inline_review_comments": True,  # GitHub ⇄ GitHub でインラインコメントとして再現
    "sync_state": True,              # クローズ/マージ/再オープンを双方向に反映
    "state_change_comment": True,    # 状態変化時に注記コメントを投稿
    "sync_assignee": False,
    "user_map": {},                  # ユーザー名 -> もう片方でのユーザー名（両方向まとめて）
    "timezone_offset_hours": 9,
    "write_interval_sec": 1.0,       # 書き込み API の間隔（レート制限対策）
    "work_dir": ".work",
    "state_file": None,
}

# ツールが書いた本文・コメントの先頭に付ける印（エコー防止。BMP 内の文字なので Backlog でも使える）
MARK = "⇄"
STATE_LABEL = {"open": "オープン", "closed": "クローズ（未マージ）", "merged": "マージ済み"}
REVIEW_LABEL = {
    "APPROVED": "が承認しました",
    "CHANGES_REQUESTED": "が変更をリクエストしました",
    "COMMENTED": "がレビューしました",
    "DISMISSED": "のレビュー（取り下げ済み）",
}
DEFAULT_LABEL = {"github": "GH", "backlog": "BL"}


class Side:
    def __init__(self, name: str, provider, label: str):
        self.name = name
        self.p = provider
        self.label = label
        self.other: "Side" = self  # 後で設定

    def tag(self, number: Any) -> str:
        return f"{self.label}#{number}"

    @property
    def branch_label(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", self.label).strip("-") or self.name


def _strip(side: str, cid: str) -> str:
    return cid.split(":", 1)[1] if cid.startswith(side + ":") else cid


class Syncer:
    def __init__(self, a: Side, b: Side, opts: dict, state: State, args, git: Optional[GitMirror]):
        self.sides = {"a": a, "b": b}
        self.o = opts
        self.state = state
        self.args = args
        self.git = git
        self._warned: set = set()

    # ---- 共通 ----
    def _w(self, desc: str, fn, *a, fake: Any = "dry", **kw):
        """書き込み API 呼び出し（dry-run 時はログのみ）"""
        if self.args.dry_run:
            log(f"[dry-run] {desc}")
            return fake
        res = fn(*a, **kw)
        time.sleep(float(self.o["write_interval_sec"]))
        return res

    def warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            warn(msg)

    def user_label(self, name: str) -> str:
        return self.o["user_map"].get(name, name)

    def label_prefix(self, side: Side) -> str:
        return self.o["title_label_format"].format(label=side.label)

    def looks_like_mirror(self, side: Side, pr: PR) -> bool:
        """対応表に無くても、ツールが作ったミラー PR と思われるものは同期元として扱わない"""
        return pr.title.startswith(self.label_prefix(side.other)) or pr.body.lstrip().startswith(MARK)

    # ---- 本文 ----
    def render_title(self, origin: Side, pr: PR) -> str:
        return f"{self.label_prefix(origin)}{pr.title}"

    def render_body(self, origin: Side, mirror: Side, pr: PR, m: dict, snapshot: bool) -> str:
        body = pr.body or ""
        if not self.o["add_header"]:
            return truncate(body)
        f = mirror.p.fmt
        lines = [
            f"{MARK} {f.bold('同期元 PR:')} {f.link(origin.tag(pr.number), pr.url)} （{origin.p.describe()}）",
            f"- 作成者: {self.user_label(pr.author)} / 作成日時: {fmt_time(pr.created)}",
            f"- ブランチ: {f.code(pr.head)} → {f.code(pr.base)}",
            "- この PR はツールで同期しています。コメントとクローズは双方向に反映されます。"
            "タイトル・本文は同期元で編集してください。",
        ]
        if pr.issue_key:
            lines.append(f"- 関連課題: {pr.issue_key}")
        if snapshot:
            lines.append(f"- 差分再現のため、当時のベース {f.code(pr.base)} を {f.code(m['base'])} として作成しています")
        if m.get("head") and m["head"] != pr.head:
            lines.append(f"- head ブランチは {f.code(m['head'])} として作成しています")
        return truncate("\n".join(lines) + f"\n\n{f.hr()}\n\n" + body)

    def render_comment(self, src: Side, dst: Side, c: Comment, inline: bool) -> str:
        f = dst.p.fmt
        who = f.bold(self.user_label(c.author))
        when = fmt_time(c.created)
        if c.kind == "review":
            head = f"{who} {REVIEW_LABEL.get(c.review_state or '', 'のレビュー')}（{when}）"
        elif c.kind == "review_comment":
            head = f"{who} のレビューコメント（{when}）"
        else:
            head = f"{who} のコメント（{when}）"
        head = f"{MARK} [{src.label}] {head}"
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

    def note(self, side: Side, number: int, text: str) -> None:
        if self.o["state_change_comment"]:
            self._w(f"注記コメント {side.tag(number)}", side.p.add_comment, number,
                    side.p.fmt.quote(f"{MARK} {text}"))

    # ---- ブランチ ----
    def mirror_branches(self, origin: Side, mirror: Side, pr: PR) -> Tuple[str, str, bool]:
        prefix = f"{self.o['branch_prefix']}{origin.branch_label}-{pr.number}/"
        snapshot = pr.state != "open" and self.o["closed_pr_strategy"] == "snapshot"
        heads = self.git.heads(mirror.name) if self.git else {}
        head = pr.head
        if pr.is_fork:
            head = f"{prefix}{pr.head}"
        elif head in heads and pr.head_sha and heads[head] != pr.head_sha:
            if pr.state != "open" or (self.git and not self.git.is_ancestor(heads[head], pr.head_sha)):
                head = f"{prefix}head"   # 同名ブランチが別内容で存在する
        base = f"{prefix}base" if snapshot else pr.base
        return base, head, snapshot

    def push_new(self, origin: Side, mirror: Side, pr: PR, base: str, head: str, snapshot: bool) -> str:
        g = self.git
        assert g is not None
        head_sha = g.heads(origin.name).get(pr.head) if not pr.is_fork and pr.state == "open" else None
        head_sha = head_sha or pr.head_sha
        if not g.ensure_commit(head_sha):
            raise MigrateError(f"head のコミットを取得できません ({pr.head} {head_sha or ''})")
        mh = g.heads(mirror.name)
        if snapshot:
            if not g.ensure_commit(pr.base_sha):
                raise MigrateError(f"base のコミットを取得できません ({pr.base} {pr.base_sha or ''})")
            if mh.get(base) != pr.base_sha and not g.push(mirror.name, pr.base_sha, base, force=True):
                raise MigrateError(f"base ブランチ {base} を作成できません")
        elif base not in mh:
            base_sha = g.heads(origin.name).get(pr.base) or pr.base_sha
            if not g.ensure_commit(base_sha) or not g.push(mirror.name, base_sha, base):
                raise MigrateError(f"{mirror.label} に base ブランチ {base} がなく、作成もできません")
        cur = mh.get(head)
        if cur != head_sha and not g.push(mirror.name, head_sha, head, force=bool(self.args.force_push)):
            if cur is None:
                raise MigrateError(f"head ブランチ {head} を作成できません")
            warn(f"{origin.tag(pr.number)}: head ブランチ {head} を更新できませんでした（--force-push で強制更新）")
        return head_sha

    def sync_branches(self, e: dict, origin: Side, mirror: Side, op: PR) -> None:
        """head ブランチの双方向同期。前回同期したコミットから片側だけ進んでいればそちらに合わせる"""
        g = self.git
        assert g is not None
        m = e[mirror.name]
        mb = m.get("head") or op.head
        o_branch = None if op.is_fork else op.head
        oh = (g.heads(origin.name).get(o_branch) if o_branch else None) or op.head_sha
        mh = g.heads(mirror.name).get(mb)
        last = e.get("head_sha")
        if not oh or oh == mh:
            e["head_sha"] = oh or last
            return
        g.ensure_commit(oh)
        if mh:
            g.ensure_commit(mh)
        forward = mh is None or mh == last or (last is None and g.is_ancestor(mh, oh))
        backward = (not forward and o_branch is not None and o_branch in g.heads(origin.name)
                    and self.o["sync_branches_both_ways"]
                    and (oh == last or (last is None and g.is_ancestor(oh, mh))))
        if forward or (self.args.force_push and not backward):
            if g.push(mirror.name, oh, mb, force=(mh == last) or bool(self.args.force_push)):
                e["head_sha"] = oh
        elif backward:
            log(f"{mirror.tag(m['number'])} の head に追加されたコミットを {origin.tag(op.number)} に反映します")
            if g.push(origin.name, mh, o_branch, force=(oh == last)):
                e["head_sha"] = mh
        else:
            self.warn_once(f"conflict:{origin.name}#{op.number}",
                           f"{origin.tag(op.number)}: {origin.label} と {mirror.label} の両方の head ブランチに"
                           f"別々のコミットがあるため同期できません（{origin.label} 側を優先するなら --force-push）")

    # ---- 新規作成 ----
    def create(self, origin: Side, mirror: Side, pr: PR) -> Any:
        """ミラー PR を作成して対応表の項目を返す。作らなかった場合は None、dry-run では文字列 'dry'"""
        if pr.state != "open" and not mirror.p.can_close and self.o["closed_pr_strategy"] != "skip":
            self.warn_once(f"noclose:{mirror.name}",
                           f"{mirror.label} は API で PR をクローズできないため、クローズ/マージ済みの PR は"
                           f"{mirror.label} にミラーを作成しません")
            return None
        base, head, snapshot = self.mirror_branches(origin, mirror, pr)
        log(f"{origin.tag(pr.number)} [{pr.state}] {pr.title}  → {mirror.label} ({head} -> {base})")
        head_sha = pr.head_sha
        if self.git and not self.args.no_push:
            head_sha = self.push_new(origin, mirror, pr, base, head, snapshot)

        m = {"base": base, "head": head}
        title = self.render_title(origin, pr)
        body = self.render_body(origin, mirror, pr, m, snapshot)
        existing = None if self.args.dry_run else mirror.p.find_open_pr(base, head)
        if existing:
            number, url = existing
            log(f"  {mirror.label} に同じブランチの PR が既にあるため紐付けます: {mirror.tag(number)}")
            self._w(f"{mirror.tag(number)} タイトル/本文を更新", mirror.p.update_pr, number, title=title, body=body)
        else:
            number, url = self._w(
                f"{mirror.label} に PR 作成 '{title}' ({head} -> {base})", mirror.p.create_pr,
                title, body, base, head, draft=pr.draft, fake=(None, "(dry-run)"))
            if number is not None:
                log(f"  PR を作成しました: {mirror.tag(number)} {url}")
        if number is None:
            return "dry"

        e = {
            "origin": origin.name,
            origin.name: {"number": pr.number, "url": pr.url, "state": "open"},
            mirror.name: {"number": number, "url": url, "state": "open", **m},
            "snapshot": snapshot, "meta_hash": sha1(title + "\0" + body), "head_sha": head_sha,
            "comments": {}, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.state.links[f"{origin.name}#{pr.number}"] = e
        self.state.save()

        if self.o["sync_assignee"] and pr.assignee:
            user = self.user_label(pr.assignee)
            try:
                self._w(f"担当者設定 {user}", mirror.p.assign, number, user)
            except Exception as ex:  # noqa: BLE001
                warn(f"{origin.tag(pr.number)}: 担当者を設定できませんでした: {ex}")
        return e

    # ---- 既存の対応の同期 ----
    def sync_link(self, e: dict, origin: Side, mirror: Side, op: PR, mp: PR) -> None:
        if (self.git and not self.args.no_push and op.state == "open" and mp.state == "open"):
            self.sync_branches(e, origin, mirror, op)
        self.sync_meta(e, origin, mirror, op)
        if self.o["sync_comments"] and not self.args.no_comments:
            self.sync_comments(e, origin, mirror, op, mp)
        self.sync_state(e, origin, mirror, op, mp)
        e[origin.name]["url"] = op.url
        e[mirror.name]["url"] = mp.url
        self.state.save()

    def sync_meta(self, e: dict, origin: Side, mirror: Side, op: PR) -> None:
        if not self.o["sync_description"]:
            return
        m = e[mirror.name]
        title = self.render_title(origin, op)
        body = self.render_body(origin, mirror, op, m, e.get("snapshot", False))
        h = sha1(title + "\0" + body)
        if e.get("meta_hash") == h:
            return
        self._w(f"{mirror.tag(m['number'])} タイトル/本文を更新", mirror.p.update_pr, m["number"],
                title=title, body=body)
        e["meta_hash"] = h
        log(f"{origin.tag(op.number)}: タイトル/本文を {mirror.tag(m['number'])} に反映しました")

    def sync_comments(self, e: dict, origin: Side, mirror: Side, op: PR, mp: PR) -> None:
        cmap: Dict[str, dict] = e.setdefault("comments", {})
        copies = {r["copy"]: k for k, r in cmap.items() if r.get("copy")}
        for src, dst, sp, dp in ((origin, mirror, op, mp), (mirror, origin, mp, op)):
            new = upd = 0
            for c in src.p.list_comments(sp.number):
                cid = f"{src.name}:{c.id}"
                if cid in copies:
                    continue    # ツールが投稿したコピー
                h = sha1((c.review_state or "") + "\0" + c.body)
                rec = cmap.get(cid)
                if rec is None:
                    if c.body.lstrip().lstrip(">").lstrip().startswith(MARK):
                        continue    # 対応表に無いがツールが投稿したもの（エコー防止）
                    tid = self.post_comment(src, dst, dp.number, c, cmap, copies)
                    if self.args.dry_run or not tid:
                        continue
                    copy = f"{dst.name}:{tid}"
                    cmap[cid] = {"copy": copy, "hash": h}
                    copies[copy] = cid
                    new += 1
                    self.state.save()
                elif rec["hash"] != h and self.o["sync_comment_edits"]:
                    tid = _strip(dst.name, rec["copy"])
                    body = self.render_comment(src, dst, c, inline=tid.startswith("rc"))
                    self._w(f"コメント更新 {dst.tag(dp.number)} {tid}", dst.p.update_comment, dp.number, tid, body)
                    rec["hash"] = h
                    upd += 1
            if new or upd:
                log(f"{src.tag(sp.number)} → {dst.tag(dp.number)}: コメント 追加 {new} 件 / 更新 {upd} 件")

    def post_comment(self, src: Side, dst: Side, number: int, c: Comment,
                     cmap: Dict[str, dict], copies: Dict[str, str]) -> Optional[str]:
        want_inline = (c.kind == "review_comment" and dst.p.supports_inline
                       and src.p.kind == "github" and self.o["inline_review_comments"])
        if want_inline:
            parent = None
            if c.in_reply_to:
                pk = f"{src.name}:{c.in_reply_to}"
                if pk in cmap:
                    parent = _strip(dst.name, cmap[pk]["copy"])   # 元コメントのコピーへの返信
                elif pk in copies:
                    parent = _strip(dst.name, copies[pk])         # コピーへの返信 → 元コメントへの返信
            if not c.in_reply_to or (parent and parent.startswith("rc")):
                body = self.render_comment(src, dst, c, inline=True)
                try:
                    return self._w(f"インラインコメント投稿 {dst.tag(number)} {c.path}",
                                   dst.p.add_review_comment, number, c, body, reply_to=parent)
                except (ApiError, MigrateError) as ex:
                    warn(f"インラインコメントとして投稿できないため通常コメントにします: {ex}")
        body = self.render_comment(src, dst, c, inline=False)
        return self._w(f"コメント投稿 {dst.tag(number)} ({c.kind} by {c.author})",
                       dst.p.add_comment, number, body)

    def sync_state(self, e: dict, origin: Side, mirror: Side, op: PR, mp: PR) -> None:
        if self.o["sync_state"]:
            for x, y, px, py in ((origin, mirror, op, mp), (mirror, origin, mp, op)):
                prev = e[x.name].get("state", "open")
                if prev == "open" and px.state != "open" and py.state == "open":
                    self.close_other(e, x, y, px, py)
                elif prev != "open" and px.state == "open" and py.state != "open":
                    self.reopen_other(e, x, y, px, py)
        for s, p in ((origin, op), (mirror, mp)):
            e[s.name]["state"] = p.state
            if p.state != "open":
                e[s.name].pop("needs_manual_close", None)

    def close_other(self, e: dict, x: Side, y: Side, px: PR, py: PR) -> None:
        when = px.merged_at if px.state == "merged" else px.closed_at
        what = f"{x.tag(px.number)} が {STATE_LABEL[px.state]} になりました（{fmt_time(when)}）"
        if y.p.can_close:
            self.note(y, py.number, what + "。こちらもクローズします。")
            self._w(f"PR クローズ {y.tag(py.number)}", y.p.set_state, py.number, False)
            py.state = "closed"
            e[y.name]["closed_by_sync"] = True
            log(f"{x.tag(px.number)} の{STATE_LABEL[px.state]}を受けて {y.tag(py.number)} をクローズしました")
        else:
            self.note(y, py.number, what + "。この PR は手動でクローズしてください"
                                           f"（{y.label} の API ではクローズできません）。")
            e[y.name]["needs_manual_close"] = True
            warn(f"{x.tag(px.number)} が{STATE_LABEL[px.state]}になりました。"
                 f"{y.tag(py.number)} は手動でクローズしてください {y.p.pr_web_url(py.number)}")

    def reopen_other(self, e: dict, x: Side, y: Side, px: PR, py: PR) -> None:
        if py.state == "merged" or not y.p.can_close or not e[y.name].get("closed_by_sync"):
            return
        self.note(y, py.number, f"{x.tag(px.number)} が再オープンされたため、こちらも再オープンします。")
        self._w(f"PR 再オープン {y.tag(py.number)}", y.p.set_state, py.number, True)
        py.state = "open"
        e[y.name].pop("closed_by_sync", None)
        log(f"{x.tag(px.number)} の再オープンを受けて {y.tag(py.number)} を再オープンしました")

    # ---- 全体 ----
    def run(self, sel: "Selection") -> Counter:
        stats: Counter = Counter()
        if self.git and not self.args.no_push:
            self.git.prepare()
        prs = {n: {p.number: p for p in s.p.list_prs("all")} for n, s in self.sides.items()}

        def get(side: Side, number: int) -> PR:
            p = prs[side.name].get(number)
            if p is None:
                p = side.p.get_pr(number)
                prs[side.name][number] = p
            return p

        # 1) 既存の対応を同期
        for key, e in list(self.state.links.items()):
            origin = self.sides[e["origin"]]
            mirror = origin.other
            on, mn = e[origin.name]["number"], e[mirror.name]["number"]
            if not sel.match_link(on, mn, origin, mirror):
                continue
            try:
                op, mp = get(origin, on), get(mirror, mn)
                self.sync_link(e, origin, mirror, op, mp)
                stats["synced"] += 1
            except Exception as ex:  # noqa: BLE001
                stats["error"] += 1
                error(f"{origin.tag(on)} ⇄ {mirror.tag(mn)}: {ex}")
                if DEBUG:
                    traceback.print_exc()
                self.state.save()

        # 2) 未同期の PR からミラーを作成
        if self.args.no_create:
            return stats
        linked = {(s, e[s]["number"]) for e in self.state.links.values() for s in ("a", "b")}
        candidates: List[Tuple[Side, PR]] = []
        for n, side in self.sides.items():
            if self.o["directions"] not in ("both", f"{n}_to_{side.other.name}"):
                continue
            for pr in sorted(prs[n].values(), key=lambda p: (p.created, p.number)):
                if (n, pr.number) in linked or not sel.match_pr(side, pr):
                    continue
                if self.looks_like_mirror(side, pr):
                    self.warn_once(f"orphan:{n}#{pr.number}",
                                   f"{side.tag(pr.number)} はミラー PR のようですが対応表にありません。スキップします")
                    stats["skip"] += 1
                    continue
                if pr.state != "open" and self.o["closed_pr_strategy"] == "skip":
                    continue
                candidates.append((side, pr))
        candidates.sort(key=lambda t: t[1].created)
        if sel.limit:
            candidates = candidates[: sel.limit]
        for side, pr in candidates:
            try:
                e = self.create(side, side.other, pr)
                if not isinstance(e, dict):
                    stats["skip" if e is None else "created(dry-run)"] += 1
                    continue
                stats["created"] += 1
                mp = side.other.p.get_pr(e[side.other.name]["number"]) if not self.args.dry_run else None
                if mp is not None:
                    self.sync_link(e, side, side.other, pr, mp)
            except Exception as ex:  # noqa: BLE001
                stats["error"] += 1
                error(f"{side.tag(pr.number)}: {ex}")
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


class Selection:
    """--pr / --state / --since / --limit による絞り込み"""

    def __init__(self, args, sides: Dict[str, Side]):
        self.state = getattr(args, "state", "all")
        self.limit = getattr(args, "limit", None)
        self.since = None
        if getattr(args, "since", None):
            s = args.since
            self.since = parse_time(s if "T" in s else s + "T00:00:00+09:00")
        self.prs: Optional[set] = None
        if getattr(args, "pr", None):
            self.prs = set()
            names = {s.label.lower(): s.name for s in sides.values()}
            names.update({"a": "a", "b": "b"})
            for tok in args.pr:
                m = re.fullmatch(r"\s*([^:#\s]+)\s*[:#]\s*(\d+)\s*", tok)
                if not m or m.group(1).lower() not in names:
                    raise ConfigError(f"--pr は <ラベル>:<番号> の形式で指定してください（例: "
                                      f"{sides['a'].label}:12）: {tok}")
                self.prs.add((names[m.group(1).lower()], int(m.group(2))))

    def _state_ok(self, st: str) -> bool:
        return self.state == "all" or st == self.state or (self.state == "closed" and st == "merged")

    def match_pr(self, side: Side, pr: PR) -> bool:
        if self.prs is not None and (side.name, pr.number) not in self.prs:
            return False
        if not self._state_ok(pr.state):
            return False
        if self.since and (parse_time(pr.created) or self.since) < self.since:
            return False
        return True

    def match_link(self, on: int, mn: int, origin: Side, mirror: Side) -> bool:
        if self.prs is None:
            return True
        return (origin.name, on) in self.prs or (mirror.name, mn) in self.prs


def cmd_check(sides: Dict[str, Side], git: Optional[GitMirror]) -> int:
    ok = True
    for s in sides.values():
        try:
            log(f"{s.name} [{s.label}]: {s.p.describe()} -> OK {s.p.check()}"
                + ("" if s.p.can_close else "（API でのクローズ不可）"))
        except Exception as e:  # noqa: BLE001
            ok = False
            error(f"{s.name} [{s.label}]: {s.p.describe()} -> {e}")
    if git:
        for n, url in git.urls.items():
            r = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0:
                log(f"{n} [{sides[n].label}] git: OK（ブランチ {len(r.stdout.splitlines())} 件）")
            else:
                ok = False
                error(f"{n} [{sides[n].label}] git: {mask(r.stderr.strip())}")
    return 0 if ok else 1


def cmd_list(sides: Dict[str, Side], syncer: Syncer, sel: Selection, state: State) -> int:
    prs = {n: {p.number: p for p in s.p.list_prs("all")} for n, s in sides.items()}
    a, b = sides["a"], sides["b"]
    print(f"== 同期中の PR（{len(state.links)} 件）")
    linked = set()
    for e in sorted(state.links.values(), key=lambda e: e.get("created_at") or ""):
        o = sides[e["origin"]]
        pa, pb = prs["a"].get(e["a"]["number"]), prs["b"].get(e["b"]["number"])
        linked.update({("a", e["a"]["number"]), ("b", e["b"]["number"])})
        if not sel.match_link(e[o.name]["number"], e[o.other.name]["number"], o, o.other):
            continue
        sa = pa.state if pa else e["a"].get("state", "?")
        sb = pb.state if pb else e["b"].get("state", "?")
        title = (prs[o.name].get(e[o.name]["number"]) or PR(0, "", "", "", "", "", None, None, "", "", "")).title
        flag = "  ※要手動クローズ" if any(e[s].get("needs_manual_close") for s in ("a", "b")) else ""
        print(f"  {a.tag(e['a']['number']):>8} {sa:<6} ⇄ {b.tag(e['b']['number']):>8} {sb:<6} "
              f"(同期元 {o.label}) {title}{flag}")
    for n, s in sides.items():
        rows = [p for p in prs[n].values() if (n, p.number) not in linked and sel.match_pr(s, p)]
        print(f"== {s.label} の未同期 PR（{len(rows)} 件）")
        for p in sorted(rows, key=lambda p: p.number):
            mark = "  ※ミラーらしいが対応表に無い" if syncer.looks_like_mirror(s, p) else ""
            print(f"  {s.tag(p.number):>8} {p.state:<6} {p.title}  ({p.head} -> {p.base}){mark}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.json", help="設定ファイル（既定: config.json）")
    common.add_argument("--debug", action="store_true", help="詳細ログ")

    sel = argparse.ArgumentParser(add_help=False)
    sel.add_argument("--pr", nargs="+", metavar="LABEL:NUM",
                     help="対象の PR（例: BL:12 GH:3。a:12 / b:3 も可）")
    sel.add_argument("--state", choices=["open", "closed", "merged", "all"], default="all",
                     help="新規ミラー作成の対象とする PR の状態（既定: all）")
    sel.add_argument("--since", help="この日付以降に作成された PR のみ新規作成（例: 2024-01-01）")
    sel.add_argument("--limit", type=int, help="新規作成する最大件数")

    run = argparse.ArgumentParser(add_help=False)
    run.add_argument("--dry-run", action="store_true", help="書き込みせずに実行内容だけ表示")
    run.add_argument("--no-create", action="store_true", help="新しいミラー PR を作らず、既存の対応だけ同期")
    run.add_argument("--no-push", action="store_true", help="git によるブランチ転送をしない")
    run.add_argument("--force-push", action="store_true",
                     help="head ブランチが両側で食い違う場合、同期元を優先して強制更新する")
    run.add_argument("--no-comments", action="store_true", help="コメントを同期しない")
    run.add_argument("--watch", type=int, metavar="SEC", help="SEC 秒ごとに繰り返し実行")

    ap = argparse.ArgumentParser(description="GitHub / Backlog Git 間の PR 双方向同期ツール")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check", parents=[common], help="API・git の接続確認")
    sub.add_parser("list", parents=[common, sel], help="両リポジトリの PR と同期状況を表示")
    sub.add_parser("sync", parents=[common, sel, run], help="未同期 PR のミラーを作成し、全体を同期")
    sub.add_parser("migrate", parents=[common, sel, run], help="sync と同じ（旧コマンド名）")
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
    for k, v in (("dry_run", False), ("no_push", False), ("force_push", False), ("no_create", False),
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
            raise ConfigError("options.closed_pr_strategy は skip / snapshot / as-is のいずれかです")
        if opts["directions"] not in ("both", "a_to_b", "b_to_a"):
            raise ConfigError("options.directions は both / a_to_b / b_to_a のいずれかです")
        if "{label}" not in opts["title_label_format"]:
            raise ConfigError("options.title_label_format には {label} を含めてください（例: \"[{label}] \"）")
        _TZ_OFFSET = opts["timezone_offset_hours"]

        sides: Dict[str, Side] = {}
        for n, alias in (("a", "source"), ("b", "target")):
            c = cfg.get(n) or cfg.get(alias)
            if not c:
                raise ConfigError(f"設定ファイルに \"{n}\" がありません")
            label = c.get("label") or DEFAULT_LABEL.get(c.get("type"), n.upper())
            sides[n] = Side(n, make_provider(c, dotenv, n), str(label))
        sides["a"].other, sides["b"].other = sides["b"], sides["a"]
        if sides["a"].label == sides["b"].label:
            raise ConfigError(f"a と b のラベルが同じ（{sides['a'].label}）です。label で別の名前を付けてください")

        state_path = Path(opts["state_file"]) if opts["state_file"] else \
            base_dir / "state" / f"{cfg_path.stem}.state.json"
        if not state_path.is_absolute():
            state_path = base_dir / state_path
        state = State(state_path, dry_run=args.dry_run or args.command == "list")
        pair = {"a": sides["a"].p.describe(), "b": sides["b"].p.describe()}
        if state.data.get("pair") and state.data["pair"] != pair:
            raise ConfigError(f"状態ファイル {state_path} は別のリポジトリの組のものです: {state.data['pair']}")
        state.data["pair"] = pair
        sel = Selection(args, sides)

        git = None
        if opts["push_branches"] and args.command in ("check", "migrate", "sync") and not args.no_push:
            if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
                raise ConfigError("git コマンドが見つかりません（push_branches=false で無効化できます）")
            work = Path(opts["work_dir"])
            if not work.is_absolute():
                work = base_dir / work
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{pair['a']}__{pair['b']}")[:120]
            git = GitMirror(work / f"{name}.git", {"a": sides["a"].p.git_url(), "b": sides["b"].p.git_url()},
                            args.dry_run)
    except ConfigError as e:
        error(str(e))
        return 2

    syncer = Syncer(sides["a"], sides["b"], opts, state, args, git)
    if args.command == "check":
        return cmd_check(sides, git)
    if args.command == "list":
        try:
            return cmd_list(sides, syncer, sel, state)
        except Exception as e:  # noqa: BLE001
            error(str(e))
            if DEBUG:
                traceback.print_exc()
            return 1

    a, b = sides["a"], sides["b"]
    log(f"[{a.label}] {a.p.describe()}  ⇄  [{b.label}] {b.p.describe()}" + ("  [dry-run]" if args.dry_run else ""))
    while True:
        try:
            stats = syncer.run(sel)
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
            git.reset()
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
