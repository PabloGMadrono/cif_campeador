// Run with: node --test tests/test_invoice_dashboard.cjs
// Exercise the generated dashboard script without OCR or browser dependencies.
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const template = readFileSync(join(__dirname, 'templates/invoice_dashboard.html'), 'utf8');
const script = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

function dashboard(persisted = new Map(), blocked = false) {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: ['run-editor', 'rename-message'].includes(id), value: '', checked: true,
      textContent: '', innerHTML: '', style: {}, focus() {}, querySelectorAll() { return []; },
    });
    return nodes.get(id);
  };
  const fields = Object.fromEntries(Array.from({length: 8}, (_, i) => ['f' + i, 'Field ' + i]));
  const values = Object.fromEntries(Object.keys(fields).map(name => [name, 'value']));
  const invoice = {
    filename: 'one.png', category: 'easy', status: 'failed', matched: 5, total: 8, accuracy_pct: 62.5,
    mismatches: ['f0', 'f1', 'f2'], expected: values, obtained: {...values},
    image: null, error: null, duration_seconds: 1,
  };
  const run = {
    run_id: 'first', status: 'completed', threshold_pct: 70, invoices: [invoice],
    started_at: '2026-09-11T00:00:00Z', finished_at: '2026-09-11T00:00:01Z',
    updated_at: '2026-09-11T00:00:01Z', extractor: 'test.Extractor',
  };
  const second = JSON.parse(JSON.stringify(run));
  second.run_id = 'second';
  second.status = 'interrupted';
  second.invoices.push(
    {...invoice, filename: 'pending.png', category: 'medium', status: 'pending', obtained: null, matched: null, accuracy_pct: null, mismatches: []},
    {...invoice, filename: 'error.png', category: 'hard', status: 'error', obtained: null, matched: 0, accuracy_pct: 0, mismatches: Object.keys(fields)},
  );
  const runs = [run, second];
  get('report-data').textContent = JSON.stringify({runs, fields, summaries: runs.map(r => ({...r, images_scored: 1, images_total: r.invoices.length, accuracy_pct: 62.5, outcome: 'failed'}))});
  const context = vm.createContext({
    document: {getElementById: get, querySelectorAll() { return []; }},
    location: {hash: '', pathname: '/reports/dashboard.html', reload() {}},
    history: {replaceState() {}}, URLSearchParams,
    sessionStorage: {getItem() { return null; }, setItem() {}},
    localStorage: {
      getItem: key => persisted.get(key) || null,
      setItem(key, value) { if (blocked) throw Error('Storage blocked'); persisted.set(key, value); },
      removeItem(key) { if (blocked) throw Error('Storage blocked'); persisted.delete(key); },
    },
    setTimeout() { return 1; }, clearTimeout() {},
  });
  vm.runInContext(script, context);
  return {get, evaluate: code => vm.runInContext(code, context)};
}

test('manual verdicts update every score view and survive reload', () => {
  const saved = new Map();
  let page = dashboard(saved);
  page.evaluate("setFieldReview('first','one.png','f0','correct');render();");
  assert.equal(page.evaluate('summary().accuracy'), 75);
  assert.equal(page.evaluate('run.invoices[0].status'), 'passed');
  assert.match(page.get('metrics').innerHTML, /75.00%/);
  assert.match(page.get('detail').innerHTML, /auto: 62.50%/);
  assert.match(page.get('history').innerHTML, /75.00%/);
  assert.match(page.get('field-summary').innerHTML, /100%/);
  assert.match(page.get('invoice-list').innerHTML, /75%/);
  assert.match(page.get('notice').textContent, /Automated accuracy: 62.50%/);
  page = dashboard(saved);
  assert.equal(page.evaluate('summary().accuracy'), 75);
  assert.equal(page.evaluate('run.invoices[0].automated.matched'), 5);
  page.evaluate("setFieldReview('first','one.png','f3','wrong');render();");
  assert.equal(page.evaluate('summary().accuracy'), 62.5);
  assert.equal(page.evaluate('run.invoices[0].status'), 'failed');
});

