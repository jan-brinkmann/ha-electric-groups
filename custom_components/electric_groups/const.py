"""Constants shared by the Electric Groups integration."""

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

DOMAIN = "electric_groups"
NAME = "Electric Groups"

GROUP_SUM = "sum"
GROUP_DIFFERENCE = "difference"
ISSUE_PENDING_ENERGY_REPORTS = "pending_energy_reports"

CONF_DEVICES = "devices"
CONF_ENERGY_ENTITIES = "energy_entities"
CONF_POWER_ENTITIES = "power_entities"
CONF_BASE_DEVICE = "base_device"
CONF_IMPORT_HISTORY = "import_history"
CONF_REQUIRE_ALL_SOURCES_DEFAULT = "require_all_sources_default"
CONF_SOURCES = "sources"

MEASUREMENTS = {
    SensorDeviceClass.ENERGY: (EnergyConverter, UnitOfEnergy.KILO_WATT_HOUR),
    SensorDeviceClass.POWER: (PowerConverter, UnitOfPower.WATT),
}
