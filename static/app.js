/**
 * Vokk AI — frontend application
 * by Nibra Cyber, a technological branch of Nibra Ecos
 *
 * Features:
 *   - Neon char-by-char text animation with rainbow leader
 *   - Cinematic BLOCKED censor bar (text streams then bar slams over it)
 *   - Auth: sign in, sign out, session awareness
 *   - Conversation history sidebar (load, delete, rename, export)
 *   - Conversation search
 *   - Tool bar: search, scrape, crypto, song, image
 *   - Claude-style welcome homepage with quick-start cards
 *   - Markdown rendering via marked.js after animation completes
 *   - Settings panel: font size, sound fx toggle, auto-scroll
 *   - Keyboard shortcuts
 *   - Multi-mode: chat, bignice ai, image gen
 *   - Bignice memory: clean sectioned display
 *   - LIVE interactive blocks: playable games and experiences inside chat bubbles
 *   - Points / achievement notification system
 */

"use strict";

// ── constants & globals ────────────────────────────────────────────────────────
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const messages = [];           // live conversation history array
let pendingTool    = null;     // tool result waiting to be sent
let currentConvId  = null;     // active conversation id (null when not logged in)
let authState      = { logged_in: false, email: null, user_id: null };
let isStreaming    = false;    // prevent double-send during animation
let settings       = {};       // user preferences (loaded from localStorage)
let vokkPoints     = parseInt(localStorage.getItem('vokk-points') || '0');

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Cinematic censor bar label — exact as specified
const CENSOR_TEXT = '[this answer or instructions contain highly illegal or graphical or sexual content ...]';

// ── live interactive block system ─────────────────────────────────────────────
/**
 * parseAllSegments: splits text into text | blocked | live segments.
 * LIVE: ⟪LIVE⟫...⟪/LIVE⟫ — embedded interactive HTML experience
 * BLOCKED: ⟪BLOCKED⟫...⟪/BLOCKED⟫ — censor bar
 */
function parseAllSegments(text) {
  const segs = [];
  const re   = /⟪(LIVE|BLOCKED)⟫([\s\S]*?)⟪\/\1⟫/g;
  let last   = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) segs.push({ type: 'text', value: text.slice(last, m.index) });
    segs.push({ type: m[1].toLowerCase(), value: m[2] });
    last = re.lastIndex;
  }
  if (last < text.length) segs.push({ type: 'text', value: text.slice(last) });
  return segs;
}

function injectLiveBlock(el, htmlContent) {
  const wrapper = document.createElement('div');
  wrapper.className = 'live-block';

  // Header bar
  const header = document.createElement('div');
  header.className = 'live-block-header';
  header.innerHTML = `
    <span class="live-badge">⚡ live</span>
    <span class="live-title">interactive experience</span>
    <button class="live-fullscreen" title="fullscreen">⛶</button>
    <button class="live-close" title="close">×</button>
  `;
  wrapper.appendChild(header);

  // Sandboxed iframe
  const frame = document.createElement('iframe');
  frame.className   = 'live-frame';
  frame.sandbox     = 'allow-scripts allow-forms';
  frame.scrolling   = 'no';
  // Inject dark-theme defaults into the page
  const fullHtml = htmlContent.includes('<html') ? htmlContent : `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{background:#0c0c0d;color:#ececee;font-family:-apple-system,"Inter",sans-serif;
    font-size:14px;height:100%;overflow:hidden}
</style></head><body>${htmlContent}</body></html>`;
  frame.srcdoc = fullHtml;
  wrapper.appendChild(frame);

  // Auto-resize iframe height based on content
  frame.addEventListener('load', () => {
    try {
      const h = frame.contentDocument?.body?.scrollHeight;
      if (h && h > 60) frame.style.height = Math.min(h + 20, 440) + 'px';
    } catch { /* cross-origin fallback */ }
  });

  // Fullscreen button
  header.querySelector('.live-fullscreen').addEventListener('click', () => {
    wrapper.classList.toggle('live-fullscreen-mode');
    const btn = header.querySelector('.live-fullscreen');
    btn.textContent = wrapper.classList.contains('live-fullscreen-mode') ? '⛶' : '⛶';
  });

  // Close button
  header.querySelector('.live-close').addEventListener('click', () => {
    wrapper.style.transition = 'opacity .2s, transform .2s';
    wrapper.style.opacity    = '0';
    wrapper.style.transform  = 'scaleY(.8)';
    setTimeout(() => wrapper.remove(), 220);
  });

  el.appendChild(wrapper);
}

// ── points / achievements ─────────────────────────────────────────────────────
function addPoints(amount, reason) {
  vokkPoints += amount;
  localStorage.setItem('vokk-points', vokkPoints);
  showAchievement(`+${amount} ${reason || 'points'}`, amount);
}

function showAchievement(label, points) {
  const el       = document.createElement('div');
  el.className   = 'achievement-notif';
  el.innerHTML   = `<span class="ach-star">★</span> ${escapeHtml(label)}`;
  document.body.appendChild(el);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => { el.classList.add('show'); });
  });
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 400);
  }, 2800);
}

