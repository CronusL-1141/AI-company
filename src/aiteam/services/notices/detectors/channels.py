"""E10 channel_mention: someone mentioned this reader on a channel.

Reuses ``count_channel_unread`` (pure read, never advances a cursor). The model
note carries what the old UserPromptSubmit hook injected: per channel the read
call and the clear call with every argument filled in except ``last_read_at``,
which must be the message the model actually read.
"""

from __future__ import annotations

import json

from aiteam.services.notices.detectors import DetectContext, Finding, Scope

MAX_CHANNELS = 3
EXCERPT_CHARS = 80
IDENT_CHARS = 80


def _quote(value: object, limit: int) -> str:
    from aiteam.services.notices.render import clean_text

    return json.dumps(clean_text(value)[:limit], ensure_ascii=False)


def render_details(reader: str, project_id: str, channels: list[dict], truncated: bool) -> str:
    """Language-neutral per-channel instructions for the model note."""
    lines = []
    for entry in channels[:MAX_CHANNELS]:
        channel = str(entry.get("channel") or "")
        lines.append(
            f"- channel={_quote(channel, 200)} sender={_quote(entry.get('latest_sender'), IDENT_CHARS)} "
            f"count={int(entry.get('count') or 0)} latest={_quote(entry.get('latest_excerpt'), EXCERPT_CHARS)}"
        )
        lines.append(
            f"  channel_read(channel={json.dumps(channel, ensure_ascii=False)}) then "
            f"channel_read_ack(channel={json.dumps(channel, ensure_ascii=False)}, "
            f"reader={json.dumps(reader)}, project_id={json.dumps(project_id)}, "
            "last_read_at=<created_at of the last message you read>)"
        )
    hidden = len(channels) - MAX_CHANNELS
    if hidden > 0:
        lines.append(f"- {hidden} more channel(s) unread: channel_unread lists all of them")
    if truncated:
        lines.append("- scan limit reached: the real count may be higher")
    return "\n".join(lines)


class ChannelMentionDetector:
    """One key per reader, project and newest mention; a newer message is a new key."""

    name = "channels"
    catalog_ids = ("channel_mention",)
    timing = frozenset({"prompt"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return bool(ctx.reader and ctx.project_id)

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=(f"channel_mention:{ctx.reader}:{ctx.project_id}:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        channels, truncated = await ctx.repo.count_channel_unread(ctx.reader, ctx.project_id)
        channels = [entry for entry in channels if int(entry.get("count") or 0) > 0]
        if not channels:
            return []
        latest = channels[0]
        latest_at = max(entry["latest_at"] for entry in channels)
        stamp = latest_at.isoformat() if hasattr(latest_at, "isoformat") else str(latest_at)
        return [Finding(
            catalog_id="channel_mention",
            key=f"channel_mention:{ctx.reader}:{ctx.project_id}:{stamp}",
            params={
                "sender": latest.get("latest_sender") or "?",
                "channel": latest.get("channel") or "?",
                "n": sum(int(entry.get("count") or 0) for entry in channels),
                "details": render_details(ctx.reader, ctx.project_id, channels, truncated),
            },
            project_id=ctx.project_id,
            # The reader belongs to the exit hook of this host (leader-cc, leader-codex).
            host=ctx.host,
        )]
