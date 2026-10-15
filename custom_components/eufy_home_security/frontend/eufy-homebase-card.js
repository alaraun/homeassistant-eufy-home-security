// eufy HomeBase card: placeholder module until the card ships.
const CARD_VERSION = '0.0.0';

class EufyHomebaseCard extends HTMLElement {
  setConfig(config) {
    if (!config || (!config.device_id && !config.entity)) {
      throw new Error('Set device_id or entity (the HomeBase alarm panel)');
    }
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' }).innerHTML = '<ha-card><div class="card-content"></div></ha-card>';
    }
  }

  getCardSize() {
    return 1;
  }
}

if (!customElements.get('eufy-homebase-card')) {
  customElements.define('eufy-homebase-card', EufyHomebaseCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === 'eufy-homebase-card')) {
  window.customCards.push({ type: 'eufy-homebase-card', name: 'eufy HomeBase', version: CARD_VERSION });
}