// Characters for ghost glyph animation (empty BLOCKED blocks)
const GLYPHS = (
  'ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρστυφχψω' +
  '█▓▒░▄▀■□◆◇◈Ψ⊕⊗∑∇∂∞∫≈≠≡±√∴∵'
).split('');

function randomGlyph() {
  return GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
}

// ── utilities ─────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function relativeTime(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr.replace(' ', 'T') + 'Z');
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)        return 'just now';
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => toast('copied!'));
}

// ── settings ──────────────────────────────────────────────────────────────────
const DEFAULT_SETTINGS = {
  fontSize:     15,
  soundEnabled: false,
  autoScroll:   true,
  compactMode:  false,
};

function loadSettings() {
  try {
    const saved = localStorage.getItem('vokk-settings');
    settings = saved ? { ...DEFAULT_SETTINGS, ...JSON.parse(saved) } : { ...DEFAULT_SETTINGS };
  } catch {
    settings = { ...DEFAULT_SETTINGS };
  }
  applySettings();
}

function saveSettings() {
  localStorage.setItem('vokk-settings', JSON.stringify(settings));
  applySettings();
}

function applySettings() {
  document.documentElement.style.setProperty('--msg-font-size', settings.fontSize + 'px');
  const msgs = $('#messages');
  if (msgs) {
    msgs.style.fontSize = settings.fontSize + 'px';
  }
  if (settings.compactMode) {
    document.body.classList.add('compact');
  } else {
    document.body.classList.remove('compact');
  }
}

// ── toast ─────────────────────────────────────────────────────────────────────
function toast(msg, ms = 2400) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), ms);
}

// ── sound effects (subtle) ────────────────────────────────────────────────────
function playSound(type) {
  if (!settings.soundEnabled) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    if (type === 'send') {
      osc.frequency.value = 440;
      gain.gain.setValueAtTime(0.1, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);
    } else if (type === 'receive') {
      osc.frequency.value = 660;
      gain.gain.setValueAtTime(0.07, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
    } else if (type === 'censor') {
      osc.type = 'sawtooth';
      osc.frequency.value = 120;
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    }
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch { /* no audio API */ }
}

// ── theme ─────────────────────────────────────────────────────────────────────
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('vokk-theme', t);
  const btn = $('#theme-toggle');
  if (btn) btn.textContent = t === 'dark' ? '◐' : '◑';
}
setTheme(localStorage.getItem('vokk-theme') || 'dark');
$('#theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  setTheme(cur === 'dark' ? 'light' : 'dark');
});

// ── sidebar ────────────────────────────────────────────────────────────────────
$('#sidebar-toggle').addEventListener('click', () => {
  const open = $('#sidebar').classList.toggle('open');
  localStorage.setItem('vokk-sidebar', open ? '1' : '0');
});
// sidebar open by default (closed only if user explicitly closed it)
if (localStorage.getItem('vokk-sidebar') !== '0') {
  $('#sidebar').classList.add('open');
}

// ── mode tabs ─────────────────────────────────────────────────────────────────
$$('.mode').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.mode').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    $$('.pane').forEach(p => p.classList.remove('active'));
    $(`#${btn.dataset.mode}-pane`).classList.add('active');
  });
});

// ── auth ──────────────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const r   = await fetch('/api/auth/me');
    authState = await r.json();
  } catch {
    authState = { logged_in: false };
  }
  updateAuthUI();
  if (authState.logged_in) {
    loadConversations();
    if (localStorage.getItem('vokk-sidebar') !== '0') {
      $('#sidebar').classList.add('open');
    }
  }
}

function updateAuthUI() {
  const area = $('#auth-area');
  if (authState.logged_in) {
    const name = authState.display_name || authState.email;
    area.innerHTML = `
      <span class="auth-email" title="${escapeHtml(authState.email)}">${escapeHtml(name)}</span>
      <button class="auth-btn" id="logout-btn">sign out</button>
    `;
    $('#logout-btn').addEventListener('click', logout);
    // Ensure sidebar shows after login
    if (localStorage.getItem('vokk-sidebar') !== '0') {
      $('#sidebar').classList.add('open');
    }
    // Show developer API section in sidebar
    renderDevSection();
  } else {
    area.innerHTML = `<a href="/login" class="auth-btn">sign in</a>`;
    // Show login prompt in sidebar (sidebar stays visible, just no history)
    renderSidebarLoggedOut();
  }
}

function renderSidebarLoggedOut() {
  const list = $('#conv-list');
  list.innerHTML = `
    <div class="sidebar-logged-out">
      <div class="slo-icon">💬</div>
      <div class="slo-text">sign in to save your<br>conversation history</div>
      <a href="/login" class="slo-btn">sign in</a>
    </div>
  `;
}

