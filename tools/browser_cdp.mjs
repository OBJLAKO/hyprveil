// Evaluate only in a disposable Chromium profile created by portal_smoke.py.
import fs from 'node:fs';
const [portFile, expectedURL, expression] = process.argv.slice(2);
const runtime = process.env.XDG_RUNTIME_DIR;
const realFile = fs.realpathSync(portFile);
if (!runtime?.startsWith('/tmp/hv-') || !realFile.startsWith(runtime + '/browser-profile-') ||
    !fs.existsSync(runtime + '/.hyprveil-lab')) throw Error('explicit marked lab profile required');
const port = Number(fs.readFileSync(portFile, 'utf8').split('\n')[0]);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw Error('invalid debug port');
const url = new URL(expectedURL);
if (url.hostname !== '127.0.0.1') throw Error('test page must be loopback');
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = pages.find(p => p.type === 'page' && p.url === expectedURL);
if (!target) throw Error('disposable test page not found');
const socketURL = new URL(target.webSocketDebuggerUrl);
if (socketURL.hostname !== '127.0.0.1' || Number(socketURL.port) !== port) throw Error('wrong debug endpoint');
const socket = new WebSocket(socketURL);
const timeout = setTimeout(() => { socket.close(); process.exit(2); }, 35000);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
const response = new Promise((resolve, reject) => {
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id !== 1) return;
    if (message.error) reject(Error(JSON.stringify(message.error)));
    else resolve(message.result);
  };
});
socket.send(JSON.stringify({id: 1, method: 'Runtime.evaluate', params: {
  expression, awaitPromise: true, returnByValue: true, userGesture: true
}}));
try {
  const result = await response;
  if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description ?? 'page exception');
  process.stdout.write(JSON.stringify(result.result.value) + '\n');
} finally {
  clearTimeout(timeout);
  socket.close();
}
