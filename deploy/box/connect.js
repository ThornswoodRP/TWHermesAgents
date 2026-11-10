#!/usr/bin/env node
'use strict';
/*
  connect.js

  ssh's ProxyCommand to the box, on every machine that reaches it: One, each
  person's own PC and a cloud Claude thread. ssh to the box goes inside a
  WebSocket on 443 (#2753, #2891): a cloud thread's only way out is an HTTPS
  proxy, which never carries port 22, and port 22 itself is for no outside
  address once everybody comes this way. This opens wss://thornswood.net/box/
  (through the proxy when there is one), and from then on whatever ssh
  writes on this process's standard input goes to the box's sshd and
  whatever sshd answers comes out on standard output. bridge.js is the other
  end, on the box behind Caddy.

      ProxyCommand node /path/to/connect.js

  Setup-SshKey.ps1 (a PC) and Setup-CloudSsh.ps1 (a cloud thread) write that
  line into ~/.ssh/config; nobody runs this by hand. What it reads, all from
  the environment:

    THORNSWOOD_BOX_TOKEN  this one's own token, sent as "Authorization:
                          Bearer". Left unset when the cloud environment holds
                          it as a network secret instead, which the proxy adds
                          to the request itself, and on a PC, which keeps it
                          in a file:
    THORNSWOOD_BOX_TOKEN_FILE  the file the token is in, read when
                          THORNSWOOD_BOX_TOKEN is not set. When this is not
                          set either, .ssh/thornswood-box-token in the home
                          folder, if it is there. Setup-SshKey.ps1 writes it,
                          readable by its owner only.
    THORNSWOOD_BOX_URL    where the bridge is. wss://thornswood.net/box/ when
                          unset. ws:// only for the harness on loopback.
    HTTPS_PROXY           the proxy, http://host:port. With none it connects
                          straight, which is how One or a harness reaches it.
    THORNSWOOD_BOX_HEAR_MS  how long nothing may come from the bridge
                          before its WebSocket is taken for gone, 110000 when
                          unset (the harness makes it short)
    NODE_EXTRA_CA_CERTS   read by node itself: the cloud proxy re-signs TLS
                          with its own authority, and the container sets this
                          to that authority's bundle.

  The token only opens the WebSocket. Getting a shell still takes the same
  person's key, which the box accepts only from 127.0.0.1, so only through
  the bridge (Grant-BoxAccess.ps1), and ssh still checks the box's host key against
  scripts\vps\vps.json before it sends anything.

  Anything that goes wrong goes to standard error, starting "box bridge:",
  and the exit code is 1. ssh then says the connection closed, with that line
  above it.

  ONE WEBSOCKET IS NOT THE SESSION (#3214). Cloudflare, on the way to the
  box since #2995, ends a long WebSocket whenever it restarts the server
  holding it, which took box runs down 5 to 10 minutes in (bridge.js says
  what was measured). So this asks the bridge for a session it can carry
  over ("X-Thornswood-Resume: new"), and when the WebSocket goes without a
  close, or nothing at all comes from the bridge for HEAR_MS, it opens one
  new WebSocket at once with the session's name and the count of bytes it
  has, and each end sends again what the other is missing (ws.js Carry).
  ssh sees nothing of it. It says nothing on standard error while it does
  so: a line there in the middle of a session is one Windows PowerShell
  cannot read among the far side's CLIXML, and it reports "Cannot process
  the XML from the 'Error' stream" instead of the line (measured 27 Oct
  2026). Only a new WebSocket that does not open is an error, said the
  usual way. A bridge from before then names no session, and with it the
  session ends with its WebSocket as before.

  WHICH HOP (#3297). Until the WebSocket is open, this keeps track of what it
  is waiting for: the TCP connection (its DNS lookup included), the proxy's
  tunnel, TLS, then the answer to the upgrade. Straight from a PC such as
  One, TLS for wss://thornswood.net/box/ is with Cloudflare's edge (#2995),
  so "the answer to the upgrade" is the one stage past Cloudflare's front: it
  waits on Cloudflare reaching Caddy, Caddy reaching the bridge and the
  bridge reaching sshd. (Through the cloud's proxy, which re-signs TLS, TLS
  is with the proxy, and that stage also holds the proxy's own way out.)
  Each failure names the stage it happened in and how long the ones before
  it took. An answer that is not the WebSocket names its status and its
  cf-ray, the id Cloudflare's own records of that request go by.

  ssh's ConnectTimeout counts the wait for sshd's banner, which here is all of
  the opening, and when it runs out ssh ends this (measured 22 Oct 2026, #3297:
  Harden-VPS's ConnectTimeout=10 ended it with only ssh's own "timed out
  during banner exchange" said, ten seconds before OPEN_MS would have). On
  Windows, where One runs it, there is no SIGHUP to catch, so nothing here
  can count on speaking at the end. So an opening that is still going after
  SLOW_MS says where it is then, and again at each stage after that, before
  any caller's ConnectTimeout (8 s is the shortest in scripts/) can cut it
  off. A normal opening never gets there and says nothing: 40 in a row from
  a cloud thread on 22 Oct 2026 each had sshd's banner in 0.3 to 0.6 s.
*/
const net = require('net');
const tls = require('tls');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { OP, accept, encode, Reader, Carry } = require('./ws.js');