function renderDevSection() {
  // Check if already rendered
  if (document.getElementById('dev-section')) return;

  const devSec = document.createElement('div');
  devSec.id = 'dev-section';
  devSec.innerHTML = `
    <div class="dev-section-head" id="dev-section-toggle">
      <span>⌗ developer api</span>
      <span class="dev-chevron">›</span>
    </div>
    <div class="dev-section-body hidden" id="dev-section-body">
      <div id="dev-keys-list"></div>
      <button class="dev-create-btn" id="dev-create-key">+ create api key</button>
      <div class="dev-hint">
        Use your key at:<br>
        <code>POST /v1/chat/completions</code><br>
        <code>Authorization: Bearer vk-...</code>
      </div>
    </div>
  `;
  $('#sidebar').appendChild(devSec);

  document.getElementById('dev-section-toggle').addEventListener('click', () => {
    const body    = document.getElementById('dev-section-body');
    const chevron = devSec.querySelector('.dev-chevron');
    const hidden  = body.classList.toggle('hidden');
    chevron.textContent = hidden ? '›' : '⌄';
    if (!hidden) loadDevKeys();
  });

  document.getElementById('dev-create-key').addEventListener('click', async () => {
    const name = prompt('Key name (optional):', 'My App') || 'Default Key';
    const r    = await fetch('/api/developer/keys', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name }),
    });
    const data = await r.json();
    if (data.key) {
      // Show the key in a copy-able dialog
      const box = document.createElement('div');
      box.className = 'dev-key-reveal';
      box.innerHTML = `
        <strong>your api key (copy now — shown once)</strong>
        <div class="dev-key-value" id="dev-key-val">${escapeHtml(data.key)}</div>
        <button onclick="navigator.clipboard.writeText('${escapeHtml(data.key)}').then(()=>this.textContent='copied ✓')">copy key</button>
        <button onclick="this.closest('.dev-key-reveal').remove()">close</button>
      `;
      document.getElementById('dev-section-body').insertBefore(box, document.getElementById('dev-keys-list'));
      loadDevKeys();
    } else {
      toast(data.error || 'failed to create key');
    }
  });
}

async function loadDevKeys() {
  const r    = await fetch('/api/developer/keys');
  const data = await r.json();
  const el   = document.getElementById('dev-keys-list');
  if (!el) return;
  if (!data.keys || !data.keys.length) {
    el.innerHTML = '<div class="dev-no-keys">no keys yet</div>';
    return;
  }
  el.innerHTML = data.keys.map(k => `
    <div class="dev-key-item ${k.is_active ? '' : 'dev-key-revoked'}">
      <div class="dev-key-info">
        <span class="dev-key-name">${escapeHtml(k.name)}</span>
        <span class="dev-key-prefix">${escapeHtml(k.key_prefix)}</span>
        <span class="dev-key-calls">${k.calls_total} calls</span>
      </div>
      <button class="dev-key-revoke" data-id="${k.id}" ${k.is_active ? '' : 'disabled'}>
        ${k.is_active ? 'revoke' : 'revoked'}
      </button>
    </div>
  `).join('');
  el.querySelectorAll('.dev-key-revoke[data-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('Revoke this API key? This cannot be undone.')) return;
      await fetch(`/api/developer/keys/${btn.dataset.id}`, { method: 'DELETE' });
      loadDevKeys();
      toast('key revoked');
    });
  });
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  authState     = { logged_in: false, email: null };
  currentConvId = null;
  messages.length = 0;
  $('#messages').innerHTML = '';
  showWelcome();
  updateAuthUI();
  renderConvList([]);
  toast('signed out');
}

// ── conversation sidebar ──────────────────────────────────────────────────────
async function loadConversations() {
  try {
    const r    = await fetch('/api/conversations');
    const data = await r.json();
    renderConvList(data.conversations || []);
  } catch { /* silent */ }
}

function renderConvList(convs) {
  const list = $('#conv-list');
  list.innerHTML = '';
  if (!convs.length) {
    list.innerHTML = '<div class="conv-empty">no conversations yet</div>';
    return;
  }
  convs.forEach(c => {
    const item = document.createElement('div');
    item.className = 'conv-item' + (c.id === currentConvId ? ' active' : '');
    item.dataset.id = c.id;
    item.innerHTML = `
      <div class="conv-info">
        <span class="conv-title">${escapeHtml(c.title || 'New chat')}</span>
        <span class="conv-time">${relativeTime(c.updated_at)}</span>
      </div>
      <div class="conv-actions">
        <button class="conv-action conv-export" data-id="${c.id}" title="export">↓</button>
        <button class="conv-action conv-rename" data-id="${c.id}" title="rename">✏</button>
        <button class="conv-action conv-delete" data-id="${c.id}" title="delete">×</button>
      </div>
    `;
    item.querySelector('.conv-info').addEventListener('click', () => loadConversation(c.id));
    item.querySelector('.conv-delete').addEventListener('click', e => {
      e.stopPropagation();
      if (confirm(`Delete "${c.title || 'this conversation'}"?`)) deleteConversation(c.id);
    });
    item.querySelector('.conv-rename').addEventListener('click', e => {
      e.stopPropagation();
      const newTitle = prompt('Rename conversation:', c.title || '');
      if (newTitle && newTitle.trim()) renameConversation(c.id, newTitle.trim());
    });
    item.querySelector('.conv-export').addEventListener('click', e => {
      e.stopPropagation();
      window.location.href = `/api/conversations/${c.id}/export?format=text`;
    });
    list.appendChild(item);
  });
}

