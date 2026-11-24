"""Exercise group configuration, live readings, and the Recorder handoff."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.repairs import DOMAIN as REPAIRS_DOMAIN
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
    async_invalidate_translations,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)
from pytest_homeassistant.typing import ClientSessionGenerator
from sqlalchemy.exc import SQLAlchemyError

from custom_components.electric_groups import sensor
from custom_components.electric_groups.const import (
    CONF_BASE_DEVICE,
    CONF_DEVICES,
    CONF_ENERGY_ENTITIES,
    CONF_IMPORT_HISTORY,
    CONF_POWER_ENTITIES,
    CONF_SOURCES,
    DOMAIN,
    GROUP_DIFFERENCE,
    GROUP_SUM,
    ISSUE_PENDING_ENERGY_REPORTS,
)
from custom_components.electric_groups.statistics import History, write_history


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Expose the workspace integration in Home Assistant's temporary configuration."""
    path = Path(hass_tmp_config_dir)
    (path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components",
        target_is_directory=True,
    )
    return str(path)


async def create_integration(hass: HomeAssistant) -> ConfigEntry:
    """Set up Electric Groups through its public config flow and return the entry."""
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert form["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def register_meter_device(hass: HomeAssistant, name: str, *sensors: str) -> str:
    """Register a metering device and its sensor object IDs, then return its ID."""
    source_entry = MockConfigEntry(domain="demo")
    source_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id, identifiers={("demo", name)}, name=name
    )
    registry = er.async_get(hass)
    for object_id in sensors:
        registry.async_get_or_create(
            "sensor",
            "demo",
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
        )
    return device.id


async def submit_group(
    hass: HomeAssistant, entry: ConfigEntry, group_type: str, data: dict
):
    """Submit member selections to a new native group subentry flow."""
    manager = hass.config_entries.subentries
    form = await manager.async_init(
        (entry.entry_id, group_type), context={"source": "user"}
    )
    assert form["type"] is FlowResultType.FORM
    result = await manager.async_configure(form["flow_id"], data)
    await hass.async_block_till_done()
    return result


def set_energy(
    hass: HomeAssistant,
    entity_id: str,
    value: float | str,
    unit=UnitOfEnergy.KILO_WATT_HOUR,
) -> None:
    """Publish a cumulative energy meter reading in its original unit."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
            ATTR_UNIT_OF_MEASUREMENT: unit,
            "state_class": SensorStateClass.TOTAL_INCREASING,
        },
    )


def set_power(
    hass: HomeAssistant, entity_id: str, value: float | str, unit=UnitOfPower.WATT
) -> None:
    """Publish an instantaneous power reading in its original unit."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.POWER,
            ATTR_UNIT_OF_MEASUREMENT: unit,
            "state_class": SensorStateClass.MEASUREMENT,
        },
    )


