"""Accumulate signed meter changes and persist their last usable readings."""

from dataclasses import dataclass, field
from math import fsum
from typing import NotRequired, TypedDict

from .sources import Reading, SourceWeights


class MeterCheckpoint(TypedDict):
    """A meter reading and the sign it had when this reading was tracked."""

    value: float
    last_reset: float | None
    weight: int


class EnergyState(TypedDict):
    """Persist totals, meter checkpoints, and recovery after a source outage."""

    total: float
    meters: dict[str, MeterCheckpoint]
    history_imported: bool
    published_total: NotRequired[float | None]
    all_sources_available: NotRequired[bool | None]
    pending_reports: NotRequired[list[str]]


@dataclass
class EnergyAccumulator:
    """Track signed changes, the published total, and recovery confirmations."""

    total: float = 0.0
    meters: dict[str, MeterCheckpoint] = field(default_factory=dict)
    history_imported: bool = False
    published_total: float | None = None
    all_sources_available: bool | None = None
    pending_reports: set[str] = field(default_factory=set)

    def restore(self, state: EnergyState, weights: SourceWeights) -> None:
        """Restore totals and recovery state for the configured members."""
        self.total = state["total"]
        self.history_imported = state["history_imported"]
        # Older groups published every accumulated change immediately.
        self.published_total = state.get(
            "published_total", self.total if state["meters"] else None
        )
        self.all_sources_available = state.get("all_sources_available")
        self.pending_reports = set(state.get("pending_reports", ())) & weights.keys()
        self.meters = {
            ref: reading
            for ref, reading in state["meters"].items()
            if weights.get(ref) == reading["weight"]
        }

    def snapshot(self) -> EnergyState:
        """Return a consistent checkpoint suitable for storage."""
        return EnergyState(
            total=self.total,
            meters=self.meters.copy(),
            history_imported=self.history_imported,
            published_total=self.published_total,
            all_sources_available=self.all_sources_available,
            pending_reports=sorted(self.pending_reports),
        )

    def update_availability(
        self,
        weights: SourceWeights,
        unavailable: set[str],
        *,
        require_all: bool,
        confirmed_refs: set[str],
    ) -> bool:
        """Update pending confirmations and return whether recovery state changed.

        In strict mode, ignore a returning meter until its reading is confirmed.
        Once all sources are available, require confirmation from every
        source after that point before exposing a group value.
        """
        complete = not unavailable
        if not require_all:
            pending = set()
        elif complete and self.all_sources_available is False:
            pending = set(weights)
        else:
            pending = (self.pending_reports | unavailable) - confirmed_refs

        changed = (
            self.all_sources_available != complete or self.pending_reports != pending
        )
        self.all_sources_available = complete
        self.pending_reports = pending
        return changed

    def update(self, ref: str, reading: Reading, weight: int, increasing: bool) -> bool:
        """Count the signed change, treating a first reading as a baseline."""
        new = MeterCheckpoint(
            value=reading.value, last_reset=reading.last_reset, weight=weight
        )
        previous = self.meters.get(ref)
        if new == previous:
            return False
        if previous is not None:
            reset = (
                reading.last_reset is not None
                and reading.last_reset != previous["last_reset"]
            ) or (increasing and 0 <= reading.value < previous["value"])
            change = reading.value if reset else reading.value - previous["value"]
            self.total = fsum((self.total, weight * change))
        self.meters[ref] = new
        return True
