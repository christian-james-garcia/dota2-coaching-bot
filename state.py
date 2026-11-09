"""The durable match queue.

A single object owns the pending list, the per-account cursors, the retry
schedule and persistence. These were five module-level functions mutating one
global dict, which made the queue impossible to exercise without reaching into
module state, and made the retry policy a property of the call site rather than
of the queue.

The on-disk format is unchanged: {"accounts": {...}, "pending": [...]}.
"""
import json
import os
import time

from config import (
    BUG_RETRY_LIMIT,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
)


def backoff_seconds(attempts: int) -> int:
    """Delay before retry number `attempts`, capped at the configured ceiling."""
    return min(
        RETRY_BACKOFF_MAX_SECONDS,
        RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
    )


class MatchState:
    def __init__(self, path, accounts=None, pending=None):
        self.path = path
        self.accounts = {} if accounts is None else accounts
        self.pending = [] if pending is None else pending

    # ---- persistence ----

    @classmethod
    def load(cls, path, tracked_steam_ids=()):
        """Read the queue from disk, tolerating absence and corruption."""
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            # Migrate the one-cursor state file made by the earlier version.
            if "last_processed_match" in stored and tracked_steam_ids:
                old_cursor = stored.get("last_processed_match")
                if old_cursor is not None:
                    account_id = str(tracked_steam_ids[0])
                    return cls(path, accounts={
                        account_id: {
                            "last_processed_match": int(old_cursor),
                            "discovered_through_match": int(old_cursor),
                        }
                    })
            accounts = stored.get("accounts", {})
            pending = stored.get("pending", [])
            if not isinstance(accounts, dict) or not isinstance(pending, list):
                raise ValueError("state must contain an accounts object and pending list")
            return cls(path, accounts=accounts, pending=pending)
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"[STATE] Could not read {path}: {exc}", flush=True)
            return cls(path)

    def save(self) -> None:
        """Persist cursors and the queue atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_file.write_text(
            json.dumps({"accounts": self.accounts, "pending": self.pending}),
            encoding="utf-8",
        )
        os.replace(temporary_file, self.path)

    def as_dict(self) -> dict:
        return {"accounts": self.accounts, "pending": self.pending}

    # ---- queue contents ----

    def account(self, account_id) -> dict:
        return self.accounts.setdefault(
            str(account_id),
            {"last_processed_match": None, "discovered_through_match": None},
        )

    def queued_keys(self) -> set:
        return {
            (str(item.get("account_id")), int(item.get("match_id")))
            for item in self.pending
            if isinstance(item, dict)
            and item.get("account_id") is not None
            and item.get("match_id") is not None
        }

    def queue(self, account_id: int, match_id: int) -> None:
        self.pending.append({"account_id": account_id, "match_id": match_id})

    def find(self, account_id: int, match_id: int):
        for item in self.pending:
            if (int(item.get("account_id", -1)) == account_id
                    and int(item.get("match_id", -1)) == match_id):
                return item
        return None

    def eligible(self, now=None) -> list:
        """Queued matches whose backoff has elapsed and that are not parked."""
        now = time.time() if now is None else now
        return [
            item for item in self.pending
            if float(item.get("next_attempt_at", 0)) <= now and not item.get("blocked")
        ]

    # ---- transitions ----

    def release_blocked(self) -> "MatchState":
        """A fresh process is usually a redeploy, so give parked matches another try."""
        for item in self.pending:
            if item.pop("blocked", None):
                item["attempts"] = 0
                item["next_attempt_at"] = 0
                print(f"[QUEUE] Unparked match {item.get('match_id')} after restart.", flush=True)
        return self

    def accounts_without_a_cursor(self, tracked_ids) -> list:
        """Tracked accounts this queue has never delivered a match for."""
        return [
            int(t) for t in tracked_ids
            if self.accounts.get(str(t), {}).get("last_processed_match") is None
        ]

    def adopt_cursors(self, recovered: dict) -> "MatchState":
        """Seed cursors from an external record of what was already delivered.

        Only fills gaps: an account that already has a cursor keeps it, because
        the stored value is authoritative and the external record is a fallback.
        """
        for account_id, match_id in recovered.items():
            account = self.account(account_id)
            if account.get("last_processed_match") is None:
                account["last_processed_match"] = int(match_id)
                account["discovered_through_match"] = int(match_id)
                print(
                    f"[RECOVER] Adopted cursor {match_id} for account {account_id}; "
                    f"no stored cursor was found.",
                    flush=True,
                )
        if recovered:
            self.save()
        return self

    def defer(self, account_id: int, match_id: int, reason: str, fatal: bool = False) -> None:
        """Back off on a failed match without blocking later matches in the queue.

        `fatal` marks a failure that retrying cannot fix, such as a logic error.
        Those are parked after BUG_RETRY_LIMIT attempts instead of retried
        forever, because a deterministic failure retried on a timer only buries
        the log line that explains it. Parked matches stay in the queue and are
        released on the next restart.
        """
        item = self.find(account_id, match_id)
        if item is None:
            return
        attempts = int(item.get("attempts", 0)) + 1
        delay_seconds = backoff_seconds(attempts)
        item["attempts"] = attempts
        item["next_attempt_at"] = time.time() + delay_seconds
        if fatal and attempts >= BUG_RETRY_LIMIT:
            item["blocked"] = True
            print(
                f"[BLOCKED] Match {match_id} parked after {attempts} attempts at "
                f"{reason}. This needs a code fix, not a retry; it will be released "
                f"on the next restart.",
                flush=True,
            )
        else:
            print(
                f"[RETRY] Match {match_id} deferred for {delay_seconds // 60} minute(s) "
                f"after {reason}; attempt {attempts}.",
                flush=True,
            )
        self.save()

    def complete(self, account_id: int, match_id: int) -> None:
        """Drop a delivered match and advance that account's cursor.

        Called only after every embed part was accepted by Discord, so a parse,
        provider or send failure leaves the match queued for retry.
        """
        self.pending = [
            item for item in self.pending
            if not (int(item["account_id"]) == account_id
                    and int(item["match_id"]) == match_id)
        ]
        self.account(account_id)["last_processed_match"] = match_id
        self.save()