function die(why) {
  process.stderr.write('box bridge: ' + why + '\n');
  process.exit(1);
}

const where = new URL(process.env.THORNSWOOD_BOX_URL || 'wss://thornswood.net/box/');
if (where.protocol !== 'wss:' && where.protocol !== 'ws:') die('THORNSWOOD_BOX_URL must be a ws:// or wss:// address, not ' + where.protocol);
const secure = where.protocol === 'wss:';
const port = Number(where.port) || (secure ? 443 : 80);
const proxyText = process.env.HTTPS_PROXY || process.env.https_proxy || '';
const token = process.env.THORNSWOOD_BOX_TOKEN || tokenFromFile();
function tokenFromFile() {
  const named = process.env.THORNSWOOD_BOX_TOKEN_FILE || '';
  const file = named || path.join(os.homedir(), '.ssh', 'thornswood-box-token');
  try { return fs.readFileSync(file, 'utf8').trim(); } catch (e) {
    // A file that was named and cannot be read is a mistake worth saying; the
    // default one not being there means this machine sends no token itself.
    if (named) die('THORNSWOOD_BOX_TOKEN_FILE names ' + named + ', which cannot be read (' + e.code + ')');
    return '';
  }
}
// How long the proxy, TLS and the upgrade together may take before this gives
// up, for an ssh with no ConnectTimeout. One with a shorter ConnectTimeout
// ends this first (see WHICH HOP above).
const OPEN_MS = Number(process.env.THORNSWOOD_BOX_OPEN_MS) || 20000;
// When an opening that has not finished says where it is. Above the slowest
// opening measured, below the shortest ConnectTimeout in scripts/.
const SLOW_MS = Number(process.env.THORNSWOOD_BOX_SLOW_MS) || 4000;

// What the opening is waiting for now, since when, and what it already
// passed and how long each took.
const began = Date.now();
let waiting = '';
let waitingSince = began;
const passed = [];
let slow = false;
function secs(ms) { return (ms / 1000).toFixed(1) + ' s'; }
function waitFor(what) {
  const now = Date.now();
  if (waiting) passed.push(waiting + ' ' + (now - waitingSince) + ' ms');
  waiting = what;
  waitingSince = now;
  if (slow && what) process.stderr.write('box bridge: now waiting for ' + what + ' (' + secs(now - began) + ' in)\n');
}
function stage() {
  return 'waiting ' + secs(Date.now() - waitingSince) + ' for ' + waiting +
    (passed.length ? '; before that ' + passed.join(', ') : '');
}

