"""Distinguishing a service failure from a defect in this project.

Every I/O block here degrades gracefully on a transient failure, which is
correct. The same handlers used to absorb KeyError and TypeError as well, so a
defect looked exactly like OpenDota having a bad minute and was retried on a
timer. Two bugs survived months that way.
"""
import traceback


def report_unexpected(where: str, exc: BaseException) -> None:
    """Log a logic error distinctly so it is not read as a service outage."""
    print(f"[BUG] {where}: {type(exc).__name__}: {exc}", flush=True)
    traceback.print_exc()