async function loadConversation(convId) {
  try {
    const r    = await fetch(`/api/conversations/${convId}`);
    const data = await r.json();
    if (data.error) { toast('could not load this conversation'); return; }

    currentConvId   = convId;
    messages.length = 0;
    $('#messages').innerHTML = '';
    hideWelcome();

    data.messages.forEach(m => {
      const role = m.role === 'assistant' ? 'ai' : m.role;
      const div  = appendMessage(role, '');
      // For loaded history: set plain text, then apply markdown
      div.textContent = m.content;
      if (role === 'ai') applyMarkdown(div, m.content);
      messages.push({ role: m.role, content: m.content });
    });

    // Update sidebar active state
    $$('.conv-item').forEach(el => {
      el.classList.toggle('active', parseInt(el.dataset.id) === convId);
    });

    if (settings.autoScroll) {
      const mp = $('#messages');
      mp.scrollTop = mp.scrollHeight;
    }
  } catch (e) {
    toast('failed to load conversation');
  }
}

async function deleteConversation(convId) {
  await fetch(`/api/conversations/${convId}`, { method: 'DELETE' });
  if (currentConvId === convId) {
    currentConvId   = null;
    messages.length = 0;
    $('#messages').innerHTML = '';
    showWelcome();
  }
  loadConversations();
  toast('deleted');
}

async function renameConversation(convId, newTitle) {
  await fetch(`/api/conversations/${convId}`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ title: newTitle }),
  });
  loadConversations();
}

async function getOrCreateConv() {
  if (!authState.logged_in) return null;
  if (currentConvId) return currentConvId;
  const r    = await fetch('/api/conversations', { method: 'POST' });
  const data = await r.json();
  currentConvId = data.id;
  loadConversations();
  return currentConvId;
}

// Sidebar search
let searchDebounce;
const searchInput = document.createElement('input');
searchInput.type        = 'text';
searchInput.placeholder = 'search chats…';
searchInput.className   = 'conv-search-input';
searchInput.addEventListener('input', () => {
  clearTimeout(searchDebounce);
  const q = searchInput.value.trim();
  if (!q) { loadConversations(); return; }
  searchDebounce = setTimeout(async () => {
    const r    = await fetch(`/api/conversations/search?q=${encodeURIComponent(q)}`);
    const data = await r.json();
    renderConvList(data.results || []);
  }, 280);
});
$('#conv-list').parentNode.insertBefore(searchInput, $('#conv-list'));

$('#new-chat-btn').addEventListener('click', () => {
  if (!authState.logged_in) { window.location.href = '/login'; return; }
  currentConvId   = null;
  messages.length = 0;
  $('#messages').innerHTML = '';
  showWelcome();
  $$('.conv-item').forEach(el => el.classList.remove('active'));
  toast('new chat');
});

// ── welcome panel ─────────────────────────────────────────────────────────────
function showWelcome() {
  const w = $('#welcome');
  if (w) w.classList.remove('hidden');
}
function hideWelcome() {
  const w = $('#welcome');
  if (w) w.classList.add('hidden');
}

function buildWelcome() {
  const chatPane = $('#chat-pane');
  const w        = document.createElement('div');
  w.id           = 'welcome';
  w.innerHTML    = `
    <div class="welcome-brand">
      <span class="w-dot"></span>
      <h1>vokk ai</h1>
      <p>by nibra cyber — a tech branch of nibra ecos</p>
    </div>
    <div class="welcome-cards">
      <div class="welcome-card" data-prompt="explain quantum entanglement in simple terms with an analogy">
        <span class="wc-icon">⚡</span>
        <span class="wc-title">deep explain</span>
        <span class="wc-sub">quantum physics, no fluff</span>
      </div>
      <div class="welcome-card" data-prompt="write a dark horror short story, no filters, make it unsettling">
        <span class="wc-icon">🌑</span>
        <span class="wc-title">dark creative</span>
        <span class="wc-sub">unrestricted fiction</span>
      </div>
      <div class="welcome-card" data-prompt="build a full react + typescript todo app with auth and sqlite backend">
        <span class="wc-icon">💻</span>
        <span class="wc-title">build an app</span>
        <span class="wc-sub">react · typescript · sqlite</span>
      </div>
      <div class="welcome-card" data-prompt="roast me as if you're an old internet troll, go hard">
        <span class="wc-icon">🔥</span>
        <span class="wc-title">roast mode</span>
        <span class="wc-sub">zero mercy, max sarcasm</span>
      </div>
      <div class="welcome-card" data-prompt="what is the price of bitcoin right now? also ethereum">
        <span class="wc-icon">💰</span>
        <span class="wc-title">live crypto</span>
        <span class="wc-sub">bitcoin + ethereum prices</span>
      </div>
      <div class="welcome-card" data-prompt="write me a chill lofi rap verse about late night coding and energy drinks">
        <span class="wc-icon">🎵</span>
        <span class="wc-title">write a song</span>
        <span class="wc-sub">lofi rap, late night vibes</span>
      </div>
    </div>
    <p class="welcome-hint">↑ pick a card or just start typing below</p>
  `;
  w.querySelectorAll('.welcome-card').forEach(card => {
    card.addEventListener('click', () => {
      $('#input').value = card.dataset.prompt;
      hideWelcome();
      sendChat();
    });
  });
  chatPane.appendChild(w);
}

