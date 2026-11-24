"""Restore energy groups waiting for reports from reconnected meters."""

from datetime import datetime
from string import punctuation

import probatio
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from . import ElectricGroupsEntry

_MARKDOWN_ESCAPES = str.maketrans(
    {character: f"\\{character}" for character in punctuation}
)


class PendingEnergyReportsRepair(RepairsFlow):
    """Show outstanding reports and resume energy only after confirmation."""

    _issue_created: datetime | None = None

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Open the confirmation step without changing the group."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the affected entities or repair the same issue on submission."""
        issue = ir.async_get(self.hass).async_get_issue(self.handler, self.issue_id)
        if issue is None or not issue.active:
            return self.async_abort(reason="no_longer_applicable")
        if user_input is None:
            entity_ids = (issue.data or {}).get("entity_ids")
            if not isinstance(entity_ids, str) or not entity_ids:
                return self.async_abort(reason="no_longer_applicable")
            placeholders = issue.translation_placeholders or {}
            self._issue_created = issue.created
            return self.async_show_form(
                step_id="confirm",
                data_schema=probatio.Schema({}),
                description_placeholders={
                    **placeholders,
                    "group_name": placeholders.get("group", "").translate(
                        _MARKDOWN_ESCAPES
                    ),
                    "entities": self._async_entity_links(tuple(entity_ids.split())),
                },
            )
        if issue.created != self._issue_created:
            return self.async_abort(reason="no_longer_applicable")

        return self._async_repair_group()

    @callback
    def _async_repair_group(self) -> RepairsFlowResult:
        """Resume energy if the group's sensors are still awaiting reports."""
        data = self.data or {}
        entry_id, group_id = data.get("entry_id"), data.get("group_id")
        if not isinstance(entry_id, str) or not isinstance(group_id, str):
            return self.async_abort(reason="no_longer_applicable")

        entry: ElectricGroupsEntry | None = self.hass.config_entries.async_get_entry(
            entry_id
        )
        if (
            entry is None
            or entry.state is not ConfigEntryState.LOADED
            or (group := entry.subentries.get(group_id)) is None
            or (resume := entry.runtime_data.energy_recovery_callbacks.get(group_id))
            is None
            or not resume()
        ):
            return self.async_abort(reason="no_longer_applicable")
        return self.async_create_entry(
            data={},
            description_placeholders={
                "group_name": group.title.translate(_MARKDOWN_ESCAPES)
            },
        )

    @callback
    def _async_entity_links(self, entity_ids: tuple[str, ...]) -> str:
        """List names and IDs in Markdown, linking to devices when known."""
        registry = er.async_get(self.hass)
        lines = []
        for entity_id in entity_ids:
            label = f"`{entity_id}`"
            entry = registry.async_get(entity_id)
            state = self.hass.states.get(entity_id)
            name = (
                (entry and entry.name)
                or (state and state.attributes.get(ATTR_FRIENDLY_NAME))
                or (entry and entry.original_name)
            )
            if isinstance(name, str) and name and name != entity_id:
                escaped_name = " ".join(name.split()).translate(_MARKDOWN_ESCAPES)
                label = f"{escaped_name} ({label})"
            if entry is not None and entry.device_id:
                label = f"[{label}](/config/devices/device/{entry.device_id})"
            lines.append(f"- {label}")
        return "\n".join(lines)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Return the handler for a pending energy reports issue."""
    return PendingEnergyReportsRepair()
