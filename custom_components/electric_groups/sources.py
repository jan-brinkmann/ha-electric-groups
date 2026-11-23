"""Resolve group selections and normalize meter readings."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from homeassistant.components.sensor import (
    ATTR_LAST_RESET,
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State, callback, split_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import MEASUREMENTS

type SourceWeights = dict[str, int]


@dataclass(frozen=True)
class Reading:
    """A meter value in the group unit and its optional reset timestamp."""

    value: float
    last_reset: float | None = None


@callback
def source_class(hass: HomeAssistant, entity_id: str) -> SensorDeviceClass | None:
    """Return the supported measurement class, including for offline sensors."""
    if split_entity_id(entity_id)[0] != SENSOR_DOMAIN:
        return None
    state = hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.disabled:
        return None
    device_class = attributes.get(ATTR_DEVICE_CLASS)
    unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if entry is not None:
        device_class = device_class or entry.device_class or entry.original_device_class
        unit = unit or entry.unit_of_measurement
    if not isinstance(device_class, str) or device_class not in MEASUREMENTS:
        return None
    kind = SensorDeviceClass(device_class)
    if not isinstance(unit, str) or unit not in MEASUREMENTS[kind][0].VALID_UNITS:
        return None
    if kind == SensorDeviceClass.ENERGY and attributes.get(ATTR_STATE_CLASS) not in (
        None,
        SensorStateClass.TOTAL,
        SensorStateClass.TOTAL_INCREASING,
    ):
        return None
    return kind


@callback
def entity_reference(registry: er.EntityRegistry, entity_id: str) -> str:
    """Use a stable registry ID when the entity has one."""
    entry = registry.async_get(entity_id)
    return entry.id if entry is not None else entity_id


@callback
def device_sources(
    hass: HomeAssistant, device_id: str
) -> dict[SensorDeviceClass, set[str]]:
    """Find all enabled energy and power sensors on a device."""
    registry = er.async_get(hass)
    sources = {device_class: set() for device_class in MEASUREMENTS}
    for entry in er.async_entries_for_device(registry, device_id):
        if (device_class := source_class(hass, entry.entity_id)) is not None:
            sources[device_class].add(entry.id)
    return sources


def read_measurement(
    state: State | None, device_class: SensorDeviceClass
) -> Reading | None:
    """Convert a usable reading to kWh or W and return None otherwise."""
    if state is None:
        return None
    converter, target_unit = MEASUREMENTS[device_class]
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if not isinstance(unit, str) or unit not in converter.VALID_UNITS:
        return None
    try:
        value = converter.convert(float(state.state), unit, target_unit)
    except ValueError:
        return None
    if not isfinite(value):
        return None

    reset = state.attributes.get(ATTR_LAST_RESET)
    if isinstance(reset, str):
        reset = dt_util.parse_datetime(reset)
    return Reading(
        value,
        dt_util.as_utc(reset).timestamp() if isinstance(reset, datetime) else None,
    )