// Reads one HTTP head off a socket: the status line and headers, and whatever
// came after them in the same read, which already belongs to what follows.
function readHead(sock, what) {
  return new Promise((resolve, reject) => {
    let got = Buffer.alloc(0);
    function onData(chunk) {
      got = Buffer.concat([got, chunk]);
      const end = got.indexOf('\r\n\r\n');
      if (end < 0) {
        if (got.length > 16384) { done(); reject(new Error(what + ' sent more than 16 KB with no end to its headers')); }
        return;
      }
      done();
      const lines = got.subarray(0, end).toString('latin1').split('\r\n');
      const status = Number((lines[0].match(/^HTTP\/1\.[01] (\d{3})/) || [])[1] || 0);
      const headers = {};
      for (const l of lines.slice(1)) {
        const i = l.indexOf(':');
        if (i > 0) headers[l.slice(0, i).trim().toLowerCase()] = l.slice(i + 1).trim();
      }
      resolve({ status, line: lines[0], headers, rest: got.subarray(end + 4) });
    }
    function onEnd() { done(); reject(new Error(what + ' closed the connection before it answered')); }
    function onError(e) { done(); reject(e); }
    function done() { sock.off('data', onData); sock.off('end', onEnd); sock.off('error', onError); }
    sock.on('data', onData);
    sock.on('end', onEnd);
    sock.on('error', onError);
  });
}

function connectTcp(host, p) {
  return new Promise((resolve, reject) => {
    const s = net.connect({ host, port: p });
    s.once('connect', () => { s.off('error', reject); resolve(s); });
    s.once('error', reject);
  });
}

async function open(resume) {
  let sock;
  if (proxyText) {
    const proxy = new URL(proxyText);
    waitFor('a TCP connection to the proxy ' + proxy.hostname + ':' + (Number(proxy.port) || 80));
    sock = await connectTcp(proxy.hostname, Number(proxy.port) || 80);
    waitFor('the proxy\'s tunnel to ' + where.hostname + ':' + port);
    sock.write('CONNECT ' + where.hostname + ':' + port + ' HTTP/1.1\r\nHost: ' + where.hostname + ':' + port + '\r\n\r\n');
    const h = await readHead(sock, 'the proxy');
    if (h.status !== 200) throw new Error('the proxy refused the tunnel to ' + where.hostname + ':' + port + ' (' + h.line + ')');
  } else {
    waitFor('a TCP connection to ' + where.hostname + ':' + port);
    sock = await connectTcp(where.hostname, port);
  }
  if (secure) {
    waitFor('TLS with ' + where.hostname);
    sock = await new Promise((resolve, reject) => {
      const t = tls.connect({ socket: sock, servername: where.hostname, ALPNProtocols: ['http/1.1'] });
      t.once('secureConnect', () => { t.off('error', reject); resolve(t); });
      t.once('error', reject);
    });
  }
  const key = crypto.randomBytes(16).toString('base64');
  // A User-Agent of its own: Cloudflare carries this to the box since #2995,
  // and its browser check challenges a request that names no client, which
  // an upgrade cannot answer.
  const lines = [
    'GET ' + (where.pathname || '/') + ' HTTP/1.1',
    'Host: ' + where.host,
    'User-Agent: Thornswood-Bridge',
    'Upgrade: websocket',
    'Connection: Upgrade',
    'Sec-WebSocket-Key: ' + key,
    'Sec-WebSocket-Version: 13',
  ];
  if (token) lines.push('Authorization: Bearer ' + token);
  lines.push('X-Thornswood-Resume: ' + resume);
  waitFor('the answer to the upgrade');
  sock.write(lines.join('\r\n') + '\r\n\r\n');
  const h = await readHead(sock, 'the bridge');
  if (h.status === 410) throw new Error('the bridge no longer holds this session (410): it was held for a while after its WebSocket went, and has ended');
  if (h.status === 401) throw new Error(token ? 'the bridge refused this token (401): it is not one the box holds, or it was taken away' : 'no token was sent, and the bridge lets nobody in without one (401). Setup-SshKey.ps1 asks for it and keeps it in .ssh/thornswood-box-token');
  const ray = h.headers['cf-ray'] ? '; cf-ray ' + h.headers['cf-ray'] : '';
  if (h.status !== 101) throw new Error('the bridge did not open a WebSocket (' + h.line + ray + ')');
  if (h.headers['sec-websocket-accept'] !== accept(key)) throw new Error('the bridge answered the upgrade with the wrong Sec-WebSocket-Accept');
  return { sock, rest: h.rest, headers: h.headers };
}

