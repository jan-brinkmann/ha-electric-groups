const messages = {
  en: {
    back: "Back to group device",
    base: "Base device",
    members: "Group members",
    sensors: "Sensors without a device",
    empty: "No members are available in Home Assistant.",
    loading: "Loading group members…",
    notFound: "This group is no longer available.",
    error: "The group members could not be loaded.",
  },
  de: {
    back: "Zurück zum Gruppengerät",
    base: "Basisgerät",
    members: "Gruppenmitglieder",
    sensors: "Sensoren ohne Gerät",
    empty: "In Home Assistant sind keine Gruppenmitglieder verfügbar.",
    loading: "Gruppenmitglieder werden geladen …",
    notFound: "Diese Gruppe ist nicht mehr verfügbar.",
    error: "Die Gruppenmitglieder konnten nicht geladen werden.",
  },
};

/** Display a read-only list of the devices and sensors configured for a group. */
class ElectricGroupsMembersPanel extends HTMLElement {
  #main;
  #hass;
  #groupId;
  #request;

  /** Create the panel content and its theme-aware styles. */
  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { display: block; color: var(--primary-text-color); }
        main { max-width: 44rem; margin: auto; padding: 1.25rem 1rem 2rem; }
        h1 { font-size: 1.6rem; overflow-wrap: anywhere; }
        h2 { font-size: 1.15rem; margin: 1.25rem 0 .5rem; }
        ul {
          background: var(--ha-card-background, var(--card-background-color, transparent));
          border: 1px solid var(--ha-card-border-color, var(--divider-color, transparent));
          border-radius: var(--ha-card-border-radius, 12px);
          list-style: none;
          margin: 0;
          padding: 0;
        }
        li + li { border-top: 1px solid var(--divider-color, transparent); }
        li > * { display: block; padding: .75rem 1rem; overflow-wrap: anywhere; }
        a, button { color: var(--link-text-color, var(--primary-color)); }
        button {
          background: none;
          border: 0;
          cursor: pointer;
          font: inherit;
          text-align: start;
          text-decoration: underline;
          width: 100%;
        }
      </style>
      <main></main>
    `;
    this.#main = root.querySelector("main");
  }

  /** Receive Home Assistant's locale and authenticated WebSocket connection. */
  set hass(hass) {
    this.#hass = hass;
    this.#load();
  }

  /** Read the group ID from the path below the panel URL. */
  set route(route) {
    const groupId = /^\/([^/]+)\/?$/.exec(route.path)?.[1] ?? null;
    if (groupId !== this.#groupId) {
      this.#groupId = groupId;
      this.#request = undefined;
      this.#load();
    }
  }

  /** Fetch the members once per group and ignore responses for previous routes. */
  async #load() {
    if (!this.#hass || this.#groupId === undefined || this.#request) return;

    const language = this.#hass.locale.language.split("-")[0];
    const text = messages[language] ?? messages.en;
    const request = {};
    this.#request = request;
    if (this.#groupId === null) {
      this.#showStatus(text.notFound);
      return;
    }

    this.#showStatus(text.loading);
    try {
      const group = await this.#hass.callWS({
        type: "electric_groups/members",
        group_id: this.#groupId,
      });
      if (this.#request === request) this.#showMembers(group, text);
    } catch (error) {
      if (this.#request === request) {
        this.#showStatus(error?.code === "not_found" ? text.notFound : text.error);
      }
    }
  }

  /** Show and announce a loading or error message for the current route. */
  #showStatus(text) {
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    status.textContent = text;
    this.#main.replaceChildren(status);
  }

  /** Render links to the group, the base device, and the individual members. */
  #showMembers({ group, base, members, entities }, text) {
    const heading = document.createElement("h1");
    heading.textContent = group.name;
    this.#main.replaceChildren(this.#deviceLink(group, text.back), heading);

    if (base) this.#appendList(text.base, [this.#deviceLink(base)]);
    this.#appendList(
      text.members,
      members.map((device) => this.#deviceLink(device)),
      entities.length ? undefined : text.empty,
    );
    this.#appendList(
      text.sensors,
      entities.map((id) => this.#sensorDetails(id)),
    );
  }

  /** Append a titled section containing device links or sensor details. */
  #appendList(title, items, emptyText) {
    if (!items.length && !emptyText) return;

    const heading = document.createElement("h2");
    heading.textContent = title;
    if (!items.length) {
      const empty = document.createElement("p");
      empty.textContent = emptyText;
      this.#main.append(heading, empty);
      return;
    }

    const list = document.createElement("ul");
    for (const item of items) {
      const row = document.createElement("li");
      row.append(item);
      list.append(row);
    }
    this.#main.append(heading, list);
  }

  /** Build an internal link to a device's standard Home Assistant page. */
  #deviceLink(device, label = device.name) {
    const link = document.createElement("a");
    link.href = `/config/devices/device/${encodeURIComponent(device.id)}`;
    link.textContent = label;
    return link;
  }

  /** Open more-info for an available standalone sensor or show its saved ID. */
  #sensorDetails(entityId) {
    const state = this.#hass.states[entityId];
    const details = document.createElement(state ? "button" : "span");
    details.textContent = state?.attributes.friendly_name || entityId;
    if (state) {
      details.type = "button";
      details.addEventListener("click", () => {
        details.dispatchEvent(
          new CustomEvent("hass-more-info", {
            bubbles: true,
            composed: true,
            detail: { entityId },
          }),
        );
      });
    }
    return details;
  }
}

customElements.define("electric-groups-members-panel", ElectricGroupsMembersPanel);
