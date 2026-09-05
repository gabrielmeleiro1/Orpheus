import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { startFrontend } from '../lib/frontendProcess.mjs';
import { cleanupAll } from '../lib/cleanupRegistry.mjs';

test('an unrelated listener cannot satisfy frontend child readiness', async () => {
  const unrelated = http.createServer((_req,res)=>res.end('Not Orpheus'));
  await new Promise(r=>unrelated.listen(0,'127.0.0.1',r));
  const port = unrelated.address().port;
  try {
    await assert.rejects(startFrontend({desktopDir:path.resolve(import.meta.dirname,'../../../desktop'),port,log:()=>{},readyTimeoutMs:5000}),/in use|early|EADDRINUSE/i);
  } finally {
    await cleanupAll();
    await new Promise(r=>unrelated.close(r));
  }
});

test('owned IPv4 frontend serves the app without disturbing an IPv6 listener', async () => {
  const unrelated = http.createServer((_req, res) => res.end('IPv6 service'));
  await new Promise((resolve, reject) => {
    unrelated.once('error', reject);
    unrelated.listen(0, '::1', resolve);
  });
  const port = unrelated.address().port;
  let frontend;
  try {
    frontend = await startFrontend({ desktopDir: path.resolve(import.meta.dirname, '../../../desktop'), port, log: () => {} });
    const owned = await fetch(`http://127.0.0.1:${port}`);
    assert.match(await owned.text(), /src\/main\.tsx/);
    assert.equal(await (await fetch(`http://[::1]:${port}`)).text(), 'IPv6 service');
  } finally {
    await frontend?.stop();
    await new Promise(resolve => unrelated.close(resolve));
  }
});