const timer = setTimeout(() => die('no WebSocket to ' + where.href + ' after ' + OPEN_MS + ' ms, ' + stage()), OPEN_MS);
const slowTimer = setTimeout(() => {
  slow = true;
  process.stderr.write('box bridge: still opening ' + where.href + ' after ' + secs(Date.now() - began) + ', ' + stage() + '\n');
}, SLOW_MS);
// ssh ends this with SIGHUP when its ConnectTimeout runs out (on Linux; on
// Windows it cannot be caught, which is what SLOW_MS is for).
function endedEarly(sig) { die('ssh ended this (' + sig + ') before the WebSocket to ' + where.href + ' opened, ' + stage()); }
process.on('SIGHUP', endedEarly);
process.on('SIGTERM', endedEarly);

// How long a session may hear nothing from the bridge before its WebSocket
// is taken for gone: the bridge pings at least every 50 s while nothing
// moves (bridge.js PING_EVERY), so this is two of those missed.
const HEAR_MS = Number(process.env.THORNSWOOD_BOX_HEAR_MS) || 110000;
const HOLD_MAX = 8 * 1024 * 1024;
const ACK_BYTES = 128 * 1024;
const ACK_MS = 1000;

open('new').then((first) => {
  clearTimeout(timer);
  clearTimeout(slowTimer);
  process.off('SIGHUP', endedEarly);
  process.off('SIGTERM', endedEarly);
  if (slow) {
    waitFor('');
    process.stderr.write('box bridge: opened after ' + secs(Date.now() - began) + ': ' + passed.join(', ') + '\n');
  }
  // The bridge's name for this session when it can carry it over to a new
  // WebSocket (#3214). A bridge from before then names none, and the session
  // ends with its WebSocket as it always did.
  const name = /^[0-9a-f]{32}$/.test(first.headers['x-thornswood-session'] || '') ? first.headers['x-thornswood-session'] : '';
  const carry = name ? new Carry() : null;
  const opened = Date.now();
  let sock = null;
  let reader = null;
  let heard = Date.now();
  let waitDrain = false;
  let closing = false;
  let reopening = false;
  let closeSent = false;

  function sendClose() {
    if (closeSent || !sock) return;
    closeSent = true;
    sock.write(encode(OP.CLOSE, Buffer.from([0x03, 0xe8]), true));
  }

  // Standard input is read only while what it gives can go somewhere: the
  // WebSocket takes more, and what the bridge has not acked stays under
  // HOLD_MAX.
  function flow() {
    if (waitDrain || (carry && carry.keptBytes > HOLD_MAX)) process.stdin.pause();
    else process.stdin.resume();
  }

  function sendAck() {
    if (!sock || !carry.owed) return;
    carry.told = carry.got;
    sock.write(encode(OP.TEXT, Buffer.from('ack ' + carry.got), true));
  }

  function take(chunk) {
    heard = Date.now();
    let msgs;
    try { msgs = reader.push(chunk); } catch (e) { die('the bridge sent a bad frame: ' + e.message); }
    for (const m of msgs) {
      if (m.op === OP.BINARY) {
        if (carry) carry.took(m.data.length);
        if (!process.stdout.write(m.data)) {
          const s = sock;
          s.pause();
          process.stdout.once('drain', () => s.resume());
        }
        if (carry && carry.owed >= ACK_BYTES) sendAck();
      } else if (m.op === OP.PING) {
        sock.write(encode(OP.PONG, m.data, true));
      } else if (m.op === OP.CLOSE) {
        closing = true;
        sendClose();
        sock.end();
        process.stdout.end();
      } else if (m.op === OP.TEXT) {
        const a = carry ? /^ack (\d{1,15})$/.exec(m.data.toString('latin1')) : null;
        if (!a) die('the bridge sent a text frame that is not an ack');
        try { carry.ack(Number(a[1])); } catch (e) { die('the bridge sent ' + e.message); }
        flow();
      }
    }
  }

  // Joins the session to a WebSocket. Events from one it rode before are
  // not this one's and are dropped, so nothing is counted or written twice.
  function attach(s, rest) {
    sock = s;
    reader = new Reader({ masked: false });
    heard = Date.now();
    waitDrain = false;
    const mine = () => sock === s;
    s.on('data', (chunk) => { if (mine()) take(chunk); });
    s.on('drain', () => { if (mine() && waitDrain) { waitDrain = false; flow(); } });
    s.on('error', (e) => { if (mine()) gone('the connection to the bridge broke: ' + e.message); });
    s.on('close', () => { if (mine()) gone('the bridge closed the connection'); });
    if (rest && rest.length) take(rest);
    flow();
  }

  // The WebSocket went. Once ssh is done with it, or after a close frame,
  // that is the end. A session the bridge cannot carry over ends as before
  // #3214: a broken connection is an error, a closed one the end. Any other
  // goes on over a new WebSocket, opened once, at once: the bridge holds its
  // side for a minute (bridge.js RESUME_MS). Nothing is said on standard
  // error unless that fails, because a line there in the middle of a session
  // is a line Windows PowerShell cannot read among the far side's CLIXML
  // ("Cannot process the XML from the 'Error' stream", #3214).
  function gone(why) {
    if (closing) process.exit(0);
    if (!carry) {
      if (/broke/.test(why)) die(why);
      process.exit(0);
    }
    if (reopening) return;
    reopening = true;
    const old = sock;
    sock = null;
    reader = null;
    if (old) old.destroy();
    const lostAfter = secs(Date.now() - opened);
    passed.length = 0;
    waiting = '';
    const t = setTimeout(() => die('the WebSocket to ' + where.href + ' went after ' + lostAfter + ' (' + why + '), and a new one to carry the session on did not open in ' + OPEN_MS + ' ms, ' + stage()), OPEN_MS);
    open(name + ' ' + carry.got).then((next) => {
      clearTimeout(t);
      const got = Number(next.headers['x-thornswood-got']);
      let again;
      try { again = carry.since(got); } catch (e) { die('the bridge took the session on, but from ' + e.message); }
      for (const c of again) next.sock.write(encode(OP.BINARY, c, true));
      reopening = false;
      attach(next.sock, next.rest);
      if (closing) { sendClose(); sock.end(); }
    }).catch((e) => die('the WebSocket to ' + where.href + ' went after ' + lostAfter + ' (' + why + '), and a new one to carry the session on did not open: ' + e.message + ' (' + stage() + ')'));
  }

  attach(first.sock, first.rest);

  if (carry) {
    setInterval(() => {
      if (!sock) return;
      if (Date.now() - heard > HEAR_MS) return gone('nothing came from the bridge for ' + secs(Date.now() - heard));
      sendAck();
    }, ACK_MS);
  }

  process.stdin.on('data', (chunk) => {
    if (carry) carry.out(chunk);
    if (sock && !sock.write(encode(OP.BINARY, chunk, true))) waitDrain = true;
    flow();
  });
  function inputDone() {
    closing = true;
    if (!sock) return;
    sendClose();
    sock.end();
  }
  process.stdin.on('end', inputDone);
  // ssh ends its ProxyCommand with SIGHUP a moment after it closes standard
  // input (measured 21 Oct 2026, #2961: 2 ms apart). Left to the default,
  // that killed this before the close frame was out, so bridge.js held the
  // session's slot until the proxy and Caddy let the connection go, about
  // 11 s later, and one install's ssh calls a second apart filled all eight
  // slots and were refused. The close goes out and this ends when the
  // connection does, the same as when standard input ends.
  process.on('SIGHUP', inputDone);
}).catch((e) => die(e.message + ' (' + stage() + ')'));
