// part:imports
'use strict';
const http = require('http');
const crypto = require('crypto');
const url = require('url');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.env.PORT, 10) || 8100;
const HOST = '127.0.0.1';
const ROOT = __dirname;
const DATA_DIR = path.join(ROOT, 'data');
const PUBLIC_DIR = path.join(ROOT, 'public');
const USERS_FILE = path.join(DATA_DIR, 'users.json');
const NOTES_FILE = path.join(DATA_DIR, 'notes.json');
const MAX_BODY = 1 << 20;        // 1 MiB request cap
const MIN_PASSWORD = 8;
const RL_MAX = 5;                // failed logins before lockout
const RL_WINDOW_MS = 60000;      // lockout window

function ensureDirs() {
  for (const d of [DATA_DIR, PUBLIC_DIR]) {
    try { fs.mkdirSync(d, { recursive: true }); } catch (e) { /* already there */ }
  }
  const index = path.join(PUBLIC_DIR, 'index.html');
  if (!fs.existsSync(index)) {
    fs.writeFileSync(index,
      '<!doctype html><html><head><meta charset="utf-8"><title>Secure Notes</title>'
      + '</head><body><h1>Secure Notes</h1><p>Sign in to keep private notes.</p></body></html>');
  }
}
ensureDirs();

// part:store
function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (e) { return fallback; }
}
function writeJson(file, value) {
  fs.writeFileSync(file, JSON.stringify(value));
}
// users: null-prototype map username -> {salt, hash}; notes: array of note objects.
const users = Object.create(null);
(function loadUsers() {
  const raw = readJson(USERS_FILE, {});
  if (raw && typeof raw === 'object') {
    for (const k of Object.keys(raw)) users[k] = raw[k];
  }
})();
let notes = readJson(NOTES_FILE, []);
if (!Array.isArray(notes)) notes = [];
function saveUsers() { writeJson(USERS_FILE, users); }
function saveNotes() { writeJson(NOTES_FILE, notes); }

// part:hashing
function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  const derived = crypto.scryptSync(password, salt, 64);
  return { salt: salt.toString('hex'), hash: derived.toString('hex') };
}
function verifyPassword(password, record) {
  if (!record || typeof record.salt !== 'string' || typeof record.hash !== 'string') return false;
  let derived;
  try { derived = crypto.scryptSync(password, Buffer.from(record.salt, 'hex'), 64); }
  catch (e) { return false; }
  const stored = Buffer.from(record.hash, 'hex');
  if (derived.length !== stored.length) return false;
  return crypto.timingSafeEqual(derived, stored);
}

// part:sessions
const sessions = Object.create(null); // token -> username
function createSession(username) {
  const token = crypto.randomBytes(32).toString('hex');
  sessions[token] = username;
  return token;
}
function destroySession(token) { if (token) delete sessions[token]; }
function parseCookies(req) {
  const out = Object.create(null);
  const raw = req.headers['cookie'];
  if (!raw) return out;
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}
function sessionUser(req) {
  const token = parseCookies(req)['session'];
  if (!token) return null;
  return sessions[token] || null;
}
function sessionCookie(token) {
  return 'session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600';
}
function clearCookie() {
  return 'session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0';
}

// part:headers
function baseHeaders(contentType) {
  return {
    'Content-Type': contentType,
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy':
      "default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    'Referrer-Policy': 'no-referrer',
  };
}
function send(res, status, contentType, body, extraHeaders) {
  const headers = baseHeaders(contentType);
  if (extraHeaders) for (const k of Object.keys(extraHeaders)) headers[k] = extraHeaders[k];
  res.writeHead(status, headers);
  res.end(body);
}
function sendJson(res, status, obj, extraHeaders) {
  send(res, status, 'application/json; charset=utf-8', JSON.stringify(obj), extraHeaders);
}

// part:jsonguard
// Parse untrusted JSON without ever letting __proto__/constructor/prototype through,
// so no downstream merge or lookup can reach Object.prototype.
function safeParse(text) {
  return JSON.parse(text, function (key, value) {
    if (key === '__proto__' || key === 'constructor' || key === 'prototype') return undefined;
    return value;
  });
}

