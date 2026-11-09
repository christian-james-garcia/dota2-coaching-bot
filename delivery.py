"""Getting a debrief into Discord without the formatting falling apart.

An embed description has a hard length limit, so a debrief is split. The split
has to respect Discord markdown: a bold marker separated from its partner shows
up as literal asterisks, and the prompt asks for bold on exactly the takeaways
meant to stand out.
"""
import re
from typing import Optional

import discord

EMBED_CHUNK_LIMIT = 3800
EMBED_COLOR = discord.Color.purple
TITLE = "\U0001f9e0 Dota 2 AI Debrief"
FOOTER = "OpenDota Telemetry Engine"


def balanced_split_point(text: str, split_at: int) -> int:
    """Move a split point back until it is not inside a ** ** pair.

    Discord renders a chunk on its own, so a bold marker separated from its
    partner shows up as literal asterisks. The prompt asks for bold on the key
    takeaways, so an unbalanced split disfigures exactly the emphasized text.
    Falls back to the original point when no balanced boundary exists.
    """
    candidate = split_at
    while candidate > 0 and text[:candidate].count("**") % 2:
        previous = max(text.rfind("\n", 0, candidate), text.rfind(" ", 0, candidate))
        if previous <= 0:
            return split_at
        candidate = previous
    return candidate or split_at


def chunk_debrief(text: str, max_len: int = 3800) -> list:
    """Split text into chunks that stay below Discord's embed description limit."""
    if not text:
        return []

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        pieces = []
        remaining = paragraph
        while len(remaining) > max_len:
            split_at = max(remaining.rfind("\n", 0, max_len), remaining.rfind(" ", 0, max_len))
            if split_at < max_len // 2:
                split_at = max_len
            split_at = balanced_split_point(remaining, split_at)
            pieces.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        pieces.append(remaining)

        for piece in pieces:
            addition = piece if not current else "\n\n" + piece
            if len(current) + len(addition) > max_len:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current += addition
    if current:
        chunks.append(current)
    return chunks


MATCH_IN_TITLE = re.compile(r"Match (\d+)")
ACCOUNT_IN_FOOTER = re.compile(r"acct (\d+)")


async def send_debrief(channel, match_id: int, text: str, account_id=None) -> int:
    """Send a debrief as one embed per chunk. Returns the number sent.

    The caller advances the queue only after this returns, so a failure part way
    through leaves the match queued and the whole debrief is re-sent later.

    The match id goes in the title and the account id in the footer so that
    `recover_cursors` can read back what was delivered. That makes the channel
    itself the durable record, which matters on hosts with an ephemeral disk.
    """
    parts = chunk_debrief(text)
    total = len(parts)
    for index, part in enumerate(parts, 1):
        title = f"{TITLE} \u2014 Match {match_id}"
        if total > 1:
            title += f" (Part {index}/{total})"
        footer = f"{FOOTER} | Part {index} of {total}"
        if account_id is not None:
            footer += f" | acct {account_id}"
        embed = discord.Embed(title=title, description=part, color=EMBED_COLOR())
        embed.set_footer(text=footer)
        await channel.send(embed=embed)
    return total


def read_delivery(embed) -> tuple:
    """(match_id, account_id) from a posted embed; either may be None."""
    title = (getattr(embed, "title", None) or "") if not isinstance(embed, dict) else ""
    footer = getattr(getattr(embed, "footer", None), "text", None)
    if footer is None and isinstance(getattr(embed, "footer", None), str):
        footer = embed.footer
    match = MATCH_IN_TITLE.search(title or "")
    account = ACCOUNT_IN_FOOTER.search(footer or "")
    return (int(match.group(1)) if match else None,
            int(account.group(1)) if account else None)


async def recover_cursors(channel, author_id: int, tracked_ids, limit: int = 300) -> dict:
    """Highest delivered match id per tracked account, read back from the channel.

    The queue is written to disk, but on hosts with an ephemeral filesystem that
    file is gone after every redeploy, and discovery then re-anchors to the newest
    listed match and silently skips anything played during the downtime. The
    channel is the one durable record of what was actually posted, so it is read
    instead.

    Embeds posted before the footer carried an account id are attributed to the
    only tracked account when there is exactly one, and ignored otherwise, since
    guessing which account an old post belonged to could skip matches.
    """
    tracked = [int(t) for t in tracked_ids]
    sole_account = tracked[0] if len(tracked) == 1 else None
    recovered = {}
    try:
        async for message in channel.history(limit=limit):
            if getattr(message, "author", None) is None or message.author.id != author_id:
                continue
            for embed in getattr(message, "embeds", None) or []:
                match_id, account_id = read_delivery(embed)
                if match_id is None:
                    continue
                if account_id is None:
                    account_id = sole_account
                if account_id is None or account_id not in tracked:
                    continue
                if match_id > recovered.get(account_id, 0):
                    recovered[account_id] = match_id
    except discord.DiscordException as exc:
        print(f"[RECOVER] Could not read channel history: {exc}", flush=True)
        return {}
    for account_id, match_id in recovered.items():
        print(f"[RECOVER] Account {account_id} was last delivered match {match_id}.", flush=True)
    return recovered
