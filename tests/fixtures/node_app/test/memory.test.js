const test = require('node:test');
const assert = require('node:assert');

// Deliberately holds ~150 MB so the profiler has a known memory floor to find
// (Node baseline is ~40-60 MB, so the true floor is roughly 256m).
test('needs about 150mb', () => {
  const block = Buffer.alloc(150 * 1024 * 1024, 1);
  assert.strictEqual(block[block.length - 1], 1);
});