// part:ratelimit
const failures = Object.create(null); // key -> {count, first}
function limitKey(username, ip) { return username ? ('user:' + username) : ('ip:' + ip); }
function isLocked(key) {
  const f = failures[key];
  if (!f) return false;
  if (Date.now() - f.first > RL_WINDOW_MS) { delete failures[key]; return false; }
  return f.count >= RL_MAX;
}
function recordFailure(key) {
  const now = Date.now();
  const f = failures[key];
  if (!f || now - f.first > RL_WINDOW_MS) failures[key] = { count: 1, first: now };
  else f.count += 1;
}
function clearFailures(key) { delete failures[key]; }

// part:register
const USERNAME_RE = /^[A-Za-z0-9_]{3,32}$/;
function reservedName(name) {
  return name === '__proto__' || name === 'constructor' || name === 'prototype';
}
function handleRegister(req, res, body) {
  let data;
  try { data = safeParse(body || '{}'); } catch (e) { return sendJson(res, 400, { error: 'invalid json' }); }
  const username = typeof data.username === 'string' ? data.username : '';
  const password = typeof data.password === 'string' ? data.password : '';
  if (!USERNAME_RE.test(username) || reservedName(username)) {
    return sendJson(res, 400, { error: 'invalid username' });
  }
  if (password.length < MIN_PASSWORD) {
    return sendJson(res, 400, { error: 'weak password' });
  }
  if (users[username]) return sendJson(res, 409, { error: 'username taken' });
  users[username] = hashPassword(password);
  saveUsers();
  sendJson(res, 201, { ok: true, username: username });
}

// part:login
function handleLogin(req, res, body, ip) {
  let data;
  try { data = safeParse(body || '{}'); } catch (e) { return sendJson(res, 400, { error: 'invalid json' }); }
  const username = typeof data.username === 'string' ? data.username : '';
  const password = typeof data.password === 'string' ? data.password : '';
  const key = limitKey(username, ip);
  if (isLocked(key)) return sendJson(res, 429, { error: 'too many attempts' });
  const record = users[username];
  if (!record || !verifyPassword(password, record)) {
    recordFailure(key);
    return sendJson(res, 401, { error: 'invalid credentials' });
  }
  clearFailures(key);
  const token = createSession(username);
  sendJson(res, 200, { ok: true, username: username }, { 'Set-Cookie': sessionCookie(token) });
}

// part:logout
function handleLogout(req, res) {
  destroySession(parseCookies(req)['session']);
  sendJson(res, 200, { ok: true }, { 'Set-Cookie': clearCookie() });
}

// part:noteslist
function handleNotesList(req, res) {
  const user = sessionUser(req);
  if (!user) return sendJson(res, 401, { error: 'auth required' });
  const mine = notes
    .filter(function (n) { return n.owner === user; })
    .map(function (n) { return { id: n.id, title: n.title, body: n.body, created: n.created }; });
  sendJson(res, 200, { notes: mine });
}

// part:notecreate
function handleNoteCreate(req, res, body) {
  const user = sessionUser(req);
  if (!user) return sendJson(res, 401, { error: 'auth required' });
  let data;
  try { data = safeParse(body || '{}'); } catch (e) { return sendJson(res, 400, { error: 'invalid json' }); }
  const title = typeof data.title === 'string' ? data.title : '';
  const bodyText = typeof data.body === 'string' ? data.body : '';
  if (!title && !bodyText) return sendJson(res, 400, { error: 'empty note' });
  const id = crypto.randomBytes(9).toString('hex');
  notes.push({ id: id, owner: user, title: title, body: bodyText, created: Date.now() });
  saveNotes();
  sendJson(res, 201, { id: id });
}

// part:noteget
function findNote(id) {
  for (let i = 0; i < notes.length; i++) if (notes[i].id === id) return notes[i];
  return null;
}
function handleNoteGet(req, res, id) {
  const user = sessionUser(req);
  if (!user) return sendJson(res, 401, { error: 'auth required' });
  const note = findNote(id);
  if (!note || note.owner !== user) return sendJson(res, 404, { error: 'not found' });
  sendJson(res, 200, { id: note.id, title: note.title, body: note.body, created: note.created });
}

// part:notedelete
function handleNoteDelete(req, res, id) {
  const user = sessionUser(req);
  if (!user) return sendJson(res, 401, { error: 'auth required' });
  const idx = notes.findIndex(function (n) { return n.id === id; });
  if (idx < 0 || notes[idx].owner !== user) return sendJson(res, 404, { error: 'not found' });
  notes.splice(idx, 1);
  saveNotes();
  sendJson(res, 200, { ok: true });
}

