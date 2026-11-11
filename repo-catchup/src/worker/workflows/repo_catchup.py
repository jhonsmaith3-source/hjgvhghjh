import json
import re
from datetime import datetime, timezone
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai import Agent, Runner
from mistralai.workflows.plugins.mistralai.connectors import ToolCallClient, connector, uses_connectors
from pydantic import BaseModel

github_connector = connector("github_app", run_as="auto")
slack_connector = connector("slack", run_as="auto")

MODEL = "mistral-medium-latest"
MAX_PAGES = 5
PAGE_SIZE = 100


class RepoCatchupInput(BaseModel):
    repo: str
    since: str


class PullRequest(BaseModel):
    number: int
    title: str
    url: str
    author: str
    updated_at: str | None = None
    summary: str | None = None


def _parse_repo_handle(repo: str) -> tuple[str, str]:
    handle = repo.strip().rstrip("/")
    handle = re.sub(r"^https?://(www\.)?github\.com/", "", handle)
    handle = re.sub(r"\.git$", "", handle)
    parts = handle.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo handle: {repo!r}. Use 'owner/name' or a GitHub URL.")
    return parts[0], parts[1]


def _parse_since(since: str) -> str:
    try:
        parsed = datetime.strptime(since.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date: {since!r}. Use YYYY-MM-DD.") from exc
    return parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_tool_payload(result: Any) -> Any:
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            text = "\n".join(
                item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            return result
    else:
        return result
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _to_pull_requests(payload: Any) -> list[PullRequest]:
    items = payload if isinstance(payload, list) else []
    prs: list[PullRequest] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prs.append(
            PullRequest(
                number=int(item.get("number", 0)),
                title=str(item.get("title", "")),
                url=str(item.get("html_url", "") or item.get("url", "")),
                author=str((item.get("user") or {}).get("login", "unknown")),
                updated_at=item.get("updated_at"),
            )
        )
    return [pr for pr in prs if pr.number]


def _merged_since(prs: list[PullRequest], since_iso: str) -> list[PullRequest]:
    return [
        pr
        for pr in prs
        if pr.updated_at is not None and pr.updated_at >= since_iso
    ]


def _waiting_on_review(prs: list[PullRequest]) -> list[PullRequest]:
    return [pr for pr in prs if pr.updated_at is not None]


def _format_pr_lines(prs: list[PullRequest]) -> str:
    return "\n".join(f"#{pr.number} | {pr.title} | author: {pr.author}" for pr in prs)


def _parse_summaries(raw: str, prs: list[PullRequest]) -> dict[int, str]:
    summaries: dict[int, str] = {}
    for line in raw.splitlines():
        match = re.match(r"\s*#?(\d+)\s*[|:\-]\s*(.+)", line.strip())
        if match:
            summaries[int(match.group(1))] = match.group(2).strip()
    return {pr.number: summaries.get(pr.number, pr.title) for pr in prs}


@workflows.activity()
async def fetch_merged_pull_requests(
    owner: str,
    repo: str,
    since_iso: str,
    github: ToolCallClient = Depends(github_connector),
) -> list[PullRequest]:
    collected: list[PullRequest] = []
    for page in range(1, MAX_PAGES + 1):
        result = await github.call_tool(
            tool_name="list_pull_requests",
            arguments={
                "owner": owner,
                "repo": repo,
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": PAGE_SIZE,
                "page": page,
            },
        )
        prs = _to_pull_requests(_extract_tool_payload(result))
        if not prs:
            break
        collected.extend(prs)
        oldest = min(pr.updated_at or "" for pr in prs)
        if oldest < since_iso:
            break
    return _merged_since(collected, since_iso)


@workflows.activity()
async def fetch_open_pull_requests(
    owner: str,
    repo: str,
    github: ToolCallClient = Depends(github_connector),
) -> list[PullRequest]:
    result = await github.call_tool(
        tool_name="list_pull_requests",
        arguments={
            "owner": owner,
            "repo": repo,
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": PAGE_SIZE,
        },
    )
    return _to_pull_requests(_extract_tool_payload(result))


@workflows.activity()
async def summarize_pull_requests(section: str, prs: list[PullRequest]) -> list[PullRequest]:
    if not prs:
        return prs
    agent = Agent(
        name=f"repo-catchup-summarizer-{section}",
        model=MODEL,
        instructions=(
            "You write one-line summaries of GitHub pull requests for a digest. "
            "For each input line '#<number> | <title> | author: <login>', reply with exactly one line "
            "'#<number> | <one-line summary>' per pull request, in the same order. "
            "The summary must be a single sentence under 100 characters, no markdown, no links."
        ),
    )
    result = await Runner.run(agent=agent, inputs=f"{section}:\n{_format_pr_lines(prs)}")
    text = " ".join(getattr(chunk, "text", "") for chunk in result)
    summaries = _parse_summaries(text, prs)
    return [pr.model_copy(update={"summary": summaries[pr.number]}) for pr in prs]


@workflows.activity()
async def build_digest(
    owner: str,
    repo: str,
    since_display: str,
    merged: list[PullRequest],
    waiting: list[PullRequest],
) -> str:
    lines = [f"*Repo catch-up: `{owner}/{repo}` (merged since {since_display})*"]

    lines.append(f"\n*Merged — {len(merged)} PR(s)*")
    if merged:
        for pr in merged:
            lines.append(f"• {pr.summary or pr.title} — <{pr.url}|#{pr.number}>")
    else:
        lines.append("• Nothing merged in this period.")

    lines.append(f"\n*Waiting on review — {len(waiting)} open PR(s)*")
    if waiting:
        for pr in waiting:
            lines.append(f"• {pr.summary or pr.title} — <{pr.url}|#{pr.number}>")
    else:
        lines.append("• No open pull requests.")

    return "\n".join(lines)


@workflows.activity()
async def post_to_slack_self(
    digest: str,
    slack: ToolCallClient = Depends(slack_connector),
) -> str:
    me = _extract_tool_payload(await slack.call_tool(tool_name="slack_get_own_user_info", arguments={}))
    user_id = me.get("user", {}).get("id") if isinstance(me, dict) else None
    if not user_id and isinstance(me, dict):
        user_id = me.get("id")
    if not user_id:
        raise ValueError("Could not resolve own Slack user ID.")
    await slack.call_tool(
        tool_name="slack_send_message",
        arguments={"channel_id": user_id, "text": digest},
    )
    return f"Digest posted to your Slack self-chat ({len(digest)} characters)."


@workflows.workflow.define(
    name="repo-catchup",
    workflow_display_name="Repo Catch-up Digest",
    workflow_description=(
        "Lists pull requests merged since a given date and open PRs waiting on review "
        "in a GitHub repo, summarizes each in one line, and posts the digest to your Slack self-chat."
    ),
    on_behalf_of=True,
)
@uses_connectors(github_connector, slack_connector)
class RepoCatchupWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: RepoCatchupInput) -> str:
        owner, repo = _parse_repo_handle(input.repo)
        since_iso = _parse_since(input.since)

        merged = await fetch_merged_pull_requests(owner=owner, repo=repo, since_iso=since_iso)
        waiting = await fetch_open_pull_requests(owner=owner, repo=repo)

        merged = await summarize_pull_requests(section="merged", prs=merged)
        waiting = await summarize_pull_requests(section="waiting on review", prs=waiting)

        digest = await build_digest(
            owner=owner,
            repo=repo,
            since_display=input.since,
            merged=merged,
            waiting=waiting,
        )
        return await post_to_slack_self(digest=digest)
