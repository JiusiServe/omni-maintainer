"""Who did what: human-only controls verified by timeline actor.

A label's presence proves nothing, because the routine's token can add or
remove labels. The gate therefore asks the issue timeline who applied a
label, and treats it as effective only when the latest ``labeled`` event
for it was performed by an allowlisted human (``policy.identities.humans``)
and, when a head is involved, after the current head commit was pushed.

For the pause label the asymmetry is deliberate: a human ``labeled`` event
stays effective until a human ``unlabeled`` event, so a routine removing the
label changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .reads import parse_time

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class LabelEvent:
    event: str                # labeled | unlabeled
    label: str
    actor_login: str
    actor_type: str
    created_at: datetime | None

    @property
    def by_human(self) -> bool:
        return self.actor_type == "User" and not self.actor_login.endswith("[bot]")

    @property
    def stamp(self) -> datetime:
        return self.created_at or _EPOCH


def label_events(timeline: Iterable[dict[str, Any]]) -> list[LabelEvent]:
    """Label events in chronological order; undated events sort first."""
    out: list[LabelEvent] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "")
        if event not in ("labeled", "unlabeled"):
            continue
        actor = item.get("actor") or {}
        out.append(LabelEvent(
            event=event,
            label=str((item.get("label") or {}).get("name") or ""),
            actor_login=str(actor.get("login") or ""),
            actor_type=str(actor.get("type") or ""),
            created_at=parse_time(item.get("created_at")),
        ))
    out.sort(key=lambda e: e.stamp)
    return out


def human_label_active(timeline: Iterable[dict[str, Any]], *, label: str, humans: Iterable[str],
                       currently_present: bool, after: datetime | None = None) -> bool:
    """Is ``label`` effective as a human control?

    - The label must currently be present.
    - The latest ``labeled`` event for it must be by an allowlisted human.
    - When ``after`` is given (the current head commit time), that event
      must be at or after it, so a go granted for an earlier head lapses.
    """
    if not currently_present:
        return False
    allow = set(humans)
    granted_at: datetime | None = None
    active = False
    for event in label_events(timeline):
        if event.label != label:
            continue
        if not event.by_human or event.actor_login not in allow:
            # Bot or outsider events never grant and never revoke: a routine
            # re-adding a label a human removed does not resurrect the grant.
            continue
        if event.event == "labeled":
            active, granted_at = True, event.created_at
        else:
            active, granted_at = False, None
    if not active:
        return False
    if after is not None and granted_at is not None and granted_at < after:
        return False
    return True


def human_pause_active(timeline: Iterable[dict[str, Any]], *, label: str, humans: Iterable[str]) -> bool:
    """Pause is on after a human ``labeled`` until a human ``unlabeled``.

    Events by non-humans are ignored in both directions: a routine can
    neither pause the system nor lift a human's pause.
    """
    allow = set(humans)
    active = False
    for event in label_events(timeline):
        if event.label != label or not event.by_human or event.actor_login not in allow:
            continue
        active = event.event == "labeled"
    return active