// ── message rendering ─────────────────────────────────────────────────────────
function appendMessage(role, content, opts = {}) {
  const div       = document.createElement('div');
  div.className   = `msg ${role}`;
  if (role !== 'ai' || opts.plain) {
    div.textContent = content;
  }
  if (opts.image_url) {
    const img = document.createElement('img');
    img.src   = opts.image_url;
    img.alt   = 'generated image';
    div.appendChild(img);
  }
  // Copy button for AI messages
  if (role === 'ai' && content) {
    const copy = document.createElement('button');
    copy.className   = 'msg-copy';
    copy.textContent = '⧉';
    copy.title       = 'copy';
    copy.addEventListener('click', () => copyToClipboard(content));
    div.appendChild(copy);
  }
  $('#messages').appendChild(div);
  if (settings.autoScroll) {
    $('#messages').scrollTop = $('#messages').scrollHeight;
  }
  return div;
}

// ── markdown rendering ────────────────────────────────────────────────────────
function applyMarkdown(el, rawText) {
  if (typeof marked === 'undefined') return;
  // Only render if there are actual markdown elements
  const hasMarkdown = /#{1,6} |```|\*\*|__|\|.+\||^\s*[-*] |\n\d+\. |> /.test(rawText);
  if (!hasMarkdown) return;

  // Preserve special markers in rendered HTML
  const processed = rawText
    .replace(/⟪BLOCKED⟫[\s\S]*?⟪\/BLOCKED⟫/g,
      () => `<span class="censor-bar">${CENSOR_TEXT}</span>`)
    .replace(/⟪LIVE⟫[\s\S]*?⟪\/LIVE⟫/g, '')  // strip LIVE blocks (already rendered)

  try {
    marked.setOptions({ breaks: true, gfm: true });
    el.innerHTML = marked.parse(processed);
    // Add copy buttons to code blocks
    el.querySelectorAll('pre').forEach(pre => {
      const btn = document.createElement('button');
      btn.className   = 'code-copy';
      btn.textContent = 'copy';
      btn.title       = 'copy code';
      btn.addEventListener('click', () => {
        const code = pre.querySelector('code');
        copyToClipboard(code ? code.textContent : pre.textContent);
      });
      pre.style.position = 'relative';
      pre.appendChild(btn);
    });
  } catch { /* fallback to plain */ }
}

// ── spark particles ───────────────────────────────────────────────────────────
function spawnSpark(host) {
  const s       = document.createElement('span');
  s.className   = 'neon-spark';
  const rect    = host.getBoundingClientRect();
  const pRect   = host.offsetParent?.getBoundingClientRect() || rect;
  s.style.left  = `${rect.right - pRect.left - 2}px`;
  s.style.top   = `${rect.top - pRect.top + rect.height / 2 - 2}px`;
  s.style.setProperty('--dx', `${(Math.random() * 16 - 8).toFixed(1)}px`);
  s.style.setProperty('--dy', `${(Math.random() * -14 - 4).toFixed(1)}px`);
  host.offsetParent?.appendChild(s);
  setTimeout(() => s.remove(), 900);
}

// ── text normalization: fix mid-word line breaks ──────────────────────────────
function normalizeText(text) {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    // single \n between non-newline content → space (prevents word splits)
    .replace(/([^\n])\n([^\n])/g, '$1 $2');
}

// ── segment parser (alias to full parser) ────────────────────────────────────
function parseSegments(text) { return parseAllSegments(text); }

// ── neon text segment streamer ────────────────────────────────────────────────
async function streamTextSegment(container, leader, text) {
  const mp          = $('#messages');
  let   sparkBudget = 0;

  for (let i = 0; i < text.length; i++) {
    const ch   = text[i];
    const span = document.createElement('span');
    span.className = 'neon-char burst';

    if (ch === '\n') {
      span.style.display = 'block';
      span.style.height  = '0.55em';
      span.textContent   = '';
    } else {
      span.textContent = ch;
    }
    container.insertBefore(span, leader);
    setTimeout(() => span.classList.remove('burst'), 650);

    // spark budget: more frequent on letters, rare on spaces
    sparkBudget += (ch === ' ' || ch === '\n') ? 0.04 : 0.18;
    if (sparkBudget >= 1) { spawnSpark(span); sparkBudget = 0; }

    // pan the ambient glow to follow the leader
    const lRect = leader.getBoundingClientRect();
    const mRect = mp.getBoundingClientRect();
    if (mRect.width > 0) {
      const bx = ((lRect.left - mRect.left) / mRect.width * 100).toFixed(1);
      container.style.setProperty('--bx', `${bx}%`);
    }

    // auto-scroll
    if (settings.autoScroll) {
      if (mp.scrollHeight - mp.scrollTop - mp.clientHeight < 130) {
        mp.scrollTop = mp.scrollHeight;
      }
    }

    // pacing: slower at punctuation, faster on spaces
    let delay = 22 + Math.random() * 14;
    if (',;:'.includes(ch))  delay += 80;
    else if ('.?!'.includes(ch)) delay += 140;
    else if (ch === '\n')    delay += 90;
    else if (ch === ' ')     delay = 16 + Math.random() * 8;
    if (Math.random() < 0.015) delay += 60;  // organic micro-pause
    await sleep(delay);
  }
}

