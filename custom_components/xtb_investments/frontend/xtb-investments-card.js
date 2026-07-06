class XTBInvestmentsCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Entity is required");
    }

    this.config = {
      header: "XTB",
      show_positions: true,
      show_orders: false,
      ...config,
    };
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return {
      entity: "sensor.xtb_balance",
      header: "XTB",
      show_positions: true,
      show_orders: false,
    };
  }

  static getConfigElement() {
    return document.createElement("xtb-investments-card-editor");
  }

  render() {
    if (!this.config || !this._hass) {
      return;
    }

    const state = this._hass.states[this.config.entity];
    if (!state) {
      this.innerHTML = `
        <ha-card>
          <div class="xtb-card xtb-empty">Brak encji: ${this.escape(this.config.entity)}</div>
        </ha-card>
        ${this.styles()}
      `;
      return;
    }

    const attrs = state.attributes || {};
    const summary = attrs.summary || {};
    const positions = Array.isArray(attrs.positions) ? attrs.positions : [];
    const sortedPositions = [...positions].sort(
      (a, b) => this.positionProfit(b) - this.positionProfit(a)
    );
    const orders = Array.isArray(attrs.orders) ? attrs.orders : [];
    const currency = summary.currency || attrs.unit_of_measurement || "";
    const totalEquity = this.asNumber(summary.total_equity);
    const profitNet = this.asNumber(summary.profit_net);
    const totalEquityWithProfit =
      totalEquity !== undefined && profitNet !== undefined ? totalEquity + profitNet : undefined;
    const cashBalance = this.asNumber(summary.cash_balance);
    const assetValue = this.asNumber(summary.asset_value);
    const calculatedAccountValue =
      cashBalance !== undefined && assetValue !== undefined
        ? cashBalance + assetValue
        : undefined;
    const accountValue =
      summary.side_bar_account_value ??
      totalEquityWithProfit ??
      summary.account_value ??
      summary.portfolio_value ??
      calculatedAccountValue ??
      summary.balance ??
      state.state;
    const profit = Number(summary.profit_net ?? summary.position_profit_net ?? 0);
    const profitClass = profit >= 0 ? "positive" : "negative";
    const profitEntityId = this.profitEntityId(currency);
    const header = this.cardHeader();

    this.innerHTML = `
      <ha-card>
        <div class="xtb-card">
          <section class="hero">
            <div class="account-value"${this.entityDataAttribute(this.config.entity)}>
              <div class="label">Wartość konta</div>
              <div class="equity">${this.money(accountValue, currency)}</div>
            </div>
            <div class="hero-side">
              <div class="brand">${this.escape(header)}</div>
              <div class="updated">${this.formatDate(attrs.updated_at)}</div>
              <div class="profit ${profitClass}"${this.entityDataAttribute(profitEntityId)}>
                <ha-icon icon="${profit >= 0 ? "mdi:trending-up" : "mdi:trending-down"}"></ha-icon>
                <span>${this.money(profit, currency)}</span>
                <small>${this.percent(summary.profit_percent)}</small>
              </div>
            </div>
          </section>

          ${
            this.config.show_positions
              ? this.tableSection(
                  "Pozycje",
                  sortedPositions,
                  ["Symbol", "Dzień", "Zysk/strata"],
                  (position) => {
                    const positionProfit = this.positionProfit(position);
                    const positionEntityId = this.positionEntityId(position, currency);
                    return `
                      <tr${this.entityDataAttribute(positionEntityId)}>
                        <td class="strong">
                          <div class="instrument">
                            ${this.instrumentMark(position)}
                            <span class="instrument-name">${this.escape(this.instrumentName(position))}</span>
                          </div>
                        </td>
                        <td class="${Number(position.daily_change_percent || 0) >= 0 ? "positive" : "negative"}">
                          ${this.percent(position.daily_change_percent)}
                        </td>
                        <td class="pl-cell ${Number(positionProfit || 0) >= 0 ? "positive" : "negative"}">
                          <span>${this.money(positionProfit, currency)}</span>
                        </td>
                      </tr>
                    `;
                  }
                )
              : ""
          }

          ${
            this.config.show_orders && orders.length
              ? this.tableSection(
                  "Zlecenia",
                  orders,
                  ["Symbol", "Strona", "Wolumen", "Cena", "Typ"],
                  (order) => `
                    <tr>
                      <td class="strong">${this.escape(order.symbol)}</td>
                      <td>${this.escape(order.side || "")}</td>
                      <td>${this.number(order.volume)}</td>
                      <td>${this.number(order.price)}</td>
                      <td>${this.escape(order.order_type || "")}</td>
                    </tr>
                  `
                )
              : ""
          }
        </div>
      </ha-card>
      ${this.styles()}
    `;
    this.attachImageFallbacks();
    this.attachInteractions();
  }

  tableSection(title, rows, headers, rowTemplate) {
    if (!rows.length) {
      return `
        <section class="table-section">
          <h3>${title}</h3>
          <div class="empty">Brak danych</div>
        </section>
      `;
    }

    return `
      <section class="table-section">
        <h3>${title}</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>${headers.map((header) => `<th>${header}</th>`).join("")}</tr>
            </thead>
            <tbody>${rows.map(rowTemplate).join("")}</tbody>
          </table>
        </div>
      </section>
    `;
  }

  instrumentName(item) {
    return item.display_name || item.name || item.description || item.symbol || "";
  }

  cardHeader() {
    const value = this.config.header ?? this.config.title ?? "XTB";
    return String(value || "").trim() || "XTB";
  }

  entityDataAttribute(entityId) {
    return entityId ? ` data-entity-id="${this.escape(entityId)}"` : "";
  }

  positionEntityId(position, currency) {
    const symbol = String(position.symbol || "").toUpperCase();
    if (!symbol || !this._hass?.states) {
      return "";
    }

    const accountNumber = String(position.account_number || "");
    const orderId = String(position.order_id || "");
    const expectedProfit = this.positionProfit(position);
    let best = { entityId: "", score: 0 };

    Object.entries(this._hass.states).forEach(([entityId, state]) => {
      const attrs = state.attributes || {};
      if (!entityId.startsWith("sensor.") || String(attrs.symbol || "").toUpperCase() !== symbol) {
        return;
      }

      let score = 8;
      if (accountNumber && String(attrs.account_number || "") === accountNumber) {
        score += 4;
      }
      if (orderId && String(attrs.order_id || "") === orderId) {
        score += 6;
      }
      if (currency && (attrs.unit_of_measurement === currency || attrs.currency === currency)) {
        score += 3;
      }
      if (this.asNumber(attrs.market_value) !== undefined || this.asNumber(attrs.volume) !== undefined) {
        score += 2;
      }
      if (this.asNumber(attrs.profit_loss_percent) !== undefined) {
        score += 2;
      }
      const friendlyName = String(attrs.friendly_name || attrs.name || "").toLowerCase();
      if (friendlyName.includes("zysk/strata") || friendlyName.includes("profit")) {
        score += 4;
      }
      const stateProfit = this.asNumber(state.state);
      if (stateProfit !== undefined && Math.abs(stateProfit - expectedProfit) < 0.02) {
        score += 3;
      }

      if (score > best.score) {
        best = { entityId, score };
      }
    });

    return best.score >= 10 ? best.entityId : "";
  }

  profitEntityId(currency) {
    if (!this._hass?.states) {
      return "";
    }

    let best = { entityId: "", score: 0 };
    Object.entries(this._hass.states).forEach(([entityId, state]) => {
      const attrs = state.attributes || {};
      if (!entityId.startsWith("sensor.") || entityId === this.config.entity) {
        return;
      }

      let score = 0;
      if (currency && (attrs.unit_of_measurement === currency || attrs.currency === currency)) {
        score += 3;
      }
      if (this.asNumber(attrs.profit_percent) !== undefined) {
        score += 4;
      }
      if (this.asNumber(attrs.position_profit_net) !== undefined) {
        score += 2;
      }

      const friendlyName = String(attrs.friendly_name || "").toLowerCase();
      if (friendlyName.includes("zysk") || friendlyName.includes("profit")) {
        score += 3;
      }
      if (friendlyName.includes("zysk/strata")) {
        score -= 6;
      }
      if (attrs.symbol) {
        score -= 6;
      }

      if (score > best.score) {
        best = { entityId, score };
      }
    });

    return best.score >= 6 ? best.entityId : "";
  }

  instrumentMark(item) {
    const iconUrl = this.instrumentIconUrl(item);
    const key = item.symbol || this.instrumentName(item);
    const avatarStyle = this.avatarStyle(key);
    const initials = this.escape(this.instrumentInitials(item));
    if (iconUrl) {
      return `
        <span class="instrument-avatar image" style="${avatarStyle}" aria-hidden="true">
          <span class="instrument-fallback">${initials}</span>
          <img src="${this.escape(iconUrl)}" alt="">
        </span>
      `;
    }

    return `
      <span class="instrument-avatar" style="${avatarStyle}" aria-hidden="true">
        <span class="instrument-fallback">${initials}</span>
      </span>
    `;
  }

  instrumentIconUrl(item) {
    const value =
      item.icon_url ||
      item.logo_url ||
      item.image_url ||
      item.iconUrl ||
      item.logoUrl ||
      item.imageUrl;
    const url = String(value || "").trim();
    if (
      url.startsWith("https://") ||
      url.startsWith("http://") ||
      url.startsWith("/") ||
      url.startsWith("data:image/")
    ) {
      return url;
    }
    return this.xtbLogoUrl(item.symbol);
  }

  xtbLogoUrl(symbol) {
    const slug = String(symbol || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
    return slug ? `https://logos.xtb.com/${slug}.png` : "";
  }

  instrumentInitials(item) {
    const symbol = String(item.symbol || "").trim();
    const ticker = symbol.split(".")[0].replace(/[^a-z0-9]/gi, "").slice(0, 4);
    if (ticker) {
      return ticker.toUpperCase();
    }

    const words = this.instrumentName(item)
      .replace(/\b(CFD|GDR|SA|S\.A\.|PLC|LTD|CO|CLASS|ONLY)\b/gi, " ")
      .match(/[a-z0-9]+/gi);
    if (!words || !words.length) {
      return "XTB";
    }
    return words
      .slice(0, 2)
      .map((word) => word[0])
      .join("")
      .toUpperCase();
  }

  avatarStyle(value) {
    const palette = [
      "#0f766e",
      "#2563eb",
      "#b45309",
      "#be123c",
      "#4f46e5",
      "#15803d",
      "#0369a1",
      "#c2410c",
    ];
    const text = String(value || "");
    let hash = 0;
    for (let index = 0; index < text.length; index += 1) {
      hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
    }
    return `--avatar-bg: ${palette[hash % palette.length]}`;
  }

  attachImageFallbacks() {
    this.querySelectorAll(".instrument-avatar.image img").forEach((image) => {
      image.addEventListener(
        "error",
        () => image.closest(".instrument-avatar")?.classList.add("image-error"),
        { once: true }
      );
      image.addEventListener(
        "load",
        () => image.closest(".instrument-avatar")?.classList.add("image-loaded"),
        { once: true }
      );
    });
  }

  attachInteractions() {
    this.querySelectorAll("[data-entity-id]").forEach((element) => {
      const entityId = element.dataset.entityId;
      if (!entityId || !this._hass.states[entityId]) {
        return;
      }
      element.classList.add("clickable");
      element.setAttribute("role", "button");
      element.setAttribute("tabindex", "0");
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        this.showMoreInfo(entityId);
      });
      element.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        this.showMoreInfo(entityId);
      });
    });
  }

  showMoreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId },
      })
    );
  }

  positionProfit(position) {
    const profit = this.asNumber(position.profit_loss ?? position.profit_net);
    return profit ?? 0;
  }

  money(value, currency) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) {
      return "-";
    }

    if (!currency) {
      return this.number(amount);
    }

    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount);
  }

  number(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) {
      return "-";
    }

    return new Intl.NumberFormat(undefined, {
      maximumFractionDigits: 4,
    }).format(amount);
  }

  percent(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) {
      return "-";
    }

    return `${new Intl.NumberFormat(undefined, {
      maximumFractionDigits: 2,
    }).format(amount)}%`;
  }

  asNumber(value) {
    const amount = Number(value);
    return Number.isFinite(amount) ? amount : undefined;
  }

  formatDate(value) {
    if (!value) {
      return "";
    }

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "";
    }

    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  styles() {
    return `
      <style>
        .xtb-card {
          padding: 18px;
          color: var(--primary-text-color);
        }

        .xtb-empty {
          color: var(--error-color);
        }

        .hero,
        .profit {
          display: flex;
          align-items: flex-end;
        }

        h3 {
          margin: 0;
          font-weight: 650;
          letter-spacing: 0;
        }

        h3 {
          font-size: 15px;
          margin-bottom: 8px;
        }

        .hero {
          justify-content: space-between;
          gap: 18px;
          padding: 0 0 16px;
          align-items: flex-end;
        }

        .account-value {
          min-width: 0;
        }

        .hero-side {
          display: flex;
          flex-direction: column;
          align-items: flex-end;
          gap: 5px;
          min-width: 150px;
          text-align: right;
        }

        .brand {
          color: var(--secondary-text-color);
          font-size: 11px;
          line-height: 1;
          letter-spacing: 0;
          text-transform: uppercase;
        }

        .updated {
          color: var(--secondary-text-color);
          font-size: 12px;
          line-height: 1.2;
          white-space: nowrap;
        }

        .label {
          color: var(--secondary-text-color);
          font-size: 12px;
        }

        .equity {
          font-size: 30px;
          line-height: 1.15;
          font-weight: 700;
          letter-spacing: 0;
        }

        .profit {
          justify-content: flex-end;
          align-items: center;
          gap: 6px;
          font-weight: 700;
          white-space: nowrap;
        }

        .account-value.clickable,
        .profit.clickable {
          border-radius: 8px;
          cursor: pointer;
        }

        .account-value.clickable:hover,
        .profit.clickable:hover {
          filter: brightness(1.08);
        }

        .profit ha-icon {
          width: 20px;
          height: 20px;
        }

        .profit small {
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 600;
        }

        .positive {
          color: #0b7f4f;
        }

        .negative {
          color: #b3261e;
        }

        .table-section {
          margin-top: 16px;
        }

        .table-wrap {
          overflow-x: auto;
        }

        table {
          width: 100%;
          border-collapse: collapse;
          font-size: 13px;
          table-layout: fixed;
        }

        th:first-child,
        td:first-child {
          width: 58%;
        }

        th,
        td {
          padding: 8px 6px;
          text-align: right;
          border-bottom: 1px solid var(--divider-color);
          white-space: nowrap;
        }

        tbody tr:last-child td {
          border-bottom: 0;
        }

        th:first-child,
        td:first-child {
          text-align: left;
        }

        th {
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0;
        }

        tr.clickable {
          cursor: pointer;
        }

        tbody tr.clickable:hover {
          background: color-mix(in srgb, var(--primary-text-color) 5%, transparent);
        }

        .clickable:focus-visible {
          outline: 2px solid var(--primary-color);
          outline-offset: 3px;
        }

        .strong {
          font-weight: 650;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        .instrument {
          display: flex;
          align-items: center;
          gap: 8px;
          min-width: 0;
        }

        .instrument-avatar {
          position: relative;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex: 0 0 24px;
          width: 24px;
          height: 24px;
          overflow: hidden;
          border: 1px solid color-mix(in srgb, var(--divider-color) 70%, transparent);
          border-radius: 8px;
          background: var(--avatar-bg, #246b8f);
          color: #fff;
          font-size: 9px;
          font-weight: 800;
          line-height: 1;
          letter-spacing: 0;
        }

        .instrument-avatar.image {
          background: transparent;
        }

        .instrument-avatar img {
          display: block;
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: cover;
          border-radius: inherit;
          background: var(--card-background-color, #fff);
        }

        .instrument-avatar.image-error img {
          display: none;
        }

        .instrument-avatar.image-error {
          background: var(--avatar-bg, #246b8f);
        }

        .instrument-avatar.image-loaded .instrument-fallback {
          display: none;
        }

        .instrument-fallback {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 100%;
          height: 100%;
        }

        .instrument-name {
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
        }

        .pl-cell span {
          display: inline-block;
        }

        .empty {
          color: var(--secondary-text-color);
          padding: 8px 0;
          font-size: 13px;
        }

        @media (max-width: 640px) {
          .xtb-card {
            padding: 14px;
          }

          .hero {
            gap: 12px;
          }

          .hero-side {
            min-width: 116px;
          }

          .equity {
            font-size: 25px;
          }

          table {
            font-size: 12px;
          }

          th,
          td {
            padding: 8px 4px;
          }
        }
      </style>
    `;
  }
}

