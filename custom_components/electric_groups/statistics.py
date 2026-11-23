"""Backfill a group's Recorder history from signed cumulative member statistics."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from math import fsum, isclose, isfinite
from typing import Literal

from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    get_last_short_term_statistics,
    get_last_statistics,
    get_metadata,
    get_short_term_statistics_run_cache,
    get_start_time,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .energy import EnergyState, MeterCheckpoint
from .sources import Reading, SourceWeights

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIT = UnitOfEnergy.KILO_WATT_HOUR
_FIVE_MINUTES = 300
_HOUR = 3600


@dataclass
class History:
    """Calculated hourly rows, recent rows, and initial live meter readings."""

    hourly: list[StatisticData] = field(default_factory=list)
    short_term: list[StatisticData] = field(default_factory=list)
    state: EnergyState = field(
        default_factory=lambda: EnergyState(
            total=0.0, meters={}, history_imported=False
        )
    )


@dataclass(frozen=True)
class RecordedReading:
    """A Recorder consumption sum with the corresponding sensor reading."""

    reading: Reading
    total: float


def combine_statistics(
    hourly: dict[str, list[StatisticsRow]],
    short_term: dict[str, list[StatisticsRow]],
    weights: SourceWeights,
) -> History:
    """Combine meter sums at interval ends, carrying readings across gaps."""
    result = History()
    events: dict[float, dict[str, RecordedReading]] = {}
    latest: dict[str, RecordedReading] = {}
    for rows, duration, output in (
        (hourly, _HOUR, result.hourly),
        (short_term, _FIVE_MINUTES, result.short_term),
    ):
        ends: set[float] = set()
        for ref, records in rows.items():
            for row in records:
                value, total = row.get("state"), row.get("sum")
                if (
                    value is None
                    or total is None
                    or not isfinite(value)
                    or not isfinite(total)
                ):
                    continue
                end = row["start"] + duration
                events.setdefault(end, {})[ref] = RecordedReading(
                    Reading(value, row.get("last_reset")), total
                )
                ends.add(end)

        # Hourly rows enter the timeline only when their hour ends. Five-minute
        # rows win when both readings for a meter end at the same time.
        latest.clear()
        for end, updates in sorted(events.items()):
            latest.update(updates)
            if end in ends:
                total = fsum(weights[ref] * row.total for ref, row in latest.items())
                output.append(
                    StatisticData(
                        start=dt_util.utc_from_timestamp(end - duration),
                        state=total,
                        sum=total,
                    )
                )

    result.state = EnergyState(
        total=fsum(weights[ref] * row.total for ref, row in latest.items()),
        meters={
            ref: MeterCheckpoint(
                value=row.reading.value,
                last_reset=row.reading.last_reset,
                weight=weights[ref],
            )
            for ref, row in latest.items()
        },
        history_imported=False,
    )
    return result


def _last_target_row(recorder: Recorder, entity_id: str) -> StatisticsRow | None:
    """Find the last target row, preferring the short-term checkpoint."""
    for getter in (get_last_short_term_statistics, get_last_statistics):
        rows = getter(recorder.hass, 1, entity_id, False, {"state", "sum"})
        if matching_rows := rows.get(entity_id):
            return matching_rows[0]
    return None


def prepare_history(
    recorder: Recorder,
    entity_id: str,
    source_ids: dict[str, str],
    weights: SourceWeights,
    pending: EnergyState | None,
) -> History | None:
    """Read compatible member rows, or confirm a previously committed import."""
    if (row := _last_target_row(recorder, entity_id)) is not None:
        if pending is not None and all(
            value is not None and isclose(value, pending["total"])
            for value in (row.get("state"), row.get("sum"))
        ):
            return None
        raise HomeAssistantError(f"{entity_id} already has Recorder statistics")

    refs_by_id = {entity_id: ref for ref, entity_id in source_ids.items()}
    if not refs_by_id:
        return History()
    metadata = get_metadata(recorder.hass, statistic_ids=set(refs_by_id))
    compatible = {
        key
        for key, (_, meta) in metadata.items()
        if meta["has_sum"]
        and meta["unit_of_measurement"] in EnergyConverter.VALID_UNITS
        and meta["unit_class"] in (None, EnergyConverter.UNIT_CLASS)
    }
    if not compatible:
        return History()

    short_end = get_start_time() + timedelta(seconds=_FIVE_MINUTES)
    hourly_end = short_end.replace(minute=0)
    periods: list[tuple[Literal["hour", "5minute"], datetime]] = [
        ("hour", hourly_end),
        ("5minute", short_end),
    ]
    series = []
    for period, end in periods:
        rows = statistics_during_period(
            recorder.hass,
            _EPOCH,
            end,
            compatible,
            period,
            {"energy": _UNIT},
            {"state", "sum", "last_reset"},
        )
        series.append({refs_by_id[key]: values for key, values in rows.items()})
    return combine_statistics(series[0], series[1], weights)


def write_history(recorder: Recorder, entity_id: str, history: History) -> None:
    """Commit both history tables atomically and seed Recorder's live delta baseline."""
    if not history.state["meters"]:
        return
    if _last_target_row(recorder, entity_id) is not None:
        raise HomeAssistantError(
            f"{entity_id} acquired Recorder statistics during import"
        )

    metadata = StatisticMetaData(
        statistic_id=entity_id,
        source="recorder",
        name=None,
        unit_of_measurement=_UNIT,
        unit_class="energy",
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
    )
    short_term = history.short_term.copy()
    start = get_start_time()
    total = history.state["total"]
    if not short_term or short_term[-1]["start"] < start:
        short_term.append(StatisticData(start=start, state=total, sum=total))

    # The regular Recorder import API queues each table separately. Use one
    # transaction so a retry never sees half an import.
    with recorder.get_session() as session, session.begin():
        manager = recorder.statistics_meta_manager
        existing = manager.get_many(session, statistic_ids={entity_id})
        _, metadata_id = manager.update_or_add(session, metadata, existing)
        session.add_all(
            Statistics.from_stats(metadata_id, row) for row in history.hourly
        )
        recent_rows = [
            StatisticsShortTerm.from_stats(metadata_id, row) for row in short_term
        ]
        session.add_all(recent_rows)
        session.flush()
        latest_id = recent_rows[-1].id

    get_short_term_statistics_run_cache(recorder.hass).set_latest_id_for_metadata_id(
        metadata_id, latest_id
    )


@dataclass
class _HistoryTask[T](RecorderTask):
    """Run Recorder work in queue order and report completion to the event loop."""

    operation: Callable[[Recorder], T]
    future: asyncio.Future[T]

    def run(self, instance: Recorder) -> None:
        """Complete the future with a return value or the Recorder error."""
        if self.future.cancelled():
            return
        try:
            result = self.operation(instance)
        except Exception as err:
            complete = partial(self.future.set_exception, err)
        else:
            complete = partial(self.future.set_result, result)
        instance.hass.loop.call_soon_threadsafe(self._finish, complete)

    def _finish(self, complete: Callable[[], None]) -> None:
        """Resolve the awaiting coroutine unless the entity was unloaded."""
        if not self.future.done():
            complete()


async def async_recorder_job[T](
    recorder: Recorder, operation: Callable[[Recorder], T]
) -> T:
    """Wait for Recorder readiness and run an operation on its write queue."""
    if not await asyncio.shield(recorder.async_db_ready):
        raise HomeAssistantError("The Recorder database is unavailable")
    future = recorder.hass.loop.create_future()
    recorder.queue_task(_HistoryTask(operation, future))
    return await future
