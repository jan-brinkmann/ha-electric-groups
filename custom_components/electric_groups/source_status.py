"""Describe which configured sources can contribute to a group measurement."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SourceStatusState(StrEnum):
    """List the translatable statuses shown independently of the measurement."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING_SOURCES = "missing_sources"
    NO_READINGS = "no_readings"
    NO_SOURCES = "no_sources"
    WAITING_FOR_REPORTS = "waiting_for_reports"
    IMPORTING_HISTORY = "importing_history"
    MISSING_BASE = "missing_base"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class SourceStatus:
    """Capture valid readings and any missing or unconfirmed source reports."""

    state: SourceStatusState
    total_sources: int
    available_sources: int | None = None
    missing_sources: tuple[str, ...] = ()
    pending_reports: tuple[str, ...] = ()
    unresolved_sources: int = 0

    @classmethod
    def untracked(cls, total_sources: int) -> SourceStatus:
        """Describe an inactive measurement, or one with no configured sources."""
        if not total_sources:
            return cls(SourceStatusState.NO_SOURCES, total_sources, 0)
        return cls(SourceStatusState.INACTIVE, total_sources)

    def attributes(self) -> dict[str, Any]:
        """Return counts and include source lists when they explain the status."""
        attributes: dict[str, Any] = {"total_sources": self.total_sources}
        if self.available_sources is not None:
            attributes["available_sources"] = self.available_sources
        if self.missing_sources:
            attributes["missing_sources"] = list(self.missing_sources)
        if self.pending_reports:
            attributes["pending_reports"] = list(self.pending_reports)
        if self.unresolved_sources:
            attributes["unresolved_sources"] = self.unresolved_sources
        return attributes