def assert_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Compare an emitted numeric state to its expected value."""
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(value)


def assert_unavailable(hass: HomeAssistant, entity_id: str) -> None:
    """Assert the registered entity publishes the unavailable state."""
    state = hass.states.get(entity_id)
    assert state is not None and state.state == STATE_UNAVAILABLE


async def test_sum_group_device_and_entity_selection(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Create a virtual device and deduplicate a device sensor selected explicitly."""
    entry = await create_integration(hass)
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert duplicate["type"] is FlowResultType.ABORT

    source_entry = MockConfigEntry(domain="demo")
    source_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("demo", "plug")},
        name="Plug",
    )
    meter = registry.async_get_or_create(
        "sensor",
        "demo",
        "plug_meter",
        device_id=device.id,
        suggested_object_id="plug_meter",
    )
    power = registry.async_get_or_create(
        "sensor",
        "demo",
        "plug_power",
        device_id=device.id,
        suggested_object_id="plug_power",
    )
    set_energy(hass, meter.entity_id, 10000, UnitOfEnergy.WATT_HOUR)
    set_energy(hass, "sensor.lamp_energy", 5)
    set_power(hass, power.entity_id, 0.2, UnitOfPower.KILO_WATT)
    set_power(hass, "sensor.lamp_power", 25)

    result = await submit_group(
        hass,
        entry,
        GROUP_SUM,
        {
            CONF_NAME: "Lights",
            CONF_DEVICES: [device.id],
            CONF_ENERGY_ENTITIES: [meter.entity_id, "sensor.lamp_energy"],
            CONF_POWER_ENTITIES: ["sensor.lamp_power"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    energy = registry.async_get("sensor.lights_energy")
    group_power = registry.async_get("sensor.lights_power")
    switch = registry.async_get("switch.lights_require_all_entities")
    assert energy is not None and group_power is not None and switch is not None
    assert (
        energy.device_id is not None
        and energy.device_id == group_power.device_id == switch.device_id
    )
    assert (
        energy.config_subentry_id
        == group_power.config_subentry_id
        == switch.config_subentry_id
        == subentry.subentry_id
    )
    assert_value(hass, energy.entity_id, 0)
    assert_value(hass, group_power.entity_id, 225)
    switch_state = hass.states.get(switch.entity_id)
    assert switch_state is not None and switch_state.state == STATE_ON
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch.entity_id}, blocking=True
    )

    renamed = registry.async_update_entity(
        meter.entity_id, new_entity_id="sensor.renamed_meter"
    )
    set_energy(hass, renamed.entity_id, 11000, UnitOfEnergy.WATT_HOUR)
    set_energy(hass, "sensor.lamp_energy", 6)
    set_power(hass, power.entity_id, STATE_UNAVAILABLE, UnitOfPower.KILO_WATT)
    await hass.async_block_till_done()
    assert_value(hass, energy.entity_id, 2)
    assert_value(hass, group_power.entity_id, 25)
    set_power(hass, "sensor.lamp_power", STATE_UNAVAILABLE)
    assert_value(hass, group_power.entity_id, 0)
    set_power(hass, power.entity_id, 0.2, UnitOfPower.KILO_WATT)
    assert_value(hass, group_power.entity_id, 200)

    # Removing the group also removes its virtual device and entities.
    assert hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    assert not registry.async_get(energy.entity_id)
    assert not registry.async_get(switch.entity_id)
    assert not devices.async_get(energy.device_id)


async def test_existing_sum_group_gets_availability_switch(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Keep the legacy default and wait for fresh energy readings when enabled."""
    entry = await create_integration(hass)
    set_energy(hass, "sensor.light_energy", 10)
    set_energy(hass, "sensor.plug_energy", 5)
    set_power(hass, "sensor.light_power", 100)
    set_power(hass, "sensor.plug_power", 50)
    legacy_group = ConfigSubentry(
        subentry_type=GROUP_SUM,
        title="Kitchen",
        unique_id=None,
        data={
            CONF_NAME: "Kitchen",
            CONF_IMPORT_HISTORY: False,
            CONF_SOURCES: {
                SensorDeviceClass.ENERGY: {
                    "sensor.light_energy": 1,
                    "sensor.plug_energy": 1,
                },
                SensorDeviceClass.POWER: {
                    "sensor.light_power": 1,
                    "sensor.plug_power": 1,
                },
            },
        },
    )
    with patch("custom_components.electric_groups.switch.async_setup_entry"):
        assert hass.config_entries.async_add_subentry(entry, legacy_group)
        await hass.async_block_till_done()
    switch_id = "switch.kitchen_require_all_entities"
    energy_id, power_id = "sensor.kitchen_energy", "sensor.kitchen_power"
    assert hass.states.get(switch_id) is None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(switch_id)
    assert state is not None and state.state == STATE_OFF

    set_power(hass, "sensor.plug_power", STATE_UNAVAILABLE)
    assert_value(hass, power_id, 100)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert_unavailable(hass, power_id)
    assert_value(hass, energy_id, 0)

    set_energy(hass, "sensor.plug_energy", STATE_UNAVAILABLE)
    set_energy(hass, "sensor.light_energy", 12)
    set_power(hass, "sensor.plug_power", 40)
    assert_value(hass, power_id, 140)
    assert_unavailable(hass, energy_id)
    # A cached lower reading on reconnection must not count as a meter reset.
    set_energy(hass, "sensor.plug_energy", 4)
    assert_unavailable(hass, energy_id)
    set_energy(hass, "sensor.light_energy", 12)
    assert_unavailable(hass, energy_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_unavailable(hass, energy_id)
    set_energy(hass, "sensor.plug_energy", 6)
    assert_value(hass, energy_id, 3)

    set_energy(hass, "sensor.plug_energy", STATE_UNAVAILABLE)
    set_power(hass, "sensor.plug_power", STATE_UNAVAILABLE)
    set_energy(hass, "sensor.light_energy", 13)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert_value(hass, energy_id, 4)
    assert_value(hass, power_id, 100)


@pytest.mark.parametrize(
    ("group_type", "total"), [(GROUP_SUM, 4), (GROUP_DIFFERENCE, 2)]
)
async def test_pending_energy_reports_repair(
    recorder_mock: Recorder, hass: HomeAssistant, group_type: str, total: float
) -> None:
    """Confirm energy recovery without publishing an incomplete power reading."""
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    entry = await create_integration(hass)
    base, member = "sensor.main_energy", "sensor.load_energy"
    base_power, member_power = "sensor.main_power", "sensor.load_power"
    idle = "sensor.idle_energy"
    load_device = register_meter_device(hass, "load", "load_energy")
    er.async_get(hass).async_update_entity(member, name="Meter [Office]")
    data = {
        CONF_NAME: "Office",
        CONF_POWER_ENTITIES: [member_power],
        CONF_ENERGY_ENTITIES: [member, idle, base]
        if group_type == GROUP_SUM
        else [member, idle],
    }
    if group_type == GROUP_DIFFERENCE:
        data[CONF_BASE_DEVICE] = register_meter_device(
            hass, "base", "main_energy", "main_power"
        )
        set_power(hass, base_power, 100)
    set_energy(hass, base, 100)
    set_energy(hass, member, 40)
    set_energy(hass, idle, 7)
    set_power(hass, member_power, 30)
    result = await submit_group(hass, entry, group_type, data)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    (group_id,) = entry.subentries
    issue_id = f"{ISSUE_PENDING_ENERGY_REPORTS}_{group_id}"
    issues = ir.async_get(hass)
    energy_id = "sensor.office_energy"
    energy_status_id = "sensor.office_energy_source_status"
    power_id = "sensor.office_power"
    switch_id = "switch.office_require_all_entities"
    assert_value(hass, power_id, 30 if group_type == GROUP_SUM else 70)
    set_power(hass, member_power, STATE_UNAVAILABLE)
    assert_unavailable(hass, power_id)

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, base, 103)
    assert_unavailable(hass, energy_id)
    status = hass.states.get(energy_status_id)
    assert status is not None and status.state == "missing_sources"
    assert status.attributes["missing_sources"] == [member]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    set_energy(hass, member, 41)
    assert_unavailable(hass, energy_id)
    status = hass.states.get(energy_status_id)
    assert status is not None and status.state == "waiting_for_reports"
    assert status.attributes["available_sources"] == 3
    assert status.attributes["pending_reports"] == [idle, member, base]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=58))
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders == {
        "group": "Office",
        "entities": f"{idle}, {member}, {base}",
    }

    set_energy(hass, base, 103)
    status = hass.states.get(energy_status_id)
    assert status is not None and status.state == "waiting_for_reports"
    assert status.attributes["pending_reports"] == [idle, member]
    updated_issue = issues.async_get_issue(DOMAIN, issue_id)
    assert updated_issue is not None and updated_issue.created == issue.created
    assert updated_issue.translation_placeholders == {
        "group": "Office",
        "entities": f"{idle}, {member}",
    }

    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    entity_links = (
        f"- `{idle}`\n"
        f"- [Meter \\[Office\\] (`{member}`)](/config/devices/device/{load_device})"
    )
    placeholders = {
        "group": "Office",
        "group_name": "Office",
        "entities": entity_links,
    }
    assert result["description_placeholders"] == placeholders
    flow_key = f"component.{DOMAIN}.issues.{updated_issue.translation_key}.fix_flow"
    prefix = f"{flow_key}.step.{result['step_id']}"
    for language, button in (("de", "Reparieren"), ("en", "Repair")):
        translations = await async_get_translations(hass, language, "issues", [DOMAIN])
        assert "Office" in translations[f"{prefix}.title"].format_map(placeholders)
        description = translations[f"{prefix}.description"].format_map(placeholders)
        assert entity_links in description
        assert "Office" in description
        assert translations[f"{prefix}.submit"] == button
    assert_unavailable(hass, energy_id)

    manager.async_abort(result["flow_id"])
    assert issues.async_get_issue(DOMAIN, issue_id) == updated_issue
    assert_unavailable(hass, energy_id)
    assert all(event.data["entity_id"] != switch_id for event in events)

    result = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description_placeholders"] == {"group_name": "Office"}
    for language, expected in (
        ("de", "Die Energie-Entität der Gruppe Office ist wieder verfügbar."),
        ("en", "The energy entity for the Office group is available again."),
    ):
        translations = await async_get_translations(hass, language, "issues", [DOMAIN])
        message = translations[f"{flow_key}.create_entry.default"].format_map(
            result["description_placeholders"]
        )
        assert message == expected
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert_value(hass, energy_id, total)
    status = hass.states.get(energy_status_id)
    assert status is not None and status.state == "complete"
    assert "pending_reports" not in status.attributes
    assert_unavailable(hass, power_id)
    assert all(event.data["entity_id"] not in {power_id, switch_id} for event in events)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_value(hass, energy_id, total)
    assert_unavailable(hass, power_id)
    switch = hass.states.get(switch_id)
    assert switch is not None and switch.state == STATE_ON
    set_energy(hass, member, STATE_UNAVAILABLE)
    assert_unavailable(hass, energy_id)


async def test_source_status_diagnostics(
    recorder_mock: Recorder, hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Show partial and missing readings and export status for the entry and device."""
    entry = await create_integration(hass)
    first, second = "sensor.first_power", "sensor.second_power"
    register_meter_device(hass, "second", "second_power")
    set_power(hass, first, 30)
    set_power(hass, second, STATE_UNAVAILABLE)
    result = await submit_group(
        hass,
        entry,
        GROUP_SUM,
        {CONF_NAME: "Kitchen", CONF_POWER_ENTITIES: [first, second]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    power = "sensor.kitchen_power"
    status_id = "sensor.kitchen_power_source_status"
    registry = er.async_get(hass)
    status_entry = registry.async_get(status_id)
    assert status_entry is not None
    assert status_entry.entity_category is EntityCategory.DIAGNOSTIC
    (group_id,) = entry.subentries
    assert status_entry.config_subentry_id == group_id
    assert status_entry.device_id is not None
    device = dr.async_get(hass).async_get(status_entry.device_id)
    assert device is not None and (DOMAIN, group_id) in device.identifiers

    assert_unavailable(hass, power)
    status = hass.states.get(status_id)
    assert status is not None and status.state == "missing_sources"
    assert status.attributes["missing_sources"] == [second]

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.kitchen_require_all_entities"},
        blocking=True,
    )
    assert_value(hass, power, 30)
    status = hass.states.get(status_id)
    assert status is not None and status.state == "partial"
    assert status.attributes["available_sources"] == 1
    assert status.attributes["total_sources"] == 2

    group_diagnostics = {
        "group_type": GROUP_SUM,
        "require_all_sources": False,
        "power": {
            "status": "partial",
            "available_sources": 1,
            "total_sources": 2,
            "missing_sources": ["source_1"],
        },
        "energy": {"status": "no_sources", "available_sources": 0, "total_sources": 0},
    }
    assert await get_diagnostics_for_config_entry(hass, hass_client, entry) == {
        "groups": [group_diagnostics]
    }
    assert await get_diagnostics_for_device(hass, hass_client, entry, device) == (
        group_diagnostics
    )

    registry.async_remove(second)
    set_power(hass, first, STATE_UNAVAILABLE)
    assert_value(hass, power, 0)
    status = hass.states.get(status_id)
    assert status is not None and status.state == "no_readings"
    assert status.attributes["available_sources"] == 0
    assert status.attributes["missing_sources"] == [first]
    assert status.attributes["unresolved_sources"] == 1


async def test_reload_refreshes_cached_repair_translations(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """After a reload, serve repair texts added since the cache was populated."""
    entry = await create_integration(hass)
    async_invalidate_translations(hass, {DOMAIN})
    with patch(
        "homeassistant.helpers.translation._load_translations_files_by_language",
        return_value={},
    ):
        assert await async_get_translations(hass, "de", "issues", [DOMAIN]) == {}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    translations = await async_get_translations(hass, "de", "issues", [DOMAIN])
    prefix = f"component.{DOMAIN}.issues.{ISSUE_PENDING_ENERGY_REPORTS}.fix_flow.step"
    assert "{entities}" in translations[f"{prefix}.confirm.description"]
    assert translations[f"{prefix}.confirm.submit"] == "Reparieren"


async def test_pending_energy_repair_waits_for_home_assistant_startup(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Restore a waiting group and publish its repair after startup."""
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    entry = await create_integration(hass)
    meter = "sensor.load_energy"
    set_energy(hass, meter, 10)
    result = await submit_group(
        hass, entry, GROUP_SUM, {CONF_NAME: "Office", CONF_ENERGY_ENTITIES: [meter]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (group_id,) = entry.subentries
    issue_id = f"{ISSUE_PENDING_ENERGY_REPORTS}_{group_id}"
    issues = ir.async_get(hass)

    set_energy(hass, meter, STATE_UNAVAILABLE)
    set_energy(hass, meter, 10)
    hass.set_state(CoreState.starting)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    prefix = f"component.{DOMAIN}.issues.{ISSUE_PENDING_ENERGY_REPORTS}.fix_flow.step"
    cached = async_get_cached_translations(hass, hass.config.language, "issues", DOMAIN)
    assert f"{prefix}.confirm.description" in cached

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert_unavailable(hass, "sensor.office_energy")
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    await hass.async_start()
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    manager = repairs_flow_manager(hass)
    assert manager is not None
    form = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    assert form["type"] is FlowResultType.FORM
    resources = await async_get_translations(hass, "de", "issues")
    description = resources[f"{prefix}.{form['step_id']}.description"].format_map(
        form["description_placeholders"]
    )
    assert meter in description
    assert "Office" in description
    manager.async_abort(form["flow_id"])


async def test_pending_energy_reports_repair_cleanup(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Clear resolved issues and reject confirmation of a superseded outage."""
    assert await async_setup_component(hass, REPAIRS_DOMAIN, {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    entry = await create_integration(hass)
    member = "sensor.load_energy"
    set_energy(hass, member, 10)
    result = await submit_group(
        hass, entry, GROUP_SUM, {CONF_NAME: "Office", CONF_ENERGY_ENTITIES: [member]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (group_id,) = entry.subentries
    issue_id = f"{ISSUE_PENDING_ENERGY_REPORTS}_{group_id}"
    issues = ir.async_get(hass)

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, member, 10)
    set_energy(hass, member, 10)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, member, 10)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    set_energy(hass, member, 10)
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert_value(hass, "sensor.office_energy", 0)

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, member, 10)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    form = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    assert form["type"] is FlowResultType.FORM
    set_energy(hass, member, STATE_UNAVAILABLE)
    assert issues.async_get_issue(DOMAIN, issue_id) is None

    set_energy(hass, member, 10)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    stale_result = await manager.async_configure(form["flow_id"], {})
    assert stale_result["type"] is FlowResultType.ABORT
    assert stale_result["reason"] == "no_longer_applicable"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    assert_unavailable(hass, "sensor.office_energy")
    assert hass.config_entries.async_remove_subentry(entry, group_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_difference_power_requires_available_base_reading(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Skip missing readings and resume power when a base sensor returns."""
    entry = await create_integration(hass)
    base = register_meter_device(
        hass, "base", "base_energy", "base_main_power", "base_aux_power"
    )
    main, auxiliary = "sensor.base_main_power", "sensor.base_aux_power"
    set_energy(hass, "sensor.base_energy", 10)
    set_power(hass, main, 180)
    set_power(hass, auxiliary, 20)
    set_power(hass, "sensor.downstream_power", 40)
    set_power(hass, "sensor.other_power", 10)
    result = await submit_group(
        hass,
        entry,
        GROUP_DIFFERENCE,
        {
            CONF_NAME: "Remaining",
            CONF_BASE_DEVICE: base,
            CONF_POWER_ENTITIES: ["sensor.downstream_power", "sensor.other_power"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.remaining_require_all_entities"},
        blocking=True,
    )

    entity_id = "sensor.remaining_power"
    assert_value(hass, entity_id, 150)
    set_power(hass, "sensor.downstream_power", STATE_UNAVAILABLE)
    assert_value(hass, entity_id, 190)
    set_power(hass, auxiliary, STATE_UNAVAILABLE)
    assert_value(hass, entity_id, 170)
    set_power(hass, main, STATE_UNAVAILABLE)
    set_power(hass, "sensor.downstream_power", 40)
    state = hass.states.get(entity_id)
    assert state is not None and state.state == STATE_UNAVAILABLE
    status = hass.states.get("sensor.remaining_power_source_status")
    assert status is not None and status.state == "missing_base"
    assert status.attributes["missing_sources"] == [auxiliary, main]
    set_power(hass, main, 180)
    assert_value(hass, entity_id, 130)


async def test_difference_rejects_base_devices_with_zero_or_multiple_energy_sensors(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Require one compatible energy sensor on the selected base device."""
    entry = await create_integration(hass)
    power_only = register_meter_device(hass, "power_only", "base_power")
    two_meters = register_meter_device(hass, "two_meters", "base_a", "base_b")
    set_power(hass, "sensor.base_power", 50)
    set_energy(hass, "sensor.base_a", 10)
    set_energy(hass, "sensor.base_b", 10)
    set_energy(hass, "sensor.member_energy", 3)

    for base_device in (power_only, two_meters):
        result = await submit_group(
            hass,
            entry,
            GROUP_DIFFERENCE,
            {
                CONF_NAME: "Remaining",
                CONF_BASE_DEVICE: base_device,
                CONF_ENERGY_ENTITIES: ["sensor.member_energy"],
            },
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {
            CONF_BASE_DEVICE: "base_device_requires_one_energy_sensor"
        }
    assert not entry.subentries


async def test_difference_power_sensor_follows_base_device(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Create and remove the power entity as the base gains or loses power support."""
    entry = await create_integration(hass)
    energy_only = register_meter_device(hass, "energy_only", "base_energy")
    with_power = register_meter_device(hass, "with_power", "upstream", "upstream_power")
    member = register_meter_device(hass, "member", "member_energy", "member_power")
    set_energy(hass, "sensor.base_energy", 10)
    set_energy(hass, "sensor.upstream", 15)
    set_energy(hass, "sensor.member_energy", 3)
    set_power(hass, "sensor.upstream_power", 90)
    set_power(hass, "sensor.member_power", 25)

    data = {
        CONF_NAME: "Remaining",
        CONF_BASE_DEVICE: energy_only,
        CONF_DEVICES: [member],
    }
    result = await submit_group(hass, entry, GROUP_DIFFERENCE, data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry_id,) = entry.subentries
    manager = hass.config_entries.subentries
    registry = er.async_get(hass)
    power_id = "sensor.remaining_power"
    energy_id = "sensor.remaining_energy"
    assert registry.async_get(power_id) is None
    assert hass.states.get(power_id) is None
    set_energy(hass, "sensor.member_energy", 4)
    set_energy(hass, "sensor.base_energy", 13)
    assert_value(hass, energy_id, 2)

    for base_device in (with_power, energy_only):
        form = await manager.async_init(
            (entry.entry_id, GROUP_DIFFERENCE),
            context={"source": "reconfigure", "subentry_id": subentry_id},
        )
        result = await manager.async_configure(
            form["flow_id"], data | {CONF_BASE_DEVICE: base_device}
        )
        assert result["type"] is FlowResultType.ABORT
        await hass.async_block_till_done()
        assert_value(hass, energy_id, 2)
        if base_device == with_power:
            assert_value(hass, power_id, 65)
        else:
            assert registry.async_get(power_id) is None
            assert hass.states.get(power_id) is None


async def test_difference_outage_reload_and_reconfigure(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Apply pending readings at the base's pace across outages and reloads."""
    entry = await create_integration(hass)
    base = register_meter_device(hass, "base", "upstream", "upstream_power")
    set_energy(hass, "sensor.upstream", 10)
    set_energy(hass, "sensor.downstream", 4)
    set_energy(hass, "sensor.another", 30)
    set_power(hass, "sensor.upstream_power", 200)
    set_power(hass, "sensor.downstream_power", 50)
    manager = hass.config_entries.subentries
    form = await manager.async_init(
        (entry.entry_id, GROUP_DIFFERENCE), context={"source": "user"}
    )
    valid = {
        CONF_NAME: "Strip",
        CONF_BASE_DEVICE: base,
        CONF_ENERGY_ENTITIES: ["sensor.downstream"],
        CONF_POWER_ENTITIES: ["sensor.downstream_power"],
    }
    with pytest.raises(InvalidData):
        await manager.async_configure(
            form["flow_id"],
            {key: value for key, value in valid.items() if key != CONF_BASE_DEVICE},
        )
    result = await manager.async_configure(form["flow_id"], valid)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    switch_id = "switch.strip_require_all_entities"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )

    entity_id = "sensor.strip_energy"
    assert_value(hass, "sensor.strip_power", 150)
    initial = hass.states.get(entity_id)
    assert initial is not None
    last_reported = initial.last_reported
    set_energy(hass, "sensor.downstream", 5)
    assert_value(hass, entity_id, 0)
    assert initial.last_reported == last_reported
    set_energy(hass, "sensor.upstream", 10)
    assert_value(hass, entity_id, -1)
    set_energy(hass, "sensor.upstream", STATE_UNAVAILABLE)
    set_energy(hass, "sensor.downstream", 6)
    set_energy(hass, "sensor.downstream", STATE_UNAVAILABLE)
    assert_value(hass, entity_id, -2)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_value(hass, entity_id, -2)
    set_energy(hass, "sensor.upstream", 13)
    assert_value(hass, entity_id, 1)
    set_energy(hass, "sensor.downstream", 6)
    set_power(hass, "sensor.downstream_power", 80)
    assert_value(hass, "sensor.strip_power", 120)

    (subentry_id,) = entry.subentries
    # Simulate the saved checkpoint of a group created by the previous code.
    entry.runtime_data.states[subentry_id].pop("published_total")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_value(hass, entity_id, 1)
    set_energy(hass, "sensor.downstream", 7)
    assert_value(hass, entity_id, 1)
    set_energy(hass, "sensor.upstream", 14)
    assert_value(hass, entity_id, 1)

    form = await manager.async_init(
        (entry.entry_id, GROUP_DIFFERENCE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    cycle = await manager.async_configure(
        form["flow_id"],
        valid
        | {
            CONF_ENERGY_ENTITIES: [entity_id],
        },
    )
    assert cycle["errors"] == {"base": "cyclic_group"}
    result = await manager.async_configure(
        form["flow_id"],
        valid
        | {
            CONF_ENERGY_ENTITIES: ["sensor.downstream", "sensor.another"],
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    switch_state = hass.states.get(switch_id)
    assert switch_state is not None and switch_state.state == STATE_OFF
    assert_value(hass, entity_id, 1)

    # Keep the increment before the reset and the reading after it.
    set_energy(hass, "sensor.another", 31)
    set_energy(hass, "sensor.another", 2)
    assert_value(hass, entity_id, 1)
    set_energy(hass, "sensor.upstream", 14)
    assert_value(hass, entity_id, -2)


async def test_difference_availability_switch(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Start new switches on and retain each group's setting across reloads."""
    entry = await create_integration(hass)
    base_device = register_meter_device(hass, "base", "main_energy", "main_power")
    base, member = "sensor.main_energy", "sensor.load_energy"
    set_energy(hass, base, 100)
    set_energy(hass, member, 40)
    set_power(hass, "sensor.main_power", 100)
    set_power(hass, "sensor.load_power", 50)
    result = await submit_group(
        hass,
        entry,
        GROUP_DIFFERENCE,
        {
            CONF_NAME: "Office",
            CONF_BASE_DEVICE: base_device,
            CONF_ENERGY_ENTITIES: [member],
            CONF_POWER_ENTITIES: ["sensor.load_power"],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    energy_id = "sensor.office_energy"
    power_id = "sensor.office_power"
    switch_id = "switch.office_require_all_energy_sources"
    registry = er.async_get(hass)
    energy = registry.async_get(energy_id)
    switch = registry.async_get("switch.office_require_all_entities")
    assert energy is not None and switch is not None
    assert energy.device_id == switch.device_id
    (group_id,) = entry.subentries
    assert switch.unique_id == f"{group_id}_require_all_energy_sources"
    registry.async_update_entity(switch.entity_id, new_entity_id=switch_id)
    await hass.async_block_till_done()
    state = hass.states.get(switch_id)
    assert state is not None and state.state == STATE_ON
    assert state.name == "Office Require all entities"

    set_energy(hass, member, STATE_UNAVAILABLE)
    assert_unavailable(hass, energy_id)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    result = await submit_group(
        hass, entry, GROUP_SUM, {CONF_NAME: "Meter total", CONF_ENERGY_ENTITIES: [base]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    state = hass.states.get(switch_id)
    assert state is not None and state.state == STATE_OFF
    other_switch = hass.states.get("switch.meter_total_require_all_entities")
    assert other_switch is not None and other_switch.state == STATE_ON

    set_energy(hass, base, 105)
    assert_value(hass, energy_id, 5)
    set_energy(hass, member, 43)
    assert_value(hass, energy_id, 2)

    set_power(hass, "sensor.load_power", STATE_UNAVAILABLE)
    assert_value(hass, power_id, 100)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert_unavailable(hass, power_id)
    assert_value(hass, energy_id, 2)
    set_energy(hass, base, STATE_UNAVAILABLE)
    set_energy(hass, member, 44)
    assert_unavailable(hass, energy_id)

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert_value(hass, energy_id, 1)
    assert_value(hass, power_id, 100)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(switch_id)
    assert state is not None and state.state == STATE_ON
    assert_unavailable(hass, energy_id)
    assert_unavailable(hass, power_id)
    set_power(hass, "sensor.load_power", 35)
    assert_value(hass, power_id, 65)
    assert_unavailable(hass, energy_id)

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, base, 110)
    assert_unavailable(hass, energy_id)
    set_energy(hass, member, 45)
    assert_unavailable(hass, energy_id)
    set_energy(hass, base, 110)
    assert_unavailable(hass, energy_id)

    set_energy(hass, member, STATE_UNAVAILABLE)
    set_energy(hass, member, 45)
    set_energy(hass, member, 45)
    assert_unavailable(hass, energy_id)
    set_energy(hass, base, 110)
    assert_value(hass, energy_id, 5)
    set_energy(hass, member, 46)
    assert_value(hass, energy_id, 5)
    set_energy(hass, base, 110)
    assert_value(hass, energy_id, 4)

    set_power(hass, "sensor.main_power", STATE_UNAVAILABLE)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert_unavailable(hass, power_id)
    set_power(hass, "sensor.main_power", 100)
    assert_value(hass, power_id, 65)


@pytest.mark.parametrize(
    ("group_type", "historical", "live", "later", "interruption"),
    [
        (GROUP_SUM, [15, 17], 20, 21, None),
        (GROUP_DIFFERENCE, [5, 7], 6, 5, None),
        (GROUP_SUM, [15, 17], 20, 21, "before_commit"),
        (GROUP_DIFFERENCE, [5, 7], 6, 5, "after_commit"),
    ],
)
async def test_history_and_recorder_continuation(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    group_type: str,
    historical: list[float],
    live: float,
    later: float,
    interruption: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backfill and continue in kWh, even when the first attempt is interrupted."""
    # Recorder seeds a compiled interval when its fixture opens the database.
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    start = now - timedelta(hours=2)
    freezer.move_to(now + timedelta(minutes=1))
    await hass.async_start()

    for entity_id, unit, hourly, short in [
        (
            "sensor.a",
            UnitOfEnergy.WATT_HOUR,
            [(0, 100000, 10000), (60, 2000, 12000)],
            [(60, 1000, 11000), (115, 2000, 12000)],
        ),
        ("sensor.b", UnitOfEnergy.KILO_WATT_HOUR, [(0, 20, 5)], [(115, 21, 6)]),
    ]:
        metadata = StatisticMetaData(
            statistic_id=entity_id,
            source="recorder",
            name=None,
            unit_of_measurement=unit,
            unit_class="energy",
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
        )
        for table, rows in [(Statistics, hourly), (StatisticsShortTerm, short)]:
            recorder_mock.async_import_statistics(
                metadata,
                [
                    StatisticData(
                        start=start + timedelta(minutes=minute), state=value, sum=total
                    )
                    for minute, value, total in rows
                ],
                table,
            )
    await async_wait_recording_done(hass)

    data = {CONF_NAME: "History", CONF_IMPORT_HISTORY: True}
    if group_type == GROUP_DIFFERENCE:
        data[CONF_BASE_DEVICE] = register_meter_device(hass, "base", "a")
        data[CONF_ENERGY_ENTITIES] = ["sensor.b"]
        set_energy(hass, "sensor.a", STATE_UNAVAILABLE, UnitOfEnergy.WATT_HOUR)
        set_energy(hass, "sensor.b", 22)
    else:
        data[CONF_ENERGY_ENTITIES] = ["sensor.a", "sensor.b"]
        set_energy(hass, "sensor.a", 3000, UnitOfEnergy.WATT_HOUR)
        set_energy(hass, "sensor.b", STATE_UNAVAILABLE)
    entry = await create_integration(hass)

    def interrupt_import(recorder: Recorder, entity_id: str, history: History) -> None:
        """Interrupt the import inside the transaction or after it commits."""
        if interruption == "before_commit":
            with patch.object(
                StatisticsShortTerm, "from_stats", side_effect=SQLAlchemyError
            ):
                write_history(recorder, entity_id, history)
        else:
            write_history(recorder, entity_id, history)
            raise HomeAssistantError("Interrupted after the Recorder commit")

    with monkeypatch.context() as attempt:
        if interruption is not None:
            attempt.setattr(sensor, "write_history", interrupt_import)
        result = await submit_group(hass, entry, group_type, data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.history_require_all_entities"},
        blocking=True,
    )

    entity_id = "sensor.history_energy"
    if interruption is not None:
        state = hass.states.get(entity_id)
        assert state is not None and state.state == STATE_UNAVAILABLE
        status = hass.states.get("sensor.history_energy_source_status")
        assert status is not None and status.state == "importing_history"
        if interruption == "before_commit":
            assert not await hass.async_add_executor_job(
                statistics_during_period,
                hass,
                start,
                None,
                {entity_id},
                "hour",
                None,
                {"sum"},
            )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    if group_type == GROUP_DIFFERENCE:
        # The member's live change is published while the base meter is offline.
        assert_value(hass, entity_id, live - 1)
        set_energy(hass, "sensor.a", 3000, UnitOfEnergy.WATT_HOUR)
    else:
        assert_value(hass, entity_id, live - 1)
        set_energy(hass, "sensor.b", 22)
    assert_value(hass, entity_id, live)
    await async_wait_recording_done(hass)
    freezer.move_to(now + timedelta(minutes=6))
    do_adhoc_statistics(hass, start=now)
    await async_wait_recording_done(hass)

    hourly_rows = await hass.async_add_executor_job(
        statistics_during_period, hass, start, None, {entity_id}, "hour", None, {"sum"}
    )
    assert [row["sum"] for row in hourly_rows[entity_id]] == historical
    short_rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {entity_id},
        "5minute",
        None,
        {"sum"},
    )
    assert [row["sum"] for row in short_rows[entity_id]] == [
        16 if group_type == GROUP_SUM else 6,
        18 if group_type == GROUP_SUM else 6,
        live,
    ]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_value(hass, entity_id, live)
    set_energy(hass, "sensor.b", 23)
    if group_type == GROUP_DIFFERENCE:
        assert_value(hass, entity_id, live)
        set_energy(hass, "sensor.a", 3000, UnitOfEnergy.WATT_HOUR)
    assert_value(hass, entity_id, later)
    await async_wait_recording_done(hass)
    freezer.move_to(now + timedelta(minutes=11))
    do_adhoc_statistics(hass, start=now + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    short_rows = await hass.async_add_executor_job(
        statistics_during_period, hass, now, None, {entity_id}, "5minute", None, {"sum"}
    )
    assert [row["sum"] for row in short_rows[entity_id]] == [live, later]
