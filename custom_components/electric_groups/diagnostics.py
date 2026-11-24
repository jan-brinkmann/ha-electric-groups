"""Export group source availability through Home Assistant's diagnostics panel."""

from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import ElectricGroupsEntry
from .const import CONF_REQUIRE_ALL_SOURCES_DEFAULT, CONF_SOURCES, DOMAIN, MEASUREMENTS
from .source_status import SourceStatus


def _group_diagnostics(
    entry: ElectricGroupsEntry, group: ConfigSubentry
) -> dict[str, Any]:
    """Report both measurements using anonymous labels for source entities."""
    storage = getattr(entry, "runtime_data", None)
    default_mode = group.data.get(CONF_REQUIRE_ALL_SOURCES_DEFAULT, False)
    data: dict[str, Any] = {
        "group_type": group.subentry_type,
        "require_all_sources": (
            storage.require_all_sources.get(group.subentry_id, default_mode)
            if storage is not None
            else default_mode
        ),
    }
    for kind in MEASUREMENTS:
        status = (
            storage.source_statuses.get((group.subentry_id, kind))
            if storage is not None
            else None
        )
        if status is None:
            status = SourceStatus.untracked(len(group.data[CONF_SOURCES][kind]))
        attributes = status.attributes()
        entities = sorted(set(status.missing_sources) | set(status.pending_reports))
        aliases = {
            entity: f"source_{index}" for index, entity in enumerate(entities, 1)
        }
        for key in ("missing_sources", "pending_reports"):
            if key in attributes:
                attributes[key] = [aliases[entity] for entity in attributes[key]]
        data[kind.value] = {"status": status.state.value, **attributes}
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElectricGroupsEntry
) -> dict[str, Any]:
    """Return the current source status of every group in the integration."""
    return {
        "groups": [
            _group_diagnostics(entry, group) for group in entry.subentries.values()
        ]
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ElectricGroupsEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return source status for the requested virtual group device."""
    for domain, group_id in device.identifiers:
        if domain == DOMAIN and (group := entry.subentries.get(group_id)) is not None:
            return _group_diagnostics(entry, group)
    return {}
