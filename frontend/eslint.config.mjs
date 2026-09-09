import js from "@eslint/js";

const browserGlobals = {
  customElements: "readonly",
  HTMLElement: "readonly",
  HTMLInputElement: "readonly",
  document: "readonly",
  window: "readonly",
  console: "readonly",
  setInterval: "readonly",
  clearInterval: "readonly",
  requestAnimationFrame: "readonly",
};

const nodeGlobals = {
  ...browserGlobals,
  global: "writable",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  process: "readonly",
};

export default [
  js.configs.recommended,
  {
    files: ["solar-planner-card.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: browserGlobals },
  },
  {
    files: ["tests/**/*.js"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module", globals: nodeGlobals },
  },
];
