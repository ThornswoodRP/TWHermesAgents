'use strict';
/*
  ws.js

  The WebSocket framing the box bridge (#2753) needs at both ends: bridge.js
  on the box and connect.js in a cloud thread. RFC 6455, binary frames only,
  plus close, ping and pong. Dependency free, like voice\voice-server.js,
  because the box runs it from a plain folder with no npm install.

  What goes through it is an ssh session, which is already encrypted and
  authenticated end to end, so this layer only has to carry bytes in order
  and say when the other end has gone. Carry, at the end, is what lets the
  session go on over a new WebSocket when Cloudflare ends the old one.
*/
const crypto = require('crypto');

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const OP = { CONT: 0x0, TEXT: 0x1, BINARY: 0x2, CLOSE: 0x8, PING: 0x9, PONG: 0xA };

// No frame past this is read. An ssh packet is at most 256 KB and a client
// frame here carries one read of stdin, so a frame larger than this is not
// ssh, and holding it in memory is what a bad peer would want.
const MAX_FRAME = 1024 * 1024;

function accept(key) {
  return crypto.createHash('sha1').update(String(key) + GUID).digest('base64');
}

// One frame. A client masks every frame it sends and a server never does
// (RFC 6455 5.1), so connect.js passes mask true and bridge.js false.
function encode(opcode, payload, mask) {
  const body = payload ? Buffer.from(payload) : Buffer.alloc(0);
  const len = body.length;
  const head = (len < 126 ? 2 : len < 65536 ? 4 : 10) + (mask ? 4 : 0);
  const out = Buffer.allocUnsafe(head + len);
  out[0] = 0x80 | opcode;
  let at = 2;
  if (len < 126) {
    out[1] = len;
  } else if (len < 65536) {
    out[1] = 126;
    out.writeUInt16BE(len, 2);
    at = 4;
  } else {
    out[1] = 127;
    out.writeBigUInt64BE(BigInt(len), 2);
    at = 10;
  }
  if (mask) {
    out[1] |= 0x80;
    const key = crypto.randomBytes(4);
    key.copy(out, at);
    at += 4;
    for (let i = 0; i < len; i++) out[at + i] = body[i] ^ key[i & 3];
  } else {
    body.copy(out, at);
  }
  return out;
}

/*
  Reads frames out of whatever the socket hands over, in pieces of any size.
  push(chunk) returns the messages it completed, each { op, data }, with a
  fragmented message put back together. It throws on anything a peer has no
  business sending: a frame over MAX_FRAME, an unmasked frame to a server or a
  masked one to a client, a control frame that is fragmented or over 125
  bytes, a continuation with nothing to continue.
*/
class Reader {
  constructor(opts) {
    this.masked = !!(opts && opts.masked);   // true on the server side
    this.buf = Buffer.alloc(0);
    this.parts = null;                       // a message in fragments
    this.partOp = 0;
    this.partLen = 0;
  }

  push(chunk) {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    const out = [];
    for (;;) {
      const f = this.next();
      if (!f) break;
      if (f.op >= 0x8) {
        if (!f.fin || f.data.length > 125) throw new Error('a control frame that is fragmented or longer than 125 bytes');
        out.push({ op: f.op, data: f.data });
        continue;
      }
      if (f.op === OP.CONT) {
        if (!this.parts) throw new Error('a continuation frame with no message to continue');
      } else {
        if (this.parts) throw new Error('a new message before the last one finished');
        this.parts = [];
        this.partOp = f.op;
        this.partLen = 0;
      }
      this.parts.push(f.data);
      this.partLen += f.data.length;
      if (this.partLen > MAX_FRAME) throw new Error('a message larger than ' + MAX_FRAME + ' bytes');
      if (f.fin) {
        out.push({ op: this.partOp, data: Buffer.concat(this.parts, this.partLen) });
        this.parts = null;
      }
    }
    return out;
  }

