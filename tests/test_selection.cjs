/* Run with node --test tests/test_selection.cjs; no browser or API calls. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');

function table(rows = [2, 4, 7, 8, 12, 15]) {
  // Only the selection counter and row painting need a DOM; do not boot the app.
  const node = {querySelectorAll: () => []};
  const context = vm.createContext({
    window: {addEventListener() {}},
    document: {querySelector: () => node},
  });
  const run = script => vm.runInContext(script, context);
  run(source);
  run(`S.rows = ${JSON.stringify(rows.map(row => ({row})))}; S.loaded = true;`);
  return {
    click(row, modifiers = {}, checkbox = false) {
      run(`selectRow(${row}, ${JSON.stringify(modifiers)}, ${checkbox});`);
    },
    selected: () => JSON.parse(run('JSON.stringify([...S.sel].sort((a,b) => a-b))')),
    run,
  };
}

test('plain row click replaces selection; checkbox and Alt clicks toggle individual rows', () => {
  const t = table();
  t.click(2);
  t.click(7, {altKey: true});
  t.click(12, {}, true);
  assert.deepEqual(t.selected(), [2, 7, 12]);
  t.click(7, {altKey: true});
  assert.deepEqual(t.selected(), [2, 12]);
  t.click(8);
  t.click(8);
  assert.deepEqual(t.selected(), [8]);
  t.click(8, {}, true);
  assert.deepEqual(t.selected(), []);
});

test('Shift ranges include both endpoints and use displayed rows, not sheet row arithmetic', () => {
  const t = table([15, 4, 12, 2, 8]);
  t.click(4);
  t.click(2, {shiftKey: true});
  assert.deepEqual(t.selected(), [2, 4, 12]);
  t.click(12, {shiftKey: true});
  assert.deepEqual(t.selected(), [4, 12]);
  t.click(15, {shiftKey: true});
  assert.deepEqual(t.selected(), [4, 15]);
});

test('Shift without an anchor starts at the clicked row; checkbox Shift also selects a range', () => {
  const t = table();
  t.click(7, {shiftKey: true});
  assert.deepEqual(t.selected(), [7]);
  t.click(15, {shiftKey: true}, true);
  assert.deepEqual(t.selected(), [7, 8, 12, 15]);
});

test('Alt+Shift preserves separate selections while extending or shrinking a range', () => {
  const t = table();
  t.click(15);
  t.click(4, {altKey: true});
  t.click(12, {shiftKey: true, altKey: true});
  assert.deepEqual(t.selected(), [4, 7, 8, 12, 15]);
  t.click(7, {shiftKey: true, altKey: true});
  assert.deepEqual(t.selected(), [4, 7, 15]);
});

test('Ctrl and Command also toggle individual rows', () => {
  const t = table();
  t.click(2);
  t.click(7, {ctrlKey: true});
  t.click(15, {metaKey: true});
  assert.deepEqual(t.selected(), [2, 7, 15]);
});

test('selection is frozen during generation, loading, project switches and appending rows', () => {
  const t = table();
  t.click(4);
  for (const flag of ['running', 'loading', 'projectPending', 'adding']) {
    t.run(`S.${flag} = true;`);
    t.click(15, {shiftKey: true});
    t.click(8, {altKey: true});
    assert.deepEqual(t.selected(), [4]);
    t.run(`S.${flag} = false;`);
  }
});

test('a reset or missing anchor cannot select a stale range in a newly loaded table', () => {
  const t = table();
  t.click(4);
  t.run('resetSelectionAnchor();');
  t.click(12, {shiftKey: true});
  assert.deepEqual(t.selected(), [12]);
  t.run('S.rows = [{row: 1}, {row: 3}]; S.sel.clear();');
  t.click(3, {shiftKey: true});
  assert.deepEqual(t.selected(), [3]);
  t.click(99, {shiftKey: true});
  assert.deepEqual(t.selected(), [3]);
});
