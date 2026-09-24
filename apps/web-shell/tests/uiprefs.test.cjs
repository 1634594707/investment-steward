const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');

const source = readFileSync(require.resolve('../src/shell/uiprefs.ts'), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
function harness(initial, fail = false) {
  let stored = initial;
  const dataset = { density: 'comfortable', mkt: 'cn' };
  const events = [];
  const context = {
    exports: {}, require: () => ({}), document: { body: { dataset } },
    localStorage: { getItem: () => stored, setItem: (_, value) => { if (fail) throw new Error('Quota exceeded'); stored = value; } },
    window: { dispatchEvent: event => events.push(event.type) },
    CustomEvent: class { constructor(type) { this.type = type; } },
  };
  vm.runInNewContext(compiled, context);
  return { api: context.exports, dataset, events, stored: () => stored };
}
test('successful saves persist before changing the UI and notifying subscribers', () => {
  const h = harness(null);
  assert.equal(h.api.saveUiPrefs({ density: 'compact', marketColors: 'legacy' }), true);
  assert.deepEqual(JSON.parse(h.stored()), { density: 'compact', marketColors: 'legacy' });
  assert.deepEqual(h.dataset, { density: 'compact', mkt: 'legacy' });
  assert.deepEqual(h.events, ['steward-ui-prefs-changed']);
});
test('failed storage leaves persisted preferences and UI unchanged', () => {
  const initial = JSON.stringify({ density: 'comfortable', marketColors: 'cn' });
  const h = harness(initial, true);
  assert.equal(h.api.saveUiPrefs({ density: 'compact', marketColors: 'legacy' }), false);
  assert.equal(h.stored(), initial);
  assert.deepEqual(h.dataset, { density: 'comfortable', mkt: 'cn' });
  assert.deepEqual(h.events, []);
});
test('invalid stored preferences resolve to the shared defaults', () => {
  const h = harness('{broken');
  assert.equal(JSON.stringify(h.api.loadUiPrefs()), JSON.stringify(h.api.DEFAULT_UI_PREFS));
});
