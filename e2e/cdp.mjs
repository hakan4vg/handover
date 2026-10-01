// Evaluates one expression in a running app's webview through the DevTools
// protocol, the way the UI itself calls the core. Used by native.py.
//   node e2e/cdp.mjs <port> <window-url-substring> <expression>
// Prints the JSON result; exits 1 with the error text on failure.
const [port, match, expression] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const target = targets.find((t) => t.type === 'page' && t.url.includes(match));
if (!target) {
  console.error(`no page matching ${match}: ${targets.map((t) => t.url).join(', ')}`);
  process.exit(1);
}
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }));
const reply = await new Promise((resolve) => { socket.onmessage = (event) => resolve(JSON.parse(event.data)); });
socket.close();
const { result, exceptionDetails } = reply.result ?? {};
if (exceptionDetails) {
  console.error(exceptionDetails.exception?.description ?? exceptionDetails.text);
  process.exit(1);
}
console.log(JSON.stringify(result?.value ?? null));
