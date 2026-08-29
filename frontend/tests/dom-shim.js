// solar-planner-card.js registers itself as a custom element at module load time
// (customElements.define, window.customCards.push) and renders by assigning
// shadowRoot.innerHTML — these globals only exist in a browser. Stub the minimum
// needed to import and render the card in Node without pulling in a full DOM
// implementation (jsdom etc).
class FakeShadowRoot {
  constructor() {
    this._html = "";
    this.activeElement = null;
  }
  set innerHTML(v) {
    this._html = v;
  }
  get innerHTML() {
    return this._html;
  }
  getElementById() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  querySelector() {
    return null;
  }
}

globalThis.HTMLElement = class HTMLElement {
  attachShadow() {
    this.shadowRoot = new FakeShadowRoot();
    return this.shadowRoot;
  }
};

// _requestRender() checks `shadowRoot.activeElement instanceof HTMLInputElement` to avoid rebuilding
// the DOM under an input being actively edited — FakeShadowRoot's activeElement is always null, so
// this never actually matches, but the class must exist for the instanceof check itself to run.
globalThis.HTMLInputElement = class HTMLInputElement {};

const registry = {};
globalThis.customElements = {
  define(tag, ctor) {
    registry[tag] = ctor;
  },
};
globalThis.window = globalThis;

export function getCardClass(tag) {
  return registry[tag];
}
