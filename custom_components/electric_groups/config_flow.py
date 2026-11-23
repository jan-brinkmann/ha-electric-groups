"""Configure one Electric Groups integration and its group subentries."""

from collections.abc import Mapping
from typing import Any, ClassVar

import probatio as prob
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    EntitySelector,
    TextSelector,
)

from .const import (
    CONF_BASE_DEVICE,
    CONF_DEVICES,
    CONF_ENERGY_ENTITIES,
    CONF_IMPORT_HISTORY,
    CONF_POWER_ENTITIES,
    CONF_REQUIRE_ALL_SOURCES_DEFAULT,
    CONF_SOURCES,
    DOMAIN,
    GROUP_DIFFERENCE,
    GROUP_SUM,
    MEASUREMENTS,
    NAME,
)
from .sources import SourceWeights, device_sources, entity_reference, source_class

ENTITY_FIELDS = {
    CONF_ENERGY_ENTITIES: SensorDeviceClass.ENERGY,
    CONF_POWER_ENTITIES: SensorDeviceClass.POWER,
}
SENSOR_FILTERS = [
    {"domain": "sensor", "device_class": device_class} for device_class in MEASUREMENTS
]


class ElectricGroupsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the integration entry that owns all virtual devices."""

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose a creation button for each group type."""
        return {GROUP_SUM: SumGroupFlow, GROUP_DIFFERENCE: DifferenceGroupFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of the integration."""
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="user")


class GroupFlow(ConfigSubentryFlow):
    """Select devices and individual sensors for a group."""

    group_type: ClassVar[str]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a group that requires all sources and can import history once."""
        return self._async_form("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an existing group's name or members."""
        return self._async_form(
            "reconfigure", user_input, self._get_reconfigure_subentry()
        )

    @callback
    def _async_form(
        self,
        step: str,
        user_input: dict[str, Any] | None,
        subentry: ConfigSubentry | None = None,
    ) -> SubentryFlowResult:
        """Validate group selections and assign the availability default on creation."""
        errors: dict[str, str] = {}
        previous = subentry.data if subentry is not None else {}
        if user_input is not None:
            data = self._async_normalize_selection(user_input)
            data[CONF_NAME] = user_input[CONF_NAME].strip()
            data[CONF_IMPORT_HISTORY] = (
                user_input.get(CONF_IMPORT_HISTORY, False)
                if subentry is None
                else previous[CONF_IMPORT_HISTORY]
            )
            if not data[CONF_NAME]:
                errors[CONF_NAME] = "empty_name"
            sources = self._async_build_sources(data, errors)
            if subentry is None and data[CONF_IMPORT_HISTORY]:
                if not sources[SensorDeviceClass.ENERGY]:
                    errors[CONF_IMPORT_HISTORY] = "history_requires_energy"
                elif RECORDER_DOMAIN not in self.hass.config.components:
                    errors[CONF_IMPORT_HISTORY] = "history_requires_recorder"
            if (
                not errors
                and subentry is not None
                and self._async_has_cycle(subentry, sources)
            ):
                errors["base"] = "cyclic_group"
            if not errors:
                data[CONF_SOURCES] = sources
                data[CONF_REQUIRE_ALL_SOURCES_DEFAULT] = (
                    subentry is None
                    or previous.get(CONF_REQUIRE_ALL_SOURCES_DEFAULT, False)
                )
                if subentry is None:
                    return self.async_create_entry(title=data[CONF_NAME], data=data)
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=data[CONF_NAME], data=data
                )

        return self.async_show_form(
            step_id=step,
            data_schema=self._async_schema(
                user_input if user_input is not None else previous, subentry is None
            ),
            errors=errors,
        )

    @callback
    def _async_schema(self, existing: Mapping[str, Any], creating: bool) -> prob.Schema:
        """Render native device and entity pickers with current entity IDs."""
        registry = er.async_get(self.hass)
        suggested = dict(existing)
        for key in ENTITY_FIELDS:
            if key in suggested:
                suggested[key] = [
                    resolved
                    for ref in suggested[key]
                    if (resolved := er.async_resolve_entity_id(registry, ref))
                    is not None
                ]
        schema = {prob.Required(CONF_NAME): TextSelector()}
        if self.group_type == GROUP_DIFFERENCE:
            schema[prob.Required(CONF_BASE_DEVICE)] = DeviceSelector(
                {
                    "entity": {
                        "domain": "sensor",
                        "device_class": SensorDeviceClass.ENERGY,
                    }
                }
            )
        schema[prob.Optional(CONF_DEVICES)] = DeviceSelector(
            {"entity": SENSOR_FILTERS, "multiple": True}
        )
        for key, device_class in ENTITY_FIELDS.items():
            schema[prob.Optional(key)] = EntitySelector(
                {
                    "filter": {"domain": "sensor", "device_class": device_class},
                    "multiple": True,
                }
            )
        if creating:
            schema[prob.Optional(CONF_IMPORT_HISTORY, default=False)] = (
                BooleanSelector()
            )
        return self.add_suggested_values_to_schema(
            prob.Schema(schema),
            {key: value for key, value in suggested.items() if value is not None},
        )

    @callback
    def _async_normalize_selection(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Save entity selections by registry ID so they survive entity renames."""
        registry = er.async_get(self.hass)
        data = {CONF_DEVICES: list(dict.fromkeys(user_input.get(CONF_DEVICES, [])))}
        for key in ENTITY_FIELDS:
            data[key] = list(
                dict.fromkeys(
                    entity_reference(
                        registry, er.async_resolve_entity_id(registry, ref) or ref
                    )
                    for ref in user_input.get(key, [])
                )
            )
        if self.group_type == GROUP_DIFFERENCE:
            data[CONF_BASE_DEVICE] = user_input[CONF_BASE_DEVICE]
        return data

    @callback
    def _async_build_sources(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> dict[SensorDeviceClass, SourceWeights]:
        """Resolve group sources and require exactly one energy sensor at the base."""
        registry = er.async_get(self.hass)
        members = {device_class: set() for device_class in MEASUREMENTS}
        base = {device_class: set() for device_class in MEASUREMENTS}
        for device_id in data[CONF_DEVICES]:
            resolved = device_sources(self.hass, device_id)
            if not any(resolved.values()):
                errors[CONF_DEVICES] = "device_without_sensors"
            for device_class, refs in resolved.items():
                members[device_class].update(refs)
        for key, device_class in ENTITY_FIELDS.items():
            for ref in data[key]:
                entity_id = er.async_resolve_entity_id(registry, ref)
                if (
                    entity_id is None
                    or source_class(self.hass, entity_id) != device_class
                ):
                    errors[key] = "invalid_sensor"
                else:
                    members[device_class].add(ref)
        member_weight = 1
        if self.group_type == GROUP_DIFFERENCE:
            member_weight = -1
            base = device_sources(self.hass, data[CONF_BASE_DEVICE])
            if len(base[SensorDeviceClass.ENERGY]) != 1:
                errors[CONF_BASE_DEVICE] = "base_device_requires_one_energy_sensor"
            if not base[SensorDeviceClass.POWER]:
                members[SensorDeviceClass.POWER].clear()
            if any(members[kind] & base[kind] for kind in MEASUREMENTS):
                errors["base"] = "overlapping_base"
        if not any(members.values()):
            errors["base"] = "select_members"

        return {
            kind: dict.fromkeys(sorted(base[kind]), 1)
            | dict.fromkeys(sorted(members[kind]), member_weight)
            for kind in MEASUREMENTS
        }

    @callback
    def _async_has_cycle(
        self, subentry: ConfigSubentry, sources: dict[SensorDeviceClass, SourceWeights]
    ) -> bool:
        """Check each measurement's graph for a path back to the edited group."""
        registry = er.async_get(self.hass)
        subentries = self._get_entry().subentries
        for kind, weights in sources.items():
            remaining = list(weights)
            visited: set[str] = set()
            while remaining:
                ref = remaining.pop()
                entity_id = er.async_resolve_entity_id(registry, ref)
                entity = (
                    registry.async_get(entity_id) if entity_id is not None else None
                )
                if entity is None or entity.platform != DOMAIN:
                    continue
                group_id = entity.config_subentry_id
                if group_id == subentry.subentry_id:
                    return True
                if (
                    group_id is not None
                    and group_id in subentries
                    and group_id not in visited
                ):
                    visited.add(group_id)
                    remaining.extend(subentries[group_id].data[CONF_SOURCES][kind])
        return False


class SumGroupFlow(GroupFlow):
    """Configure a group that adds its inputs."""

    group_type = GROUP_SUM


class DifferenceGroupFlow(GroupFlow):
    """Configure a group that subtracts members from a base."""

    group_type = GROUP_DIFFERENCE
