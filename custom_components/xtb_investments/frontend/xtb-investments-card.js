class XTBInvestmentsCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Entity is required");
    }

    this.config = {
      show_positions: true,
      show_quotes: true,
      show_orders: true,
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
      show_positions: true,
      show_quotes: true,
      show_orders: true,
    };
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
    const orders = Array.isArray(attrs.orders) ? attrs.orders : [];
    const quotes = Object.values(attrs.quotes || {});
    const currency = summary.currency || attrs.unit_of_measurement || "";
    const calculatedAccountValue =
      Number.isFinite(Number(summary.cash_balance)) && Number.isFinite(Number(summary.asset_value))
        ? Number(summary.cash_balance) + Number(summary.asset_value)
        : undefined;
    const accountValue = summary.account_value ?? summary.portfolio_value ?? calculatedAccountValue ?? summary.balance ?? state.state;
    const profit = Number(summary.profit_net ?? summary.position_profit_net ?? 0);
    const profitClass = profit >= 0 ? "positive" : "negative";

    this.innerHTML = `
      <ha-card>
        <div class="xtb-card">
          <header class="xtb-header">
            <div>
              <div class="eyebrow">XTB</div>
              <h2>Saldo</h2>
            </div>
            <div class="updated">${this.formatDate(attrs.updated_at)}</div>
          </header>

          <section class="hero">
            <div>
              <div class="label">Wartość konta</div>
              <div class="equity">${this.money(accountValue, currency)}</div>
            </div>
            <div class="profit ${profitClass}">
              <ha-icon icon="${profit >= 0 ? "mdi:trending-up" : "mdi:trending-down"}"></ha-icon>
              <span>${this.money(profit, currency)}</span>
              <small>${this.percent(summary.profit_percent)}</small>
            </div>
          </section>

          <section class="metrics">
            ${this.metric("Wolne środki", this.money(summary.cash_balance, currency), "mdi:cash")}
            ${this.metric("Aktywa", this.money(summary.asset_value, currency), "mdi:briefcase-check")}
            ${this.metric("Pozycje", summary.open_positions ?? positions.length, "mdi:format-list-bulleted")}
            ${this.metric("Zlecenia", summary.pending_orders ?? orders.length, "mdi:clipboard-clock")}
          </section>

          ${
            this.config.show_positions
              ? this.tableSection(
                  "Pozycje",
                  positions,
                  ["Symbol", "Wolumen", "Wartość", "Dzień", "Zysk/strata"],
                  (position) => {
                    const positionProfit = position.profit_loss ?? position.profit_net;
                    return `
                      <tr>
                        <td class="strong">${this.escape(this.instrumentName(position))}</td>
                        <td>${this.number(position.volume)}</td>
                        <td>${this.money(position.market_value, currency)}</td>
                        <td class="${Number(position.daily_change_percent || 0) >= 0 ? "positive" : "negative"}">
                          ${this.percent(position.daily_change_percent)}
                        </td>
                        <td class="pl-cell ${Number(positionProfit || 0) >= 0 ? "positive" : "negative"}">
                          <span>${this.money(positionProfit, currency)}</span>
                          <small>${this.percent(position.profit_loss_percent ?? position.profit_percent)}</small>
                        </td>
                      </tr>
                    `;
                  }
                )
              : ""
          }

          ${
            this.config.show_quotes
              ? this.tableSection(
                  "Notowania",
                  quotes,
                  ["Symbol", "Bid", "Ask", "Spread", "Dzień"],
                  (quote) => `
                    <tr>
                      <td class="strong">${this.escape(this.instrumentName(quote))}</td>
                      <td>${this.number(quote.bid)}</td>
                      <td>${this.number(quote.ask)}</td>
                      <td>${this.number(quote.spread)}</td>
                      <td class="${Number(quote.daily_change_percent || 0) >= 0 ? "positive" : "negative"}">
                        ${this.percent(quote.daily_change_percent)}
                      </td>
                    </tr>
                  `
                )
              : ""
          }

          ${
            this.config.show_orders
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
  }

  metric(label, value, icon) {
    return `
      <div class="metric">
        <ha-icon icon="${icon}"></ha-icon>
        <span>${label}</span>
        <strong>${value}</strong>
      </div>
    `;
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

        .xtb-header,
        .hero,
        .metrics,
        .profit,
        .metric {
          display: flex;
          align-items: center;
        }

        .xtb-header {
          justify-content: space-between;
          gap: 16px;
          margin-bottom: 14px;
        }

        .eyebrow {
          color: var(--secondary-text-color);
          font-size: 11px;
          letter-spacing: 0;
          text-transform: uppercase;
        }

        h2,
        h3 {
          margin: 0;
          font-weight: 650;
          letter-spacing: 0;
        }

        h2 {
          font-size: 22px;
        }

        h3 {
          font-size: 15px;
          margin-bottom: 8px;
        }

        .updated {
          color: var(--secondary-text-color);
          font-size: 12px;
          white-space: nowrap;
        }

        .hero {
          justify-content: space-between;
          gap: 18px;
          padding: 14px 0 16px;
          border-top: 1px solid var(--divider-color);
          border-bottom: 1px solid var(--divider-color);
        }

        .label,
        .metric span {
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
          gap: 6px;
          min-width: 138px;
          font-weight: 700;
          white-space: nowrap;
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

        .metrics {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 8px;
          margin: 14px 0 18px;
        }

        .metric {
          min-width: 0;
          gap: 8px;
          padding: 10px;
          background: color-mix(in srgb, var(--primary-text-color) 5%, transparent);
          border: 1px solid color-mix(in srgb, var(--divider-color) 65%, transparent);
          border-radius: 8px;
        }

        .metric ha-icon {
          color: #246b8f;
          width: 18px;
          height: 18px;
          flex: 0 0 auto;
        }

        .metric strong {
          display: block;
          margin-left: auto;
          min-width: 0;
          overflow-wrap: anywhere;
          text-align: right;
          font-size: 13px;
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
        }

        th,
        td {
          padding: 8px 6px;
          text-align: right;
          border-bottom: 1px solid var(--divider-color);
          white-space: nowrap;
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

        .strong {
          font-weight: 650;
        }

        .pl-cell span,
        .pl-cell small {
          display: block;
        }

        .pl-cell small {
          color: var(--secondary-text-color);
          font-size: 11px;
          margin-top: 2px;
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
            align-items: flex-start;
            flex-direction: column;
          }

          .profit {
            justify-content: flex-start;
          }

          .metrics {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }

          .equity {
            font-size: 25px;
          }
        }
      </style>
    `;
  }
}

customElements.define("xtb-investments-card", XTBInvestmentsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "xtb-investments-card",
  name: "XTB Investments Card",
  description: "XTB account balance, positions, orders and daily changes",
});