test('Auto restores original scoring and review isolation by run and field', () => {
  const page = dashboard();
  page.evaluate("setFieldReview('first','one.png','f0','correct')");
  assert.equal(page.evaluate('summary(data.runs[1]).accuracy'), 31.25);
  page.evaluate("setFieldReview('first','one.png','f0','auto')");
  assert.equal(page.evaluate('summary().accuracy'), 62.5);
  assert.equal(page.evaluate('summary().reviewed'), 0);
});

test('pending invoices, errors, unknown fields and invalid verdicts cannot be overridden', () => {
  const page = dashboard();
  for (const expression of [
    "setFieldReview('second','pending.png','f0','correct')",
    "setFieldReview('second','error.png','f0','correct')",
    "setFieldReview('first','one.png','missing','correct')",
    "setFieldReview('first','one.png','f0','maybe')",
  ]) assert.throws(() => page.evaluate(expression));
  assert.equal(page.evaluate('summary().accuracy'), 62.5);
});

test('failed persistence leaves scores unchanged; malformed saved verdicts are ignored', () => {
  const saved = new Map();
  const page = dashboard(saved, true);
  assert.throws(() => page.evaluate("setFieldReview('first','one.png','f0','correct')"));
  assert.equal(page.evaluate('summary().accuracy'), 62.5);
  saved.set(page.evaluate("reviewKey('first','one.png','f0')"), 'invalid');
  assert.equal(dashboard(saved).evaluate('summary().accuracy'), 62.5);
});

test('strict threshold applies to manual scores and original values are preserved', () => {
  const page = dashboard();
  page.evaluate("run.threshold_pct=75;setFieldReview('first','one.png','f0','correct');render();");
  assert.equal(page.evaluate('summary().accuracy'), 75);
  assert.equal(page.evaluate('run.invoices[0].status'), 'failed');
  assert.equal(page.evaluate('run.invoices[0].expected.f0'), 'value');
  assert.equal(page.evaluate('run.invoices[0].obtained.f0'), 'value');
  assert.equal(page.evaluate('run.invoices[0].automated.accuracy_pct'), 62.5);
});

test('categories include zero-score errors, exclude pending and update after review', () => {
  const saved = new Map();
  const page = dashboard(saved);
  page.evaluate("chooseRun('second')");
  const categories = JSON.parse(page.evaluate('JSON.stringify(categorySummaries())'));
  assert.deepEqual(categories.map(r => r.category), ['easy', 'medium', 'hard']);
  assert.deepEqual(categories.map(r => r.accuracy), [62.5, null, 0]);
  assert.match(page.get('category-summary').innerHTML, /Not evaluated/);
  assert.match(page.get('category-summary').innerHTML, /Overall/);
  page.evaluate("setFieldReview('second','one.png','f0','correct');render()");
  assert.equal(page.evaluate('categorySummaries()[0].accuracy'), 75);
  assert.equal(page.evaluate('summary().accuracy'), 37.5);
  assert.match(page.get('category-summary').innerHTML, /Manually reviewed/);
  const reloaded = dashboard(saved);
  reloaded.evaluate("chooseRun('second')");
  assert.equal(reloaded.evaluate('categorySummaries()[0].accuracy'), 75);
});

test('overall weights invoices, and legacy records have an uncategorized fallback', () => {
  const page = dashboard();
  page.evaluate("chooseRun('second');run.invoices.push({...run.invoices[0],filename:'extra.png'});render()");
  assert.equal(page.evaluate('categorySummaries()[0].accuracy'), 62.5);
  assert.equal(page.evaluate('summary().accuracy'), 100 * 10 / 24);
  assert.match(page.get('category-summary').innerHTML, /41.67%/);
  page.evaluate("delete run.invoices[0].category;render()");
  assert.match(page.get('category-summary').innerHTML, /Uncategorized/);
});