class XTBInvestmentsCardEditor extends HTMLElement {
  setConfig(config) {
    this.config = {
      entity: "sensor.xtb_balance",
      header: "XTB",
      show_positions: true,
      show_orders: false,
      ...config,
    };
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
  }

  render() {
    if (!this.config) {
      return;
    }

    this.innerHTML = `
      <div class="xtb-card-editor">
        <ha-textfield
          class="entity-input"
          label="Encja salda"
          value="${this.escape(this.config.entity || "")}"
        ></ha-textfield>
        <ha-textfield
          class="header-input"
          label="Nagłówek"
          value="${this.escape(this.config.header ?? "XTB")}"
        ></ha-textfield>
        <ha-formfield label="Pokaż pozycje">
          <ha-switch class="positions-input" ${this.config.show_positions !== false ? "checked" : ""}></ha-switch>
        </ha-formfield>
        <ha-formfield label="Pokaż zlecenia">
          <ha-switch class="orders-input" ${this.config.show_orders ? "checked" : ""}></ha-switch>
        </ha-formfield>
      </div>
      ${this.styles()}
    `;

    this.bindTextInput(".entity-input", "entity");
    this.bindTextInput(".header-input", "header");
    this.bindSwitchInput(".positions-input", "show_positions");
    this.bindSwitchInput(".orders-input", "show_orders");
  }

  bindTextInput(selector, key) {
    const input = this.querySelector(selector);
    input?.addEventListener("input", (event) => {
      this.updateConfig({ [key]: event.target.value });
    });
  }

  bindSwitchInput(selector, key) {
    const input = this.querySelector(selector);
    input?.addEventListener("change", (event) => {
      this.updateConfig({ [key]: event.target.checked });
    });
  }

  updateConfig(changedConfig) {
    this.config = {
      ...this.config,
      ...changedConfig,
    };
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        bubbles: true,
        composed: true,
        detail: { config: this.config },
      })
    );
  }

  escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  styles() {
    return `
      <style>
        .xtb-card-editor {
          display: grid;
          gap: 12px;
        }

        ha-textfield {
          width: 100%;
        }
      </style>
    `;
  }
}

if (!customElements.get("xtb-investments-card")) {
  customElements.define("xtb-investments-card", XTBInvestmentsCard);
}

if (!customElements.get("xtb-investments-card-editor")) {
  customElements.define("xtb-investments-card-editor", XTBInvestmentsCardEditor);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "xtb-investments-card",
  name: "XTB Investments Card",
  description: "XTB account balance, profit and sorted positions",
});