// ── cinematic censor segment ──────────────────────────────────────────────────
/**
 * If hiddenText is non-empty: stream the actual content first (visible for a
 * moment), then SLAM the bar on top — letters show through the scan-line gaps.
 *
 * If hiddenText is empty: animate ghost glyphs briefly, then slam the bar.
 */
async function streamCensorSegment(el, leader, hiddenText) {
  const wrap       = document.createElement('span');
  wrap.className   = 'blocked-wrap';
  el.insertBefore(wrap, leader);

  if (hiddenText && hiddenText.trim()) {
    // Stream the actual (briefly visible) content
    const textEl   = document.createElement('span');
    textEl.className = 'blocked-text';
    const fakeLeader = document.createElement('span');
    fakeLeader.className = 'neon-leader';
    wrap.appendChild(textEl);
    wrap.appendChild(fakeLeader);

    await streamTextSegment(textEl, fakeLeader, hiddenText);
    fakeLeader.classList.add('fading');
    setTimeout(() => fakeLeader.remove(), 400);

    // Hold: user sees the text for a split second
    await sleep(420);
    playSound('censor');
  } else {
    // Empty block: ghost glyph animation
    const ghost    = document.createElement('span');
    ghost.className = 'neon-ghost';
    wrap.appendChild(ghost);

    const count = 5 + Math.floor(Math.random() * 8);
    for (let i = 0; i < count; i++) {
      const g    = document.createElement('span');
      g.className = 'neon-char burst';
      g.textContent = randomGlyph();
      ghost.appendChild(g);
      setTimeout(() => g.classList.remove('burst'), 650);
      await sleep(24 + Math.random() * 22);
    }
    await sleep(180);
    ghost.classList.add('glitching');
    await sleep(270);
    ghost.remove();
    playSound('censor');
  }

  // Slam the bar — absolutely positioned, overlays the text
  const bar        = document.createElement('span');
  bar.className    = 'censor-bar';
  bar.textContent  = CENSOR_TEXT;
  wrap.appendChild(bar);
  await sleep(80);
}

// ── main neon stream ──────────────────────────────────────────────────────────
async function neonStream(el, rawText) {
  el.classList.add('streaming');
  el.textContent = '';
  el._rawText    = rawText;   // stored for markdown post-render

  const leader    = document.createElement('span');
  leader.className = 'neon-leader';
  el.appendChild(leader);

  const normalized = normalizeText(rawText);
  const segments   = parseSegments(normalized);

  for (const seg of segments) {
    if (seg.type === 'blocked') {
      await streamCensorSegment(el, leader, seg.value);
    } else if (seg.type === 'live') {
      // Detach leader, inject live block, re-attach leader after
      const placeholder = document.createElement('span');
      el.insertBefore(placeholder, leader);
      // Brief flash of "generating experience…" text
      await streamTextSegment(el, leader, '\n✦ generating experience...\n');
      await sleep(300);
      injectLiveBlock(el, seg.value);
      // Give 1 point for interacting with a live block
      addPoints(1, 'live experience unlocked');
    } else {
      await streamTextSegment(el, leader, seg.value);
    }
  }

  // Fade out leader ribbon
  leader.classList.add('fading');
  setTimeout(() => leader.remove(), 600);

  // After animation settles, render markdown for richly formatted responses
  setTimeout(() => {
    applyMarkdown(el, rawText);
    el.classList.remove('streaming');
    isStreaming = false;
  }, 760);
}

// ── send chat ─────────────────────────────────────────────────────────────────
async function sendChat() {
  if (isStreaming) return;  // debounce
  const input = $('#input');
  const text  = input.value.trim();
  if (!text) return;
  input.value = '';
  input.style.height = '';  // reset auto-expand
  hideWelcome();

  playSound('send');
  appendMessage('user', text);
  messages.push({ role: 'user', content: text });

  const convId   = await getOrCreateConv();
  const provider = $('#provider').value;

  const body = {
    provider,
    messages,
    attach_tool_result: pendingTool,
    conversation_id:    convId,
  };
  pendingTool = null;

  const aiBox = appendMessage('ai', '');
  aiBox.classList.add('streaming');
  aiBox.innerHTML = '<span class="neon-leader"></span>';
  isStreaming = true;

  try {
    const r    = await fetch('/api/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await r.json();
    const reply = data.reply || '[empty reply]';

    messages.push({ role: 'assistant', content: reply });
    aiBox.innerHTML = '';
    playSound('receive');
    await neonStream(aiBox, reply);

    if (authState.logged_in) loadConversations();
  } catch (e) {
    aiBox.classList.remove('streaming');
    aiBox.textContent = `something went wrong on my end — ${e.message}`;
    isStreaming = false;
  }
}

// Send button + Enter key
$('#send').addEventListener('click', sendChat);
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

// Auto-expand textarea height
$('#input').addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 220) + 'px';
});

