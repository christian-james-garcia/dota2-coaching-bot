"""Getting a debrief into Discord without the formatting falling apart.

An embed description has a hard length limit, so a debrief is split. The split
has to respect Discord markdown: a bold marker separated from its partner shows
up as literal asterisks, and the prompt asks for bold on exactly the takeaways
meant to stand out.
"""
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


async def send_debrief(channel, match_id: int, text: str) -> int:
    """Send a debrief as one embed per chunk. Returns the number sent.

    The caller advances the queue only after this returns, so a failure part way
    through leaves the match queued and the whole debrief is re-sent later.
    """
    parts = chunk_debrief(text)
    total = len(parts)
    for index, part in enumerate(parts, 1):
        title = f"{TITLE} \u2014 Match {match_id}"
        if total > 1:
            title += f" (Part {index}/{total})"
        embed = discord.Embed(title=title, description=part, color=EMBED_COLOR())
        embed.set_footer(text=f"{FOOTER} | Part {index} of {total}")
        await channel.send(embed=embed)
    return total