// part:htmlview
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
function handleNoteView(req, res, id) {
  const user = sessionUser(req);
  if (!user) return send(res, 401, 'text/html; charset=utf-8', '<!doctype html><p>auth required</p>');
  const note = findNote(id);
  if (!note || note.owner !== user) return send(res, 404, 'text/html; charset=utf-8', '<!doctype html><p>not found</p>');
  const html = '<!doctype html><html><head><meta charset="utf-8"><title>'
    + escapeHtml(note.title) + '</title></head><body><h1>' + escapeHtml(note.title)
    + '</h1><pre id="note-body">' + escapeHtml(note.body) + '</pre></body></html>';
  send(res, 200, 'text/html; charset=utf-8', html);
}

// part:staticserve
const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript',
  '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml', '.txt': 'text/plain; charset=utf-8', '.ico': 'image/x-icon',
};
function serveFile(res, abs) {
  fs.readFile(abs, function (err, data) {
    if (err) return send(res, 404, 'text/plain; charset=utf-8', 'not found');
    send(res, 200, MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream', data);
  });
}
function handleStatic(res, rawSubpath) {
  let sub;
  try { sub = decodeURIComponent(rawSubpath); } catch (e) { return send(res, 400, 'text/plain; charset=utf-8', 'bad path'); }
  if (sub.indexOf('\0') >= 0) return send(res, 400, 'text/plain; charset=utf-8', 'bad path');
  sub = sub.replace(/^[/\\]+/, '');                 // drop leading separators (absolute)
  const abs = path.normalize(path.join(PUBLIC_DIR, sub));
  const withSep = PUBLIC_DIR + path.sep;
  if (abs !== PUBLIC_DIR && !abs.startsWith(withSep)) {
    return send(res, 403, 'text/plain; charset=utf-8', 'forbidden');
  }
  serveFile(res, abs);
}

// dispatch: shared wiring, owned by no single block.
function readBody(req, cb) {
  let size = 0;
  const chunks = [];
  req.on('data', function (c) {
    size += c.length;
    if (size > MAX_BODY) { req.destroy(); return; }
    chunks.push(c);
  });
  req.on('end', function () { cb(Buffer.concat(chunks).toString('utf8')); });
  req.on('error', function () { cb(''); });
}
function clientIp(req) { return (req.socket && req.socket.remoteAddress) || ''; }

const server = http.createServer(function (req, res) {
  const parsed = url.parse(req.url);
  const pathname = parsed.pathname || '/';
  const method = req.method;
  const ip = clientIp(req);

  // Prototype-pollution probe (no auth): reports whether Object.prototype stayed clean.
  if (method === 'GET' && pathname === '/api/_probe') {
    const polluted = ('polluted' in Object.prototype) || ({}).polluted !== undefined;
    return sendJson(res, 200, { safe: !polluted });
  }

  if (pathname === '/api/notes' && method === 'GET') return handleNotesList(req, res);
  if (pathname === '/api/notes' && method === 'POST') return readBody(req, function (b) { handleNoteCreate(req, res, b); });

  let m = pathname.match(/^\/api\/notes\/([A-Za-z0-9_-]+)$/);
  if (m) {
    if (method === 'GET') return handleNoteGet(req, res, m[1]);
    if (method === 'DELETE') return handleNoteDelete(req, res, m[1]);
    return send(res, 405, 'text/plain; charset=utf-8', 'method not allowed');
  }

  m = pathname.match(/^\/notes\/([A-Za-z0-9_-]+)\/view$/);
  if (m && method === 'GET') return handleNoteView(req, res, m[1]);

  if (pathname === '/register' && method === 'POST') return readBody(req, function (b) { handleRegister(req, res, b); });
  if (pathname === '/login' && method === 'POST') return readBody(req, function (b) { handleLogin(req, res, b, ip); });
  if (pathname === '/logout' && method === 'POST') return handleLogout(req, res);

  if (pathname === '/' && method === 'GET') return serveFile(res, path.join(PUBLIC_DIR, 'index.html'));
  if (method === 'GET' && pathname.indexOf('/static/') === 0) {
    return handleStatic(res, pathname.slice('/static/'.length));
  }

  send(res, 404, 'text/plain; charset=utf-8', 'not found');
});

server.listen(PORT, HOST, function () {
  process.stdout.write('secure-notes listening on ' + HOST + ':' + PORT + '\n');
});