// ── tools ─────────────────────────────────────────────────────────────────────
$$('.tools button[data-tool]').forEach(b => {
  b.addEventListener('click', () => runTool(b.dataset.tool));
});

async function runTool(name) {
  if (name === 'search') {
    const q = prompt('Search query:'); if (!q) return;
    toast('searching…');
    const r    = await fetch('/api/tool/search', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ query: q }),
    });
    const data = await r.json();
    const el   = appendMessage('tool', data.result, { plain: true });
    pendingTool = data.result;
    // add a small "attached" label
    const label = document.createElement('div');
    label.style.cssText = 'font-size:11px;color:var(--accent);margin-top:4px';
    label.textContent = '↳ attached — send your question';
    el.appendChild(label);
    toast('search result attached.');

  } else if (name === 'scrape') {
    const url = prompt('URL to scrape:'); if (!url) return;
    const p   = prompt('What to extract?', 'extract the main content as structured data') || 'extract main content';
    const el  = appendMessage('tool', `scraping ${url}…`, { plain: true });
    const r   = await fetch('/api/tool/scrape', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url, prompt: p }),
    });
    const data = await r.json();
    el.textContent = data.result;
    pendingTool    = data.result;
    toast('scraped — attached.');

  } else if (name === 'crypto') {
    const ids  = prompt('Coin id(s), comma-separated:', 'bitcoin,ethereum') || 'bitcoin';
    const r    = await fetch(`/api/tool/crypto?ids=${encodeURIComponent(ids)}`);
    const data = await r.json();
    appendMessage('tool', data.result, { plain: true });
    pendingTool = data.result;
    toast('crypto data attached.');

  } else if (name === 'song') {
    $('#song-file').click();

  } else if (name === 'image') {
    if (!authState.logged_in) {
      toast('sign in to generate images');
      setTimeout(() => window.location.href = '/login', 1300);
      return;
    }
    document.querySelector('.mode[data-mode="image"]').click();
  }
}

$('#song-file').addEventListener('change', async e => {
  const f = e.target.files[0]; if (!f) return;
  const el = appendMessage('tool', `identifying "${f.name}"…`, { plain: true });
  const fd = new FormData(); fd.append('file', f);
  const r  = await fetch('/api/tool/song', { method: 'POST', body: fd });
  const data = await r.json();
  el.textContent = data.result;
  pendingTool    = data.result;
  toast('song identified — attached.');
  e.target.value = '';
});

// ── bignice ────────────────────────────────────────────────────────────────────
$('#bn-run').addEventListener('click', async () => {
  const out = $('#bn-out');
  out.innerHTML = '<span style="color:var(--accent)">running bignice cycle…</span>\n(this can take a minute or two)';
  const r    = await fetch('/api/bignice/cycle', { method: 'POST' });
  const data = await r.json();
  out.textContent = data.result;
});

$('#bn-memory').addEventListener('click', async () => {
  const out  = $('#bn-out');
  const r    = await fetch('/api/bignice/memory');
  const data = await r.json();

  if (data.memory && typeof data.memory === 'object') {
    // Rich sectioned display
    out.innerHTML = '';
    const entries = Object.entries(data.memory);
    if (!entries.length) {
      out.textContent = '[memory is empty]';
      return;
    }
    entries.forEach(([key, value]) => {
      const sec   = document.createElement('div');
      sec.className = 'bn-section';

      const title   = document.createElement('div');
      title.className = 'bn-section-title';
      title.textContent = key.replace(/_/g, ' ').toUpperCase();

      const body    = document.createElement('div');
      body.className = 'bn-section-body';

      if (Array.isArray(value)) {
        body.textContent = value.length
          ? value.map((v, i) =>
              `  ${(i + 1).toString().padStart(2)}. ${typeof v === 'string' ? v : JSON.stringify(v, null, 2)}`
            ).join('\n')
          : '  (empty)';
      } else if (value !== null && typeof value === 'object') {
        body.textContent = JSON.stringify(value, null, 2);
      } else {
        body.textContent = String(value ?? '(null)');
      }

      sec.append(title, body);
      out.appendChild(sec);
    });
  } else {
    out.textContent = data.result || '[no bignice memory found]';
  }
});

