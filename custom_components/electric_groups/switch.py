"""Control whether a group requires every source for each measurement."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ElectricGroupsEntry, GroupStorage, availability_signal, group_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricGroupsEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an availability switch to each group's virtual device."""
    for group in entry.subentries.values():
        async_add_entities(
            [RequireAllSourcesSwitch(group, entry.runtime_data)],
            config_subentry_id=group.subentry_id,
        )


class RequireAllSourcesSwitch(SwitchEntity):
    """Require complete energy and power readings for their respective sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "require_all_measurement_sources"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, group: ConfigSubentry, storage: GroupStorage) -> None:
        """Bind the switch and its persisted setting to a single group."""
        self._group_id = group.subentry_id
        self._storage = storage
        self._attr_is_on = storage.require_all_sources.get(self._group_id, False)
        # Keep the registry entry of the original energy-only switch.
        self._attr_unique_id = f"{self._group_id}_require_all_energy_sources"
        self._attr_device_info = group_device_info(group)

    async def async_added_to_hass(self) -> None:
        """Publish setting changes before waiting on storage writes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                availability_signal(self._group_id),
                self._async_setting_changed,
            )
        )

    @callback
    def _async_setting_changed(self) -> None:
        """Reflect the group's current setting in the switch state."""
        self._attr_is_on = self._storage.require_all_sources.get(self._group_id, False)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Require all configured sources for each measurement."""
        await self._storage.async_set_source_availability(self._group_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Allow each sensor to operate with its available sources."""
        await self._storage.async_set_source_availability(self._group_id, False)