  next() {
    const b = this.buf;
    if (b.length < 2) return null;
    const fin = (b[0] & 0x80) !== 0;
    if (b[0] & 0x70) throw new Error('a frame with reserved bits set');
    const op = b[0] & 0x0f;
    if ([OP.CONT, OP.TEXT, OP.BINARY, OP.CLOSE, OP.PING, OP.PONG].indexOf(op) < 0) throw new Error('an unknown opcode ' + op);
    const isMasked = (b[1] & 0x80) !== 0;
    if (isMasked !== this.masked) throw new Error(this.masked ? 'an unmasked frame from a client' : 'a masked frame from a server');
    let len = b[1] & 0x7f;
    let at = 2;
    if (len === 126) {
      if (b.length < 4) return null;
      len = b.readUInt16BE(2);
      at = 4;
    } else if (len === 127) {
      if (b.length < 10) return null;
      const big = b.readBigUInt64BE(2);
      if (big > BigInt(MAX_FRAME)) throw new Error('a frame larger than ' + MAX_FRAME + ' bytes');
      len = Number(big);
      at = 10;
    }
    if (len > MAX_FRAME) throw new Error('a frame larger than ' + MAX_FRAME + ' bytes');
    const keyAt = at;
    if (isMasked) at += 4;
    if (b.length < at + len) return null;
    let data = b.subarray(at, at + len);
    if (isMasked) {
      const copy = Buffer.allocUnsafe(len);
      for (let i = 0; i < len; i++) copy[i] = data[i] ^ b[keyAt + (i & 3)];
      data = copy;
    } else {
      data = Buffer.from(data);
    }
    this.buf = b.subarray(at + len);
    return { fin, op, data };
  }
}

/*
  What one end of a session that outlives its WebSocket keeps (#3214).
  Cloudflare, between every machine and the box since #2995, ends a long
  WebSocket whenever it restarts the server holding it: measured 27 Oct 2026,
  four silent 11-minute box runs from a cloud thread each lost theirs after
  329 to 588 s, two at the same millisecond, and Cloudflare's own WebSocket
  page says a client must expect it. The ssh inside does not have to end with
  it. Each end counts the ssh bytes it has sent and received, keeps what it
  sent until the other end says, in an "ack <n>" text frame, that it has the
  first n bytes, and on a new WebSocket sends again everything past what the
  other end says it got. bridge.js and connect.js each hold one.
    out(chunk)   a chunk that went out, or would have with no WebSocket up
    ack(n)       the far end has the first n bytes: what it has is let go.
                 Throws for an n below what it said before or past what was
                 sent, which only a broken far end says
    since(n)     the chunks past byte n, to send again, after ack(n)
    took(n)      n bytes came in; got is the count, owed what was not acked
*/
class Carry {
  constructor() {
    this.sent = 0;
    this.acked = 0;
    this.kept = [];
    this.keptBytes = 0;
    this.got = 0;
    this.told = 0;
  }

  out(chunk) {
    this.kept.push(chunk);
    this.keptBytes += chunk.length;
    this.sent += chunk.length;
  }

  ack(n) {
    if (!Number.isSafeInteger(n) || n < this.acked || n > this.sent) throw new Error('an ack for byte ' + n + ', when ' + this.acked + ' were acked and ' + this.sent + ' sent');
    let drop = n - this.acked;
    while (drop > 0) {
      const c = this.kept[0];
      if (c.length <= drop) {
        this.kept.shift();
        drop -= c.length;
        this.keptBytes -= c.length;
      } else {
        this.kept[0] = c.subarray(drop);
        this.keptBytes -= drop;
        drop = 0;
      }
    }
    this.acked = n;
  }

  since(n) {
    this.ack(n);
    return this.kept.slice();
  }

  took(n) {
    this.got += n;
  }

  get owed() {
    return this.got - this.told;
  }
}

module.exports = { GUID, OP, MAX_FRAME, accept, encode, Reader, Carry };
