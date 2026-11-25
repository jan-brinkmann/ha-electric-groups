"""Expose a group's configured members to its Home Assistant panel."""

from pathlib import Path
from typing import Any

import probatio as prob
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BASE_DEVICE,
    CONF_DEVICES,
    CONF_SOURCES,
    DOMAIN,
    MEMBERS_PANEL_PATH,
)

_MODULE_URL = "/electric_groups_static/members.js"
_PANEL_COMPONENT = "electric-groups-members-panel"


async def async_register_members_panel(hass: HomeAssistant, _component: str) -> None:
    """Serve the panel module and register its admin-only route and data command."""
    module = Path(__file__).with_name("frontend") / "members.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_MODULE_URL, str(module), False)]
    )
    websocket_api.async_register_command(hass, _async_websocket_members)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=MEMBERS_PANEL_PATH,
        webcomponent_name=_PANEL_COMPONENT,
        module_url=_MODULE_URL,
        require_admin=True,
    )


def _device_details(device: dr.AnyDeviceEntry) -> dict[str, str]:
    """Return the target ID and current display name for a device link."""
    return {"id": device.id, "name": device.name_by_user or device.name or device.id}


@callback
def _async_members(
    hass: HomeAssistant, group: ConfigSubentry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Resolve selected devices and sensor sources against the registries."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    member_ids = set(group.data.get(CONF_DEVICES, ()))
    base_id = group.data.get(CONF_BASE_DEVICE)
    unassigned: set[str] = set()

    for weights in group.data[CONF_SOURCES].values():
        for ref, weight in weights.items():
            if base_id is not None and weight > 0:
                continue
            if (entity_id := er.async_resolve_entity_id(entities, ref)) is None:
                continue
            entity = entities.async_get(entity_id)
            if entity is not None and entity.device_id is not None:
                member_ids.add(entity.device_id)
            else:
                unassigned.add(entity_id)

    members = [
        _device_details(member)
        for device_id in member_ids - {base_id, device.id}
        if (member := devices.async_get(device_id, include_composite_devices=False))
        is not None
    ]
    base = (
        devices.async_get(base_id, include_composite_devices=False)
        if base_id is not None
        else None
    )
    return {
        "group": _device_details(device),
        "base": _device_details(base) if base is not None else None,
        "members": sorted(
            members, key=lambda member: (member["name"].casefold(), member["id"])
        ),
        "entities": sorted(unassigned),
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {"type": f"{DOMAIN}/members", prob.Required("group_id"): str}
)
@callback
def _async_websocket_members(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the group and linkable members, or an error for an unknown group."""
    group_id = msg["group_id"]
    devices = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        group = entry.subentries.get(group_id)
        if group is None:
            continue
        device = devices.async_get_device_by_identifier(
            (DOMAIN, group_id), entry.entry_id
        )
        if device is not None:
            connection.send_result(msg["id"], _async_members(hass, group, device))
            return
        break
    connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Group not found")
