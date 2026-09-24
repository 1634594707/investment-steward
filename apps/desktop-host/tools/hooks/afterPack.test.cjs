const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const afterPack = require('./afterPack.cjs');

function sandbox(t) {
  const appOutDir = fs.mkdtempSync(path.join(os.tmpdir(), 'youzi-pack-'));
  t.after(() => fs.rmSync(appOutDir, { recursive: true, force: true }));
  const keys = path.join(appOutDir, 'resources', 'keys');
  fs.mkdirSync(keys, { recursive: true });
  fs.mkdirSync(path.join(appOutDir, 'resources', 'web', 'assets'), { recursive: true });
  fs.writeFileSync(path.join(keys, 'steward-plugin-publishing.pub.pem'),
    '-----BEGIN PUBLIC KEY-----\ntest-public-placeholder\n-----END PUBLIC KEY-----');
  return { appOutDir, keys };
}

test('package configuration includes only the public verification key', () => {
  const manifest = require('../../package.json');
  const keys = manifest.build.extraResources.filter((r) => r.to.startsWith('keys'));
  assert.deepEqual(keys, [{
    from: '../core-api/keys/steward-plugin-publishing.pub.pem',
    to: 'keys/steward-plugin-publishing.pub.pem',
  }]);
});

test('public-only package passes resource guard', async (t) => {
  await afterPack(sandbox(t));
});

test('stale private key in output aborts packaging', async (t) => {
  const context = sandbox(t);
  fs.writeFileSync(path.join(context.keys, 'steward-plugin-publishing.pem'), 'test-secret-placeholder');
  await assert.rejects(afterPack(context), /only the publisher public key/);
});

test('private key disguised as public key aborts packaging', async (t) => {
  const context = sandbox(t);
  fs.writeFileSync(path.join(context.keys, 'steward-plugin-publishing.pub.pem'),
    '-----BEGIN PRIVATE KEY-----\ntest-placeholder');
  await assert.rejects(afterPack(context), /invalid public verification key/);
});

test('missing key aborts packaging', async (t) => {
  const context = sandbox(t);
  fs.unlinkSync(path.join(context.keys, 'steward-plugin-publishing.pub.pem'));
  await assert.rejects(afterPack(context), /only the publisher public key/);
});
