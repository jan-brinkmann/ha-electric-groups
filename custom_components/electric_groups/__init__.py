"""Provide virtual electricity meters configured as Home Assistant subentries."""

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import (
    async_invalidate_translations,
    async_load_integrations,
)

from .const import CONF_REQUIRE_ALL_SOURCES_DEFAULT, DOMAIN, GROUP_DIFFERENCE, NAME
from .energy import EnergyState

_PLATFORMS = [Platform.SENSOR, Platform.SWITCH]
_SAVE_DELAY = 10


@dataclass
class GroupStorage:
    """Share meter checkpoints, availability settings, and live repair callbacks."""

    store: Store[dict[str, EnergyState]]
    states: dict[str, EnergyState]
    availability_store: Store[dict[str, bool]]
    require_all_sources: dict[str, bool]
    energy_recovery_callbacks: dict[str, Callable[[], bool]] = field(
        default_factory=dict
    )

    @callback
    def async_update(self, group_id: str, state: EnergyState) -> None:
        """Schedule a write of the current meter values and their group total."""
        self.states[group_id] = state
        self.store.async_delay_save(self.states.copy, _SAVE_DELAY)

    async def async_save(self) -> None:
        """Flush current checkpoints before unloading or committing history."""
        await self.store.async_save(self.states.copy())

    async def async_set_source_availability(
        self, group_id: str, require_all: bool
    ) -> None:
        """Apply and save the switch setting independently of meter checkpoints."""
        self._async_apply_source_availability(group_id, require_all)
        await self.async_save_availability()

    @callback
    def async_cycle_source_availability(self, group_id: str) -> None:
        """Disable strict mode for one refresh and restore it before yielding."""
        original_mode = self.require_all_sources[group_id]
        try:
            self._async_apply_source_availability(group_id, False)
        finally:
            self._async_apply_source_availability(group_id, original_mode)

    @callback
    def _async_apply_source_availability(
        self, group_id: str, require_all: bool
    ) -> None:
        """Apply the switch setting in memory and refresh the group's entities."""
        self.require_all_sources[group_id] = require_all
        async_dispatcher_send(self.store.hass, availability_signal(group_id))

    async def async_save_availability(self) -> None:
        """Flush settings before a reload or after a switch change."""
        await self.availability_store.async_save(self.require_all_sources.copy())


type ElectricGroupsEntry = ConfigEntry[GroupStorage]


def availability_signal(group_id: str) -> str:
    """Return the signal sent when the group's availability setting changes."""
    return f"{DOMAIN}_source_availability_{group_id}"


def group_device_info(group: ConfigSubentry) -> DeviceInfo:
    """Identify the group device shared by its sensors and switch."""
    return DeviceInfo(
        identifiers={(DOMAIN, group.subentry_id)},
        name=group.title,
        manufacturer=NAME,
        model=(
            "Electrical difference group"
            if group.subentry_type == GROUP_DIFFERENCE
            else "Electrical sum group"
        ),
        entry_type=DeviceEntryType.SERVICE,
    )


def _group_store(
    hass: HomeAssistant, entry: ConfigEntry
) -> Store[dict[str, EnergyState]]:
    """Open the storage file belonging to this integration entry."""
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)


def _availability_store(
    hass: HomeAssistant, entry: ConfigEntry
) -> Store[dict[str, bool]]:
    """Open settings storage separately from Recorder import checkpoints."""
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.availability", atomic_writes=True)


async def async_setup_entry(hass: HomeAssistant, entry: ElectricGroupsEntry) -> bool:
    """Refresh translations and load each group's checkpoints and settings."""
    async_invalidate_translations(hass, {DOMAIN})
    await async_load_integrations(hass, {DOMAIN})
    store = _group_store(hass, entry)
    modes_store = _availability_store(hass, entry)
    saved = await store.async_load() or {}
    saved_modes = await modes_store.async_load() or {}
    states = {
        group_id: state
        for group_id, state in saved.items()
        if group_id in entry.subentries
    }
    modes = {
        group_id: saved_modes.get(
            group_id, group.data.get(CONF_REQUIRE_ALL_SOURCES_DEFAULT, False)
        )
        for group_id, group in entry.subentries.items()
    }
    entry.runtime_data = GroupStorage(store, states, modes_store, modes)
    if states != saved:
        await entry.runtime_data.async_save()
    if modes != saved_modes:
        await entry.runtime_data.async_save_availability()
    entry.async_on_unload(entry.add_update_listener(_async_groups_updated))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def _async_groups_updated(
    hass: HomeAssistant, entry: ElectricGroupsEntry
) -> None:
    """Refresh virtual devices after a subentry was added, edited, or removed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ElectricGroupsEntry) -> bool:
    """Unload the platforms and persist their meter checkpoints and settings."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    await entry.runtime_data.async_save()
    if entry.runtime_data.require_all_sources:
        await entry.runtime_data.async_save_availability()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ElectricGroupsEntry) -> None:
    """Remove stored checkpoints and settings for a deleted integration entry."""
    await _group_store(hass, entry).async_remove()
    await _availability_store(hass, entry).async_remove()