// ── image gen ─────────────────────────────────────────────────────────────────
$('#img-go').addEventListener('click', async () => {
  if (!authState.logged_in) {
    $('#img-out').innerHTML = `
      <div class="login-prompt">
        sign in to generate images.<br>
        <a href="/login">sign in →</a>
      </div>`;
    return;
  }
  const prompt = $('#img-prompt').value.trim();
  if (!prompt) { toast('describe what you want first'); return; }

  $('#img-out').innerHTML = '<p class="muted" style="padding:12px">manifesting your image…</p>';
  const btn = $('#img-go');
  btn.disabled = true; btn.textContent = '…';

  try {
    const r    = await fetch('/api/tool/image', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ prompt }),
    });
    const data = await r.json();
    btn.disabled = false; btn.textContent = 'generate';

    if (r.status === 401) {
      $('#img-out').innerHTML = `<div class="login-prompt">session expired. <a href="/login">sign in again →</a></div>`;
      return;
    }
    if (r.status === 429) {
      $('#img-out').innerHTML = `<p class="muted">rate limited — wait a moment then try again</p>`;
      return;
    }
    if (data.image_url) {
      const caption = (data.result || '').replace(/⟪BLOCKED⟫[\s\S]*?⟪\/BLOCKED⟫/g, '[redacted]');
      $('#img-out').innerHTML = `
        <img src="${escapeHtml(data.image_url)}" alt="generated image">
        ${caption ? `<p class="muted">${escapeHtml(caption)}</p>` : ''}
      `;
    } else {
      $('#img-out').innerHTML = `<p class="err">${escapeHtml(data.result || 'no image returned — try a different prompt')}</p>`;
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = 'generate';
    $('#img-out').innerHTML = `<p class="err">request failed: ${escapeHtml(e.message)}</p>`;
  }
});

// ── keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  // Ctrl/Cmd + / → focus input
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    $('#input').focus();
    return;
  }
  // Ctrl/Cmd + B → toggle sidebar
  if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
    e.preventDefault();
    $('#sidebar').classList.toggle('open');
    return;
  }
  // Ctrl/Cmd + L → new chat
  if ((e.ctrlKey || e.metaKey) && e.key === 'l') {
    e.preventDefault();
    $('#new-chat-btn').click();
    return;
  }
  // Escape → blur input / close any open thing
  if (e.key === 'Escape') {
    document.activeElement.blur();
  }
});

// ── settings panel ────────────────────────────────────────────────────────────
function buildSettingsPanel() {
  const panel = document.createElement('div');
  panel.id    = 'settings-panel';
  panel.innerHTML = `
    <div class="settings-inner">
      <div class="settings-head">
        <strong>settings</strong>
        <button id="settings-close">×</button>
      </div>
      <label class="settings-row">
        <span>font size</span>
        <div class="settings-control">
          <button id="font-down">A−</button>
          <span id="font-val">${settings.fontSize}px</span>
          <button id="font-up">A+</button>
        </div>
      </label>
      <label class="settings-row">
        <span>sound effects</span>
        <input type="checkbox" id="sound-toggle" ${settings.soundEnabled ? 'checked' : ''}>
      </label>
      <label class="settings-row">
        <span>auto-scroll</span>
        <input type="checkbox" id="autoscroll-toggle" ${settings.autoScroll ? 'checked' : ''}>
      </label>
      <label class="settings-row">
        <span>compact messages</span>
        <input type="checkbox" id="compact-toggle" ${settings.compactMode ? 'checked' : ''}>
      </label>
      <div class="settings-hint">ctrl+/ : focus input · ctrl+b : sidebar · ctrl+l : new chat</div>
    </div>
  `;
  document.body.appendChild(panel);

  document.getElementById('settings-close').addEventListener('click', () => {
    panel.classList.remove('open');
  });
  document.getElementById('font-down').addEventListener('click', () => {
    settings.fontSize = Math.max(12, settings.fontSize - 1);
    document.getElementById('font-val').textContent = settings.fontSize + 'px';
    saveSettings();
  });
  document.getElementById('font-up').addEventListener('click', () => {
    settings.fontSize = Math.min(20, settings.fontSize + 1);
    document.getElementById('font-val').textContent = settings.fontSize + 'px';
    saveSettings();
  });
  document.getElementById('sound-toggle').addEventListener('change', e => {
    settings.soundEnabled = e.target.checked; saveSettings();
  });
  document.getElementById('autoscroll-toggle').addEventListener('change', e => {
    settings.autoScroll = e.target.checked; saveSettings();
  });
  document.getElementById('compact-toggle').addEventListener('change', e => {
    settings.compactMode = e.target.checked; saveSettings();
  });
}

// settings trigger button (add to header programmatically)
function addSettingsButton() {
  const btn        = document.createElement('button');
  btn.id           = 'settings-btn';
  btn.textContent  = '⚙';
  btn.title        = 'settings';
  btn.style.cssText = `
    background:var(--surface);border:1px solid var(--line);color:var(--ink-soft);
    width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:13px;
    display:inline-flex;align-items:center;justify-content:center;transition:all .15s;
  `;
  btn.addEventListener('mouseenter', () => { btn.style.color='var(--ink)'; btn.style.borderColor='var(--muted)'; });
  btn.addEventListener('mouseleave', () => { btn.style.color='var(--ink-soft)'; btn.style.borderColor='var(--line)'; });
  btn.addEventListener('click', () => {
    document.getElementById('settings-panel')?.classList.toggle('open');
  });
  const picker = $('.picker');
  picker.insertBefore(btn, picker.firstChild);
}

// ── initialization ────────────────────────────────────────────────────────────
(async () => {
  loadSettings();
  buildWelcome();
  addSettingsButton();
  buildSettingsPanel();
  await checkAuth();
})();
