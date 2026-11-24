"""Publish each virtual device's accumulated energy and instantaneous power."""

import logging
from asyncio import Task
from collections.abc import Callable
from functools import partial
from math import fsum

from homeassistant.components.recorder import get_instance, is_entity_recorded
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import (
    CoreState,
    Event,
    EventStateReportedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from sqlalchemy.exc import SQLAlchemyError

from . import ElectricGroupsEntry, GroupStorage, availability_signal, group_device_info
from .const import (
    CONF_IMPORT_HISTORY,
    CONF_SOURCES,
    DOMAIN,
    GROUP_DIFFERENCE,
    ISSUE_PENDING_ENERGY_REPORTS,
    MEASUREMENTS,
)
from .energy import EnergyAccumulator
from .sources import Reading, SourceWeights, read_measurement
from .statistics import async_recorder_job, prepare_history, write_history

_LOGGER = logging.getLogger(__name__)
_HISTORY_RETRY_DELAY = 60
_ENERGY_REPAIR_DELAY = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricGroupsEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up group sensors and remove power entities whose base has no power source."""
    for group in entry.subentries.values():
        device_info = group_device_info(group)
        entities: list[GroupSensor] = [GroupEnergySensor(entry, group, device_info)]
        power_sources = group.data[CONF_SOURCES][SensorDeviceClass.POWER]
        if group.subentry_type != GROUP_DIFFERENCE or any(
            weight > 0 for weight in power_sources.values()
        ):
            entities.append(
                GroupPowerSensor(
                    group, device_info, SensorDeviceClass.POWER, entry.runtime_data
                )
            )
        else:
            registry = er.async_get(hass)
            if entity_id := registry.async_get_entity_id(
                "sensor", entry.domain, f"{group.subentry_id}_{SensorDeviceClass.POWER}"
            ):
                registry.async_remove(entity_id)
        async_add_entities(entities, config_subentry_id=group.subentry_id)


class GroupSensor(SensorEntity):
    """Listen for source updates, registry renames and availability settings."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_available = False

    def __init__(
        self,
        group: ConfigSubentry,
        device_info: DeviceInfo,
        device_class: SensorDeviceClass,
        storage: GroupStorage,
    ) -> None:
        """Bind a sensor to the saved sources for its measurement."""
        self._attr_unique_id = f"{group.subentry_id}_{device_class}"
        self._attr_device_info = device_info
        self._attr_device_class = device_class
        self._attr_translation_key = str(device_class)
        self._attr_native_unit_of_measurement = MEASUREMENTS[device_class][1]
        self._group_id = group.subentry_id
        self._storage = storage
        self._is_difference = group.subentry_type == GROUP_DIFFERENCE
        self._weights: SourceWeights = group.data[CONF_SOURCES][device_class]
        self._source_ids: dict[str, str] = {}
        self._unsub_sources: list[Callable[[], None]] = []

    async def async_added_to_hass(self) -> None:
        """Subscribe to the sources and shared switch before the initial reading."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                availability_signal(self._group_id),
                self._async_availability_changed,
            )
        )
        self.async_on_remove(self._async_unsubscribe)
        self._async_subscribe()
        self._async_refresh()

    @property
    def _require_all_sources(self) -> bool:
        """Return whether this sensor requires all of its configured sources."""
        return self._storage.require_all_sources.get(self._group_id, False)

    @callback
    def _async_availability_changed(self) -> None:
        """Apply the new setting and publish any availability or value change."""
        previous = self._attr_available, self.native_value
        self._async_refresh()
        if previous != (self._attr_available, self.native_value):
            self.async_write_ha_state()

    @callback
    def _async_unsubscribe(self) -> None:
        """Remove any callbacks for the previous set of entity IDs."""
        for unsubscribe in self._unsub_sources:
            unsubscribe()
        self._unsub_sources.clear()

    @callback
    def _async_subscribe(self) -> None:
        """Resolve stable source references and listen on their current entity IDs."""
        self._async_unsubscribe()
        registry = er.async_get(self.hass)
        self._source_ids = {
            ref: entity_id
            for ref in self._weights
            if (entity_id := er.async_resolve_entity_id(registry, ref)) is not None
        }
        entity_ids = list(self._source_ids.values())
        if entity_ids:
            self._unsub_sources.extend(
                (
                    async_track_state_change_event(
                        self.hass, entity_ids, self._async_state_changed
                    ),
                    async_track_entity_registry_updated_event(
                        self.hass, entity_ids, self._async_registry_changed
                    ),
                )
            )

    @callback
    def _async_registry_changed(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        """Rebind subscriptions when an input entity is renamed or removed."""
        if "old_entity_id" in event.data or event.data["action"] == "remove":
            self._async_subscribe()
            self._async_refresh()
            self.async_write_ha_state()

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Update the published group measurement after an input changes."""
        self._async_refresh()
        self.async_write_ha_state()

    @callback
    def _async_refresh(self) -> None:
        """Update the sensor using the latest source states."""
        raise NotImplementedError

    @callback
    def _async_source_state(self, ref: str) -> State | None:
        """Return the current state for a source, even if its registry ID changed."""
        entity_id = self._source_ids.get(ref)
        return self.hass.states.get(entity_id) if entity_id is not None else None


class GroupEnergySensor(GroupSensor):
    """Track meter deltas and publish totals according to source availability."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3

    def __init__(
        self, entry: ElectricGroupsEntry, group: ConfigSubentry, device_info: DeviceInfo
    ) -> None:
        """Load the group type, configured import preference, and saved totals."""
        super().__init__(
            group, device_info, SensorDeviceClass.ENERGY, entry.runtime_data
        )
        self._entry = entry
        self._group_name = group.title
        self._accumulator = EnergyAccumulator()
        self._base_entity_ids: set[str] = set()
        self._history_task: Task[None] | None = None
        self._cancel_retry: Callable[[], None] | None = None
        self._repair_issue_id = f"{ISSUE_PENDING_ENERGY_REPORTS}_{self._group_id}"
        self._repair_entity_ids: tuple[str, ...] = ()
        self._cancel_repair_check: Callable[[], None] | None = None
        stored = self._storage.states.get(self._group_id)
        self._waiting_for_history = bool(
            self._weights
            and group.data[CONF_IMPORT_HISTORY]
            and (stored is None or not stored["history_imported"])
        )
        if stored is not None and not self._waiting_for_history:
            self._accumulator.restore(stored, self._weights)

    async def async_added_to_hass(self) -> None:
        """Register recovery and defer repair checks and history until startup."""
        await super().async_added_to_hass()
        self._storage.energy_recovery_callbacks[self._group_id] = (
            self._async_resume_energy
        )
        self.async_on_remove(self._async_remove_repair)
        self.async_on_remove(async_at_started(self.hass, self._async_update_repair))
        if self._waiting_for_history:
            self.async_on_remove(self._async_cancel_import)
            self.async_on_remove(async_at_started(self.hass, self._async_start_import))

    @property
    def _waiting_for_reports(self) -> bool:
        """Return whether strict mode is waiting for reports from online meters."""
        return (
            self._require_all_sources
            and self._accumulator.all_sources_available is True
            and bool(self._accumulator.pending_reports)
        )

    @property
    def _pending_entity_ids(self) -> tuple[str, ...]:
        """Return the sorted entity IDs still blocking recovery for online meters."""
        return tuple(
            sorted({self._source_ids[ref] for ref in self._accumulator.pending_reports})
        )

    @callback
    def _async_resume_energy(self) -> bool:
        """Resume energy with current readings and return whether it is available."""
        if not self._waiting_for_reports:
            return False
        self._async_refresh(accept_current_readings=True)
        if self.available:
            self.async_write_ha_state()
        return self.available

    @callback
    def _async_update_repair(self, hass: HomeAssistant) -> None:
        """Schedule or refresh the repair once Home Assistant has started."""
        if hass.state is not CoreState.running or not self._waiting_for_reports:
            self._async_clear_repair()
        elif self._repair_entity_ids:
            self._async_publish_repair()
        elif self._cancel_repair_check is None:
            self._cancel_repair_check = async_call_later(
                hass, _ENERGY_REPAIR_DELAY, self._async_create_repair
            )

    @callback
    def _async_create_repair(self, _now: object) -> None:
        """Publish the issue when the grace period expires while running."""
        self._cancel_repair_check = None
        if self.hass.state is CoreState.running:
            self._async_publish_repair()

    @callback
    def _async_publish_repair(self) -> None:
        """Create or update the issue when the list of blocking entities changes."""
        pending_entity_ids = self._pending_entity_ids
        if pending_entity_ids == self._repair_entity_ids:
            return
        self._repair_entity_ids = pending_entity_ids
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._repair_issue_id,
            config_entry_id=self._entry.entry_id,
            data={
                "entry_id": self._entry.entry_id,
                "group_id": self._group_id,
                "entity_ids": " ".join(pending_entity_ids),
            },
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PENDING_ENERGY_REPORTS,
            translation_placeholders={
                "group": self._group_name,
                "entities": ", ".join(pending_entity_ids),
            },
        )

    @callback
    def _async_clear_repair(self) -> None:
        """Cancel the grace period or remove the active repair issue."""
        if (cancel_check := self._cancel_repair_check) is not None:
            self._cancel_repair_check = None
            cancel_check()
        if self._repair_entity_ids:
            self._repair_entity_ids = ()
            ir.async_delete_issue(self.hass, DOMAIN, self._repair_issue_id)

    @callback
    def _async_remove_repair(self) -> None:
        """Remove the group's repair callback and issue when the sensor unloads."""
        self._storage.energy_recovery_callbacks.pop(self._group_id, None)
        self._async_clear_repair()

    @callback
    def _async_subscribe(self) -> None:
        """Watch changed states and repeated readings from the group's meters."""
        super()._async_subscribe()
        if self._is_difference:
            self._base_entity_ids = {
                entity_id
                for ref, entity_id in self._source_ids.items()
                if self._weights[ref] > 0
            }
        if self._source_ids:
            self._unsub_sources.append(
                async_track_state_report_event(
                    self.hass, self._source_ids.values(), self._async_state_reported
                )
            )

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Process changed readings, distinguishing the first report after an outage."""
        data = event.data
        self._async_source_update(
            data["entity_id"],
            data["new_state"],
            fresh=read_measurement(data["old_state"], SensorDeviceClass.ENERGY)
            is not None,
        )

    @callback
    def _async_state_reported(self, event: Event[EventStateReportedData]) -> None:
        """Accept an unchanged reading for a base update or recovery confirmation."""
        entity_id = event.data["entity_id"]
        if self._accumulator.pending_reports or entity_id in self._base_entity_ids:
            self._async_source_update(entity_id, event.data["new_state"], fresh=True)

    @callback
    def _async_source_update(
        self, entity_id: str, state: State | None, *, fresh: bool
    ) -> None:
        """Refresh the group and confirm a source only after a valid repeat report."""
        if self._waiting_for_history:
            return
        previous = self._attr_available, self._attr_native_value
        valid = read_measurement(state, SensorDeviceClass.ENERGY) is not None
        publish = not self._is_difference or (
            entity_id in self._base_entity_ids and valid
        )
        self._async_refresh(
            publish=publish, confirmed_entity_id=entity_id if fresh and valid else None
        )
        if publish or previous != (self._attr_available, self._attr_native_value):
            self.async_write_ha_state()

    @callback
    def _async_start_import(self, hass: HomeAssistant) -> None:
        """Schedule a Recorder import for this energy sensor."""
        self._history_task = self._entry.async_create_task(
            hass, self._async_import_history(), "import Electric Groups history"
        )

    @callback
    def _async_cancel_import(self) -> None:
        """Cancel queued import work and retries when the sensor goes away."""
        if self._cancel_retry is not None:
            self._cancel_retry()
        if self._history_task is not None:
            self._history_task.cancel()

    async def _async_import_history(self) -> None:
        """Save the import checkpoint, write history and enable live energy."""
        entity_id = self.entity_id
        if not is_entity_recorded(self.hass, entity_id):
            _LOGGER.warning(
                "Skipping statistics for %s because Recorder excludes it", entity_id
            )
            self._accumulator.history_imported = True
        else:
            recorder = get_instance(self.hass)
            try:
                pending = self._storage.states.get(self._group_id)
                history = await async_recorder_job(
                    recorder,
                    partial(
                        prepare_history,
                        entity_id=entity_id,
                        source_ids=self._source_ids.copy(),
                        weights=self._weights,
                        pending=pending,
                    ),
                )
                if history is not None:
                    pending = history.state
                    # Retain the exact checkpoints if Home Assistant stops between
                    # the Recorder commit and publishing the group's first state.
                    self._storage.states[self._group_id] = pending
                    await self._storage.async_save()
                    await async_recorder_job(
                        recorder,
                        partial(write_history, entity_id=entity_id, history=history),
                    )
                assert pending is not None
                self._accumulator.restore(pending, self._weights)
                self._accumulator.history_imported = True
            except (HomeAssistantError, SQLAlchemyError, ValueError) as err:
                _LOGGER.warning(
                    "Energy statistics import for %s failed. Retrying in %s s: %s",
                    entity_id,
                    _HISTORY_RETRY_DELAY,
                    err,
                )
                self._cancel_retry = async_call_later(
                    self.hass, _HISTORY_RETRY_DELAY, self._async_retry_import
                )
                return

        self._waiting_for_history = False
        self._async_refresh(publish=True)
        self._storage.async_update(self._group_id, self._accumulator.snapshot())
        self.async_write_ha_state()

    @callback
    def _async_retry_import(self, _now: object) -> None:
        """Retry a failed Recorder import while the energy sensor is loaded."""
        self._cancel_retry = None
        self._async_start_import(self.hass)

    @callback
    def _async_refresh(
        self,
        *,
        publish: bool = False,
        confirmed_entity_id: str | None = None,
        accept_current_readings: bool = False,
    ) -> None:
        """Track trusted meter deltas and wait for fresh readings in strict mode.

        Keep the last reliable reading for an unconfirmed source so a stale
        value after reconnecting cannot be mistaken for a meter reset.
        Repair can accept current readings when every energy source is available.
        """
        if self._waiting_for_history:
            return
        readings: dict[str, tuple[Reading, bool]] = {}
        confirmed_ref = None
        for ref in self._weights:
            state = self._async_source_state(ref)
            if (
                state is None
                or (reading := read_measurement(state, SensorDeviceClass.ENERGY))
                is None
            ):
                continue
            increasing = (
                state.attributes.get(ATTR_STATE_CLASS)
                == SensorStateClass.TOTAL_INCREASING
            )
            readings[ref] = reading, increasing
            if (
                confirmed_entity_id is not None
                and self._source_ids.get(ref) == confirmed_entity_id
            ):
                confirmed_ref = ref

        unavailable = self._weights.keys() - readings.keys()
        require_all = self._require_all_sources
        was_waiting = self._accumulator.all_sources_available is False or bool(
            self._accumulator.pending_reports
        )
        changed = self._accumulator.update_availability(
            self._weights,
            unavailable,
            require_all=require_all,
            confirmed_ref=confirmed_ref,
            accept_current_readings=accept_current_readings,
        )
        for ref, (reading, increasing) in readings.items():
            if ref not in self._accumulator.pending_reports:
                changed |= self._accumulator.update(
                    ref, reading, self._weights[ref], increasing
                )
        value = self._accumulator.total if self._accumulator.meters else None
        can_publish = not require_all or (
            not unavailable and not self._accumulator.pending_reports
        )
        if self._is_difference:
            previous = self._accumulator.published_total
            if (
                can_publish
                and value is not None
                and (publish or unavailable or was_waiting or previous is None)
            ):
                self._accumulator.published_total = value
                changed |= value != previous
            value = self._accumulator.published_total

        if changed:
            self._storage.async_update(self._group_id, self._accumulator.snapshot())

        self._attr_available = bool(self._weights) and can_publish and value is not None
        self._attr_native_value = value if self._attr_available else None
        self._async_update_repair(self.hass)


class GroupPowerSensor(GroupSensor):
    """Add or subtract available power readings in watts."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @callback
    def _async_refresh(self) -> None:
        """Apply the availability setting while still requiring a difference base."""
        values = []
        has_base_reading = False
        for ref, weight in self._weights.items():
            reading = read_measurement(
                self._async_source_state(ref), SensorDeviceClass.POWER
            )
            if reading is None:
                continue
            has_base_reading |= weight > 0
            values.append(weight * reading.value)
        self._attr_available = (
            has_base_reading if self._is_difference else bool(self._weights)
        ) and (not self._require_all_sources or len(values) == len(self._weights))
        self._attr_native_value = fsum(values) if self._attr_available else None
