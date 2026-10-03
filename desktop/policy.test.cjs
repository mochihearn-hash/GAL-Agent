const { test } = require('node:test');
const assert = require('node:assert/strict');
const { allowedPath } = require('./policy.cjs');
const id = 'a'.repeat(32);
test('renderer bridge exposes local app routes only', () => {
  assert.equal(allowedPath('POST', `/sessions/${id}/pause`), true);
  assert.equal(allowedPath('GET', `/sessions/${id}/events?before=100&limit=100`), true);
  for (const value of ['/shutdown', '/control/heartbeat', 'https://example.com', '/../../x', '/configs%2Fsecret', '/sessions?token=secret']) assert.equal(allowedPath('GET', value), false);
  assert.equal(allowedPath('EXEC', '/sessions'), false);
});
