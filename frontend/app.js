// STREAK SPA — Firebase Anonymous + Google auth
(() => {
'use strict';

const state = {
  lang: localStorage.getItem('streak_lang') ||
        (navigator.language.startsWith('ko') ? 'ko' :
         navigator.language.startsWith('zh') ? 'zh' : 'en'),
  i18n: {},
  user: null,            // { address: firebase_uid, referral_code, tokens, locale, auth_method, email }
  page: 'home',
  config: null,
  refFromUrl: new URLSearchParams(location.search).get('ref'),
  fbApp: null, fbAuth: null, fbUser: null,
};

// ─── i18n ────────────────────────────────────────────────────────
async function loadI18n(lang) {
  const r = await fetch(`/static/i18n/${lang}.json`);
  state.i18n = await r.json();
  state.lang = lang;
  localStorage.setItem('streak_lang', lang);
  document.documentElement.lang = lang;
  applyI18n();
}
function t(key, vars = {}) {
  const path = key.split('.');
  let v = state.i18n;
  for (const p of path) v = v?.[p];
  if (typeof v !== 'string') return key;
  return v.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
}
function applyI18n() {
  document.querySelectorAll('[data-i]').forEach(el => { el.textContent = t(el.dataset.i); });
  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.classList.toggle('active', b.dataset.lang === state.lang);
  });
}

// ─── API ─────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (state.fbUser) {
    const tok = await state.fbUser.getIdToken();
    headers['Authorization'] = `Bearer ${tok}`;
  }
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    const txt = await r.text();
    throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, body: txt });
  }
  return r.json();
}

// ─── Firebase init ───────────────────────────────────────────────
function waitForFirebaseSDK() {
  return new Promise(res => {
    if (window.firebaseSDK) return res();
    window.addEventListener('firebase-sdk-ready', res, { once: true });
  });
}

async function initFirebase(cfg) {
  await waitForFirebaseSDK();
  const sdk = window.firebaseSDK;
  if (!cfg.apiKey) {
    showToast('Firebase config not set on server'); return;
  }
  state.fbApp = sdk.initializeApp(cfg);
  state.fbAuth = sdk.getAuth(state.fbApp);

  return new Promise(res => {
    sdk.onAuthStateChanged(state.fbAuth, async (fbUser) => {
      state.fbUser = fbUser;
      if (fbUser) {
        await syncBackendUser();
      } else {
        state.user = null;
      }
      render();
      res();
    });
  });
}

async function syncBackendUser() {
  // /api/auth/me 시도 → 404면 register 호출
  try {
    state.user = await api('/api/auth/me');
  } catch (e) {
    if (e.status === 404) {
      // 신규 가입 (referral 코드 포함)
      try {
        state.user = await api('/api/auth/register', {
          method: 'POST',
          body: JSON.stringify({ referral_code: state.refFromUrl }),
        });
        if (state.refFromUrl) {
          const url = new URL(location.href);
          url.searchParams.delete('ref');
          history.replaceState({}, '', url);
          state.refFromUrl = null;
        }
      } catch (ee) { console.error('register failed', ee); showToast(t('common.error')); }
    } else {
      console.error(e); showToast(t('common.error'));
    }
  }
}

// ─── Auth actions ────────────────────────────────────────────────
async function signInAnon() {
  try {
    await window.firebaseSDK.signInAnonymously(state.fbAuth);
  } catch (e) {
    console.error(e); showToast(e.message || t('common.error'));
  }
}

async function signInGoogle() {
  try {
    const provider = new window.firebaseSDK.GoogleAuthProvider();
    if (state.fbUser && state.fbUser.isAnonymous) {
      // 익명 → Google 업그레이드 (UID 유지 + +10 보너스)
      await window.firebaseSDK.linkWithPopup(state.fbUser, provider);
    } else {
      await window.firebaseSDK.signInWithPopup(state.fbAuth, provider);
    }
  } catch (e) {
    if (e.code === 'auth/credential-already-in-use') {
      // 다른 익명 계정과 이미 연결된 Google 계정 — 그냥 sign in
      try {
        await window.firebaseSDK.signInWithPopup(state.fbAuth, new window.firebaseSDK.GoogleAuthProvider());
      } catch (e2) { showToast(e2.message); }
    } else if (e.code === 'auth/popup-closed-by-user') {
      // user closed popup — silent
    } else {
      console.error(e); showToast(e.message || t('common.error'));
    }
  }
}

async function signOutUser() {
  await window.firebaseSDK.signOut(state.fbAuth);
}

// ─── UI helpers ──────────────────────────────────────────────────
function showToast(msg, ms = 1800) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.remove('show'), ms);
}
function shortAddr(a) { return (a || '').slice(0, 6) + '…' + (a || '').slice(-4); }
function copy(text, msg) { navigator.clipboard.writeText(text); showToast(msg || t('wallet.copied')); }

// ─── Pages ───────────────────────────────────────────────────────
async function pageHome() {
  const main = document.getElementById('main');
  const isAnon = state.user.auth_method === 'anonymous';
  main.innerHTML = `
    <section>
      <div class="card hero">
        <div class="label">${t('home.balance_label')}</div>
        <div class="value">${state.user.tokens.toLocaleString()}</div>
        <div class="sub">${t('home.cycles_left', { n: state.user.tokens })}</div>
      </div>
    </section>
    ${isAnon ? `
      <section class="card" style="border-left: 3px solid var(--primary);">
        <h3>🎁 ${t('home.upgrade_title')}</h3>
        <p>${t('home.upgrade_desc', { tokens: state.config.upgrade_bonus })}</p>
        <button class="btn" id="btn-upgrade">${t('home.upgrade_cta')}</button>
      </section>
    ` : ''}
    <section class="grid-2">
      <button class="btn ghost" id="btn-invite">🎁 ${t('home.invite')}</button>
      <button class="btn ghost" id="btn-deploy">⚙ ${t('home.deploy_bot')}</button>
    </section>
    <section>
      <div class="card">
        <h3>${t('deploy.title')}</h3>
        <p>${t('deploy.description')}</p>
        <pre>curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/bot/deploy.sh | bash</pre>
        <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank">${t('deploy.more_info')} →</a>
      </div>
    </section>
  `;
  if (isAnon) document.getElementById('btn-upgrade').onclick = signInGoogle;
  document.getElementById('btn-invite').onclick = () => navigate('referrals');
  document.getElementById('btn-deploy').onclick = () => navigate('settings');
}

async function pageWallet() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <h1>${t('wallet.title')}</h1>
    <section class="card hero">
      <div class="label">${t('wallet.balance')}</div>
      <div class="value">${state.user.tokens.toLocaleString()} <span style="font-size:14px; opacity:0.8;">${t('wallet.balance_unit')}</span></div>
      <div class="sub">${t('wallet.balance_hint')}</div>
    </section>
    <section class="card">
      <div class="label">${t('wallet.your_address')}</div>
      <div class="row between">
        <span class="addr-pill">${state.user.email || shortAddr(state.user.address)}</span>
        <span style="font-size:11px; color:var(--text-3);">${state.user.auth_method}</span>
      </div>
    </section>
    <h2 style="margin-top:24px;">${t('wallet.earn_title')}</h2>
    <section class="card">
      <h3 style="color:var(--text);">🎁 ${t('wallet.earn_invite')}</h3>
      <p>${t('wallet.earn_invite_desc', { tokens: state.config.ref_l1 })}</p>
      <button class="btn ghost" id="btn-go-ref">→ ${t('referrals.title')}</button>
    </section>
    <section class="card">
      <h3 style="color:var(--text);">📺 ${t('wallet.earn_ads')}</h3>
      <p style="color:var(--text-3);">${t('wallet.earn_ads_desc')}</p>
    </section>
    <section class="card">
      <h2>${t('wallet.history')}</h2>
      <div id="tx-list" class="tx-list"><div class="empty">${t('common.loading')}</div></div>
    </section>
  `;
  document.getElementById('btn-go-ref').onclick = () => navigate('referrals');
  const list = document.getElementById('tx-list');
  try {
    const rows = await api('/api/tokens/history?limit=30');
    if (!rows.length) {
      list.innerHTML = `<div class="empty">${t('wallet.no_history')}</div>`;
    } else {
      list.innerHTML = rows.map(tx => {
        const cls = tx.delta > 0 ? 'pos' : 'neg';
        const sign = tx.delta > 0 ? '+' : '';
        const ts = new Date(tx.created_at).toLocaleString();
        return `<div class="tx-item">
          <div><div>${tx.kind}</div><div class="meta">${ts}${tx.note ? ' · ' + tx.note : ''}</div></div>
          <div class="tx-amount ${cls}">${sign}${tx.delta}</div>
        </div>`;
      }).join('');
    }
  } catch (e) { list.innerHTML = `<div class="empty">${t('common.error')}</div>`; }
}

function renderTree(node, depth = 0) {
  const indent = '  '.repeat(depth);
  const me = depth === 0 ? '🟢' : '└─';
  const tag = node.auth_method === 'google' ? '🌐' : node.auth_method === 'upgraded' ? '✨' : '👤';
  let out = `${indent}${me} ${tag} ${node.short}  ·  ${node.referral_code}  ·  ${node.joined}\n`;
  for (const c of (node.children || [])) out += renderTree(c, depth + 1);
  return out;
}

async function pageReferrals() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <h1>${t('referrals.title')}</h1>
    <section class="card">
      <div class="label">${t('referrals.your_code')}</div>
      <div class="row between" style="margin-top:6px;">
        <div class="value sm">${state.user.referral_code}</div>
        <button class="btn sm ghost" id="btn-copy-code">${t('wallet.copy')}</button>
      </div>
    </section>
    <section class="card">
      <div class="label">${t('referrals.your_link')}</div>
      <code id="invite-link" style="display:block;margin:6px 0;font-size:11px;word-break:break-all;"></code>
      <button class="btn" id="btn-copy-link">${t('referrals.copy_link')}</button>
    </section>
    <section class="card">
      <h3 style="color:var(--text);">${t('referrals.earn_l1', { tokens: state.config.ref_l1 })}</h3>
      <h3 style="color:var(--text);">${t('referrals.earn_l2', { tokens: state.config.ref_l2 })}</h3>
    </section>
    <section class="grid-2">
      <div class="card"><div class="label">${t('referrals.stats_direct')}</div><div class="value sm" id="stat-direct">—</div></div>
      <div class="card"><div class="label">${t('referrals.stats_indirect')}</div><div class="value sm" id="stat-indirect">—</div></div>
    </section>
    <section class="card">
      <div class="label">${t('referrals.stats_earned')}</div>
      <div class="value sm" id="stat-earned">—</div>
    </section>
    <section class="card">
      <h2>${t('referrals.tree_title')}</h2>
      <div id="tree-area"></div>
    </section>
  `;
  const stats = await api('/api/referrals/stats');
  document.getElementById('invite-link').textContent = stats.invite_url;
  document.getElementById('stat-direct').textContent = stats.direct_count;
  document.getElementById('stat-indirect').textContent = stats.indirect_count;
  document.getElementById('stat-earned').textContent = stats.tokens_earned + ' ' + t('common.tokens');
  document.getElementById('btn-copy-code').onclick = () => copy(state.user.referral_code);
  document.getElementById('btn-copy-link').onclick = () => copy(stats.invite_url);
  const tree = await api('/api/referrals/tree');
  const area = document.getElementById('tree-area');
  area.innerHTML = (tree.children?.length)
    ? `<div class="tree">${renderTree(tree)}</div>`
    : `<div class="empty">${t('referrals.no_referrals')}</div>`;
}

async function pageTrading() {
  const main = document.getElementById('main');
  main.innerHTML = `<div class="card"><div class="empty">${t('common.loading')}</div></div>`;

  let cfg, stats, openPos, history, assets;
  try {
    [cfg, stats, openPos, history, assets] = await Promise.all([
      api('/api/trading/config'),
      api('/api/trading/stats'),
      api('/api/trading/positions'),
      api('/api/trading/history?limit=20'),
      api('/api/trading/assets'),
    ]);
  } catch (e) { console.error(e); showToast(t('common.error')); return; }

  const selectedStrategy = cfg.entry_mode || cfg.strategy || 'low_target';
  const ms = stats.market_state || {};
  const closeTs = ms.end_ts ? new Date(ms.end_ts * 1000) : null;
  const elapsedPct = (ms.elapsed_pct || 0) * 100;
  const sideBadge = (s) => s === 'YES'
    ? `<span style="background:rgba(34,197,94,.15); color:#22c55e; padding:2px 8px; border-radius:8px; font-size:11px; font-weight:600;">YES</span>`
    : `<span style="background:rgba(239,68,68,.15); color:#ef4444; padding:2px 8px; border-radius:8px; font-size:11px; font-weight:600;">NO</span>`;
  const pnlPill = (v) => {
    const cls = v >= 0 ? 'pos' : 'neg';
    const sign = v >= 0 ? '+' : '';
    const color = v >= 0 ? '#22c55e' : '#ef4444';
    return `<span style="color:${color}; font-weight:700;">${sign}$${Math.abs(v).toFixed(2)}</span>`;
  };

  main.innerHTML = `
    <h1>${t('trading.title')}</h1>

    <section class="card hero">
      <div class="row between">
        <div>
          <div class="label">${t('trading.bot_status')}</div>
          <div class="value sm" style="margin-top:4px;">
            ${cfg.active
              ? `<span style="color:#22c55e;">● ${t('trading.running')}</span>`
              : `<span style="color:var(--text-3);">● ${t('trading.stopped')}</span>`}
          </div>
          ${ms.slug ? `
            <div class="sub" style="margin-top:8px; font-size:12px;">
              ${t('trading.current_market')}: <strong>${ms.question || ms.slug}</strong>
              ${closeTs ? ` · <span id="td-cd">${t('trading.next_cycle')}: <span id="td-cd-num">…</span></span>` : ''}
            </div>
            <div style="margin-top:8px; height:6px; background:var(--bg-2); border-radius:3px; overflow:hidden;">
              <div style="height:100%; width:${elapsedPct.toFixed(0)}%; background:var(--primary); transition:width 1s;"></div>
            </div>
            <div class="sub" style="margin-top:4px; font-size:11px; color:var(--text-3);">
              ${elapsedPct.toFixed(0)}% ${t('trading.elapsed')} · ${(100-elapsedPct).toFixed(0)}% ${t('trading.remaining')}
            </div>
            ` : ''}
        </div>
        <div>
          ${cfg.active
            ? `<button class="btn sm danger" id="btn-trade-stop">${t('trading.stop_btn')}</button>`
            : `<button class="btn sm" id="btn-trade-start">${t('trading.start_btn')} →</button>`}
        </div>
      </div>
      <p style="margin:12px 0 0; font-size:12px; color:var(--text-2);">${t('trading.auto_desc')}</p>
    </section>

    <section class="card" style="margin-top:12px;">
      <div class="label">${t('trading.asset_selector')}</div>
      <div class="row" style="gap:8px; margin-top:8px; flex-wrap:wrap;">
        ${assets.map(a => {
          const allDurs = [...a.active_durations, ...a.coming_soon_durations].sort((x,y)=>x-y);
          return allDurs.map(d => {
            const enabled = a.active_durations.includes(d);
            const sel = (cfg.asset === a.code && cfg.duration_min === d);
            return `<button class="btn sm ${sel?'':'ghost'}" data-asset="${a.code}" data-dur="${d}"
                            ${enabled?'':'disabled style="opacity:.4; cursor:not-allowed;"'}>
              ${a.icon} ${a.code} ${d}m${enabled?'':' · '+t('trading.coming_soon')}
            </button>`;
          }).join('');
        }).join('')}
      </div>
    </section>

    <section class="grid-2" style="margin-top:12px;">
      <div class="card"><div class="label">${t('trading.stats_total')}</div><div class="value sm">${stats.total_trades}</div></div>
      <div class="card"><div class="label">${t('trading.stats_winrate')}</div><div class="value sm">${(stats.win_rate*100).toFixed(0)}%</div></div>
      <div class="card"><div class="label">${t('trading.stats_pnl')}</div><div class="value sm">${pnlPill(stats.total_pnl)}</div></div>
      <div class="card"><div class="label">${t('trading.stats_consumed')}</div><div class="value sm">${cfg.cycles_consumed}</div></div>
    </section>

    ${(state.user.tokens < (state.config.cost_per_cycle || 1)) ? `
      <section class="card" style="border-left:3px solid var(--neg, #ef4444); margin-top:12px;">
        <p style="margin:0;">⚠ ${t('trading.need_tokens')}</p>
        <button class="btn ghost sm" id="btn-need-go" style="margin-top:8px;">→ ${t('referrals.title')}</button>
      </section>
    ` : ''}

    <section style="margin-top:24px;">
      <h2>${t('trading.strategy_presets')}</h2>
      <div class="row" style="gap:8px; margin-bottom:8px; flex-wrap:wrap;">
        <button class="btn sm ${selectedStrategy==='low_target'?'':'ghost'}" data-preset="low_target">🎯 ${t('trading.preset_low')}</button>
        <button class="btn sm ${selectedStrategy==='high_lead'?'':'ghost'}" data-preset="high_lead">📈 ${t('trading.preset_lead')}</button>
      </div>
      <details ${cfg.active?'':'open'}>
        <summary style="cursor:pointer; font-weight:600; padding:8px 0;">⚙ ${t('trading.advanced')}</summary>
        <p style="font-size:12px; color:var(--text-3); margin:4px 0 12px;">${t('trading.advanced_hint')}</p>
        <div class="card">
          <div class="grid-2" style="gap:10px;">
            <label class="form-row"><span>${t('trading.p_bet_size')}</span>
              <input type="number" id="f-bet" min="0.5" max="1000" step="0.5" value="${cfg.bet_size_usd}" /></label>
            <label class="form-row"><span>${t('trading.p_max_cycles')}</span>
              <input type="number" id="f-max" min="0" max="10000" step="1" value="${cfg.max_cycles_per_session}" /></label>
            <label class="form-row"><span>${t('trading.p_entry_price')}</span>
              <input type="number" id="f-entry" min="0.01" max="0.99" step="0.01" value="${cfg.entry_price}" /></label>
            <label class="form-row"><span>${t('trading.p_entry_tol')}</span>
              <input type="number" id="f-tol" min="0" max="0.5" step="0.01" value="${cfg.entry_tolerance}" /></label>
            <label class="form-row"><span>${t('trading.p_max_entry')}</span>
              <input type="number" id="f-maxe" min="0.5" max="0.99" step="0.01" value="${cfg.max_entry_price}" /></label>
            <label class="form-row"><span>${t('trading.p_tp')}</span>
              <input type="number" id="f-tp" min="0.02" max="0.99" step="0.01" value="${cfg.tp_price}" /></label>
            <label class="form-row"><span>${t('trading.p_sl')}</span>
              <input type="number" id="f-sl" min="0.01" max="0.5" step="0.01" value="${cfg.sl_price}" /></label>
            <label class="form-row"><span>${t('trading.p_tradeable')}</span>
              <input type="number" id="f-trd" min="0.05" max="1" step="0.05" value="${cfg.tradeable_pct}" /></label>
            <label class="form-row"><span>${t('trading.p_remaining')}</span>
              <input type="number" id="f-rem" min="0.05" max="1" step="0.05" value="${cfg.buy_when_remaining_below_pct}" /></label>
            <label class="form-row"><span>${t('trading.p_buy_type')}</span>
              <select id="f-buyt">
                <option value="limit" ${cfg.buy_order_type==='limit'?'selected':''}>limit</option>
                <option value="market" ${cfg.buy_order_type==='market'?'selected':''}>market</option>
              </select></label>
            <label class="form-row"><span>${t('trading.p_sell_type')}</span>
              <select id="f-sellt">
                <option value="limit" ${cfg.sell_order_type==='limit'?'selected':''}>limit</option>
                <option value="market" ${cfg.sell_order_type==='market'?'selected':''}>market</option>
              </select></label>
          </div>
          <button class="btn" id="btn-strat-save" style="width:100%; margin-top:12px;">${t('trading.p_save')}</button>
          <p style="font-size:11px; color:var(--text-3); margin:8px 0 0;">${t('trading.token_used_msg')}</p>
        </div>
      </details>
    </section>

    <section style="margin-top:24px;">
      <h2>${t('trading.open_positions')}</h2>
      ${openPos.length === 0
        ? `<div class="card"><div class="empty">${t('trading.no_open')}</div></div>`
        : openPos.map(p => `
          <div class="card">
            <div class="row between">
              <div>
                <div style="font-size:13px; color:var(--text-2);">${p.market_label}</div>
                <div style="margin-top:4px;">${sideBadge(p.side)} <span style="font-size:12px; color:var(--text-3);">@ $${p.entry_price.toFixed(2)} · ${p.size.toFixed(0)} sh</span></div>
              </div>
              <div style="font-size:11px; color:var(--text-3); text-align:right;">${p.strategy}</div>
            </div>
          </div>`).join('')}
    </section>

    <section style="margin-top:24px;">
      <h2>${t('trading.history_title')}</h2>
      ${history.length === 0
        ? `<div class="card"><div class="empty">${t('trading.no_history')}</div></div>`
        : `<div class="tx-list">${history.map(p => `
            <div class="tx-item">
              <div>
                <div>${sideBadge(p.side)} <span style="font-size:12px;">${p.market_label}</span></div>
                <div class="meta">${new Date(p.closed_at || p.opened_at).toLocaleString()} · ${p.exit_reason === 'win' ? '✓ '+t('trading.win') : '✗ '+t('trading.loss')}</div>
              </div>
              <div class="tx-amount ${p.pnl>=0?'pos':'neg'}">${p.pnl>=0?'+':''}$${p.pnl.toFixed(2)}</div>
            </div>`).join('')}</div>`}
    </section>

    <section style="margin-top:24px;">
      <details>
        <summary style="cursor:pointer; color:var(--text-3); font-size:13px;">${t('trading.advanced_self_host')}</summary>
        <div class="card" style="margin-top:8px;">
          <p style="font-size:13px; color:var(--text-2);">${t('trading.setup_desc')}</p>
          <pre style="background:var(--bg-2); padding:10px; border-radius:8px; font-size:11px; overflow-x:auto; word-break:break-all; white-space:pre-wrap;">curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/bot/deploy.sh | bash</pre>
          <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank" style="font-size:13px;">${t('deploy.more_info')} →</a>
        </div>
      </details>
    </section>
  `;

  // ─── handlers ────────────────────────────────────────
  const startBtn = document.getElementById('btn-trade-start');
  const stopBtn = document.getElementById('btn-trade-stop');
  if (startBtn) startBtn.onclick = async () => {
    try {
      await api('/api/trading/start', { method: 'POST' });
      showToast('✓ ' + t('trading.running'));
      pageTrading();
    } catch (e) {
      if (e.status === 402) showToast(t('trading.need_tokens'));
      else showToast(e.body || e.message || t('common.error'));
    }
  };

  // 자산 + duration 변경
  main.querySelectorAll('[data-asset]').forEach(b => {
    if (b.disabled) return;
    b.onclick = async () => {
      try {
        await api('/api/trading/config', {
          method: 'PUT',
          body: JSON.stringify({ asset: b.dataset.asset, duration_min: parseInt(b.dataset.dur) }),
        });
        pageTrading();
      } catch (e) { showToast(e.body || e.message); }
    };
  });

  // 프리셋 (entry_mode 빠른 변경)
  main.querySelectorAll('[data-preset]').forEach(b => {
    b.onclick = async () => {
      const preset = b.dataset.preset;
      const presets = {
        low_target: { entry_mode: 'low_target', entry_price: 0.10, tp_price: 0.15, sl_price: 0.05 },
        high_lead:  { entry_mode: 'high_lead',  entry_price: 0.70, tp_price: 0.95, sl_price: 0.50, max_entry_price: 0.85 },
      };
      try {
        await api('/api/trading/config', {
          method: 'PUT',
          body: JSON.stringify(presets[preset]),
        });
        pageTrading();
      } catch (e) { showToast(e.body || e.message); }
    };
  });

  // 고급 전략 저장
  const stratSave = document.getElementById('btn-strat-save');
  if (stratSave) stratSave.onclick = async () => {
    const body = {
      bet_size_usd: parseFloat(document.getElementById('f-bet').value),
      max_cycles_per_session: parseInt(document.getElementById('f-max').value),
      entry_price: parseFloat(document.getElementById('f-entry').value),
      entry_tolerance: parseFloat(document.getElementById('f-tol').value),
      max_entry_price: parseFloat(document.getElementById('f-maxe').value),
      tp_price: parseFloat(document.getElementById('f-tp').value),
      sl_price: parseFloat(document.getElementById('f-sl').value),
      tradeable_pct: parseFloat(document.getElementById('f-trd').value),
      buy_when_remaining_below_pct: parseFloat(document.getElementById('f-rem').value),
      buy_order_type: document.getElementById('f-buyt').value,
      sell_order_type: document.getElementById('f-sellt').value,
    };
    try {
      await api('/api/trading/config', { method: 'PUT', body: JSON.stringify(body) });
      showToast('✓ ' + t('trading.p_saved'));
      pageTrading();
    } catch (e) { showToast(e.body || e.message); }
  };
  if (stopBtn) stopBtn.onclick = async () => {
    try {
      await api('/api/trading/stop', { method: 'POST' });
      showToast(t('trading.stopped'));
      pageTrading();
    } catch (e) { showToast(e.message); }
  };
  const needGo = document.getElementById('btn-need-go');
  if (needGo) needGo.onclick = () => navigate('referrals');

  // countdown
  if (closeTs) {
    const cd = document.getElementById('td-cd-num');
    const tick = () => {
      if (!document.body.contains(cd)) return;
      const left = Math.max(0, Math.floor((closeTs - new Date()) / 1000));
      const m = Math.floor(left / 60).toString().padStart(2,'0');
      const s = (left % 60).toString().padStart(2,'0');
      cd.textContent = `${m}:${s}`;
      if (left > 0) setTimeout(tick, 1000);
      else setTimeout(() => { if (state.page === 'trading') pageTrading(); }, 5000);
    };
    tick();
  }
}

function openWalletModal() {
  const overlay = document.createElement('div');
  overlay.style.cssText = `position:fixed; inset:0; background:rgba(0,0,0,.6);
    display:flex; align-items:center; justify-content:center; z-index:1000;
    padding:16px; backdrop-filter:blur(6px); overflow:auto;`;
  overlay.innerHTML = `
    <div style="background:var(--bg); border-radius:20px; padding:24px;
                max-width:420px; width:100%; box-shadow:var(--shadow-lg); max-height:90vh; overflow:auto;">
      <h2 style="margin:0 0 8px;">${t('trading.wallet_section')}</h2>
      <div style="background:rgba(239,68,68,.1); border:1px solid rgba(239,68,68,.3); border-radius:12px; padding:14px; margin-bottom:16px;">
        <div style="font-weight:700; color:#ef4444; margin-bottom:8px;">${t('trading.wallet_warn_title')}</div>
        <ul style="margin:0; padding-left:18px; font-size:12px; line-height:1.6; color:var(--text-2);">
          <li>${t('trading.wallet_warn_1')}</li>
          <li>${t('trading.wallet_warn_2')}</li>
          <li>${t('trading.wallet_warn_3')}</li>
          <li>${t('trading.wallet_warn_4')}</li>
        </ul>
      </div>
      <div style="margin-bottom:12px;">
        <label style="font-size:12px; color:var(--text-3);">${t('trading.wallet_pk_label')}</label>
        <input id="wm-pk" type="password" placeholder="${t('trading.wallet_pk_ph')}"
               style="width:100%; margin-top:4px; padding:10px 12px; border-radius:10px;
                      border:1px solid var(--border); background:var(--bg-2); color:var(--text);
                      font-family:monospace; font-size:12px;" />
      </div>
      <div style="margin-bottom:12px;">
        <label style="font-size:12px; color:var(--text-3);">${t('trading.wallet_funder_label')}</label>
        <input id="wm-funder" type="text" placeholder="0x..."
               style="width:100%; margin-top:4px; padding:10px 12px; border-radius:10px;
                      border:1px solid var(--border); background:var(--bg-2); color:var(--text);
                      font-family:monospace; font-size:12px;" />
      </div>
      <div style="margin-bottom:16px;">
        <label style="font-size:12px; color:var(--text-3);">${t('trading.wallet_max_trade')}</label>
        <input id="wm-max" type="number" min="1" max="1000" value="10" step="1"
               style="width:100%; margin-top:4px; padding:10px 12px; border-radius:10px;
                      border:1px solid var(--border); background:var(--bg-2); color:var(--text);" />
      </div>
      <div class="row" style="gap:8px;">
        <button class="btn" id="wm-save" style="flex:1;">${t('trading.wallet_save')}</button>
        <button class="btn ghost" id="wm-cancel">${t('common.cancel')}</button>
      </div>
      <button class="btn danger sm" id="wm-remove" style="margin-top:12px; width:100%;">
        ${t('trading.wallet_remove')}
      </button>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  overlay.querySelector('#wm-cancel').onclick = close;
  overlay.querySelector('#wm-save').onclick = async () => {
    const pk = overlay.querySelector('#wm-pk').value.trim();
    const funder = overlay.querySelector('#wm-funder').value.trim() || null;
    const max = parseFloat(overlay.querySelector('#wm-max').value) || 10;
    if (pk.replace(/^0x/, '').length !== 64) { showToast(t('trading.wallet_pk_label')); return; }
    try {
      await api('/api/wallet/set', {
        method: 'POST',
        body: JSON.stringify({ private_key: pk, funder_address: funder, max_trade_usd: max }),
      });
      showToast('✓ ' + t('trading.wallet_saved'));
      close();
      if (state.page === 'trading') pageTrading();
    } catch (e) { showToast(e.body || e.message); }
  };
  overlay.querySelector('#wm-remove').onclick = async () => {
    try {
      await api('/api/wallet/remove', { method: 'DELETE' });
      showToast(t('trading.wallet_removed'));
      close();
      if (state.page === 'trading') pageTrading();
    } catch (e) { showToast(e.message); }
  };
}

function pageTradingOLD() {
  const main = document.getElementById('main');
  const savedBotUrl = localStorage.getItem('streak_bot_url') || '';
  const savedPreset = localStorage.getItem('streak_preset') || '';
  const deployCmd = 'curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/bot/deploy.sh | bash';
  main.innerHTML = `
    <h1>${t('trading.title')}</h1>

    <section class="card">
      <div class="row between">
        <div>
          <div class="label">${t('trading.bot_status')}</div>
          <div class="value sm" id="bot-status-text" style="margin-top:4px;">
            <span style="color:var(--text-3);">● ${t('trading.not_connected')}</span>
          </div>
        </div>
        <button class="btn sm ghost" id="btn-check-bot">↻</button>
      </div>
    </section>

    <section class="card">
      <h3>${t('trading.connect_existing')}</h3>
      <input id="bot-url-input" class="input" type="url"
             placeholder="${t('trading.bot_url_placeholder')}"
             value="${savedBotUrl}"
             style="width:100%; margin:8px 0; padding:10px 12px; border-radius:10px; border:1px solid var(--border); background:var(--bg-2); color:var(--text);" />
      <button class="btn" id="btn-open-dash">${t('trading.open_dashboard')} →</button>
    </section>

    <section class="card" style="border-left:3px solid var(--primary);">
      <h3>⚙ ${t('trading.setup_title')}</h3>
      <p style="color:var(--text-2); font-size:13px;">${t('trading.setup_desc')}</p>
      <ol style="padding-left:18px; font-size:13px; color:var(--text-2); line-height:1.7;">
        <li>${t('trading.setup_step1')}</li>
        <li>${t('trading.setup_step2')}</li>
      </ol>
      <pre style="background:var(--bg-2); padding:12px; border-radius:8px; font-size:11px; overflow-x:auto; word-break:break-all; white-space:pre-wrap;">${deployCmd}</pre>
      <button class="btn sm ghost" id="btn-copy-cmd">📋 ${t('wallet.copy')}</button>
      <p style="color:var(--text-2); font-size:13px; margin-top:12px;">${t('trading.setup_step3')}</p>
      <a href="https://github.com/yujoohwan6342-stack/POLY" target="_blank" style="font-size:13px;">${t('deploy.more_info')} →</a>
    </section>

    <section>
      <h2 style="margin-top:24px;">${t('trading.strategy_presets')}</h2>
      <div class="card ${savedPreset==='low'?'preset-active':''}" data-preset="low">
        <h3>🎯 ${t('trading.preset_low')}</h3>
        <p style="font-size:13px; color:var(--text-2);">${t('trading.preset_low_desc')}</p>
        <button class="btn sm ${savedPreset==='low'?'':'ghost'}" data-save-preset="low">
          ${savedPreset==='low' ? '✓ ' : ''}${t('trading.save_preset')}
        </button>
      </div>
      <div class="card ${savedPreset==='lead'?'preset-active':''}" data-preset="lead" style="margin-top:12px;">
        <h3>📈 ${t('trading.preset_lead')}</h3>
        <p style="font-size:13px; color:var(--text-2);">${t('trading.preset_lead_desc')}</p>
        <button class="btn sm ${savedPreset==='lead'?'':'ghost'}" data-save-preset="lead">
          ${savedPreset==='lead' ? '✓ ' : ''}${t('trading.save_preset')}
        </button>
      </div>
    </section>
  `;

  document.getElementById('btn-copy-cmd').onclick = () => copy(deployCmd, 'Copied!');
  document.getElementById('btn-open-dash').onclick = () => {
    const url = document.getElementById('bot-url-input').value.trim();
    if (!url) { showToast(t('trading.bot_url_placeholder')); return; }
    localStorage.setItem('streak_bot_url', url);
    window.open(url, '_blank');
  };
  document.getElementById('btn-check-bot').onclick = async () => {
    const url = (document.getElementById('bot-url-input').value || savedBotUrl).trim();
    const el = document.getElementById('bot-status-text');
    if (!url) { el.innerHTML = `<span style="color:var(--text-3);">● ${t('trading.not_connected')}</span>`; return; }
    el.innerHTML = `<span style="color:var(--text-3);">… ${t('auth.connecting')}</span>`;
    try {
      await fetch(url.replace(/\/$/, '') + '/health', { mode: 'no-cors' });
      el.innerHTML = `<span style="color:var(--pos, #22c55e);">● ${t('trading.connected')}</span>`;
    } catch {
      el.innerHTML = `<span style="color:var(--neg, #ef4444);">● ${t('trading.not_connected')}</span>`;
    }
  };
  main.querySelectorAll('[data-save-preset]').forEach(b => {
    b.onclick = () => {
      localStorage.setItem('streak_preset', b.dataset.savePreset);
      showToast('✓ ' + t('trading.save_preset'));
      pageTrading();
    };
  });
}

function pageSettings() {
  const main = document.getElementById('main');
  const isAnon = state.user.auth_method === 'anonymous';
  main.innerHTML = `
    <h1>${t('settings.title')}</h1>
    ${isAnon ? `
      <section class="card" style="border-left: 3px solid var(--primary);">
        <h3>🎁 ${t('home.upgrade_title')}</h3>
        <p>${t('home.upgrade_desc', { tokens: state.config.upgrade_bonus })}</p>
        <button class="btn" id="btn-upgrade-set">${t('home.upgrade_cta')}</button>
      </section>
    ` : ''}
    <section class="card">
      <div class="label">${t('settings.language')}</div>
      <div class="row" style="margin-top:8px; gap:8px;">
        ${['en','ko','zh'].map(l => `<button class="btn sm ${state.lang===l?'':'ghost'}" data-lang="${l}">${l.toUpperCase()}</button>`).join('')}
      </div>
    </section>
    <section class="card">
      <h3 style="color:var(--neg);">${t('settings.danger_zone')}</h3>
      <button class="btn danger" id="btn-logout">${t('settings.disconnect')}</button>
    </section>
  `;
  if (isAnon) document.getElementById('btn-upgrade-set').onclick = signInGoogle;
  main.querySelectorAll('[data-lang]').forEach(b => {
    b.onclick = async () => { await loadI18n(b.dataset.lang); pageSettings(); };
  });
  document.getElementById('btn-logout').onclick = () => signOutUser();
}

// ─── Routing ─────────────────────────────────────────────────────
function navigate(page) {
  state.page = page;
  document.querySelectorAll('#bottom-nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === page);
  });
  render();
}

function render() {
  if (!state.fbUser || !state.user) {
    document.getElementById('bottom-nav').style.display = 'none';
    renderLanding();
    return;
  }
  document.getElementById('bottom-nav').style.display = 'flex';
  applyI18n();
  switch (state.page) {
    case 'home': pageHome(); break;
    case 'trading': pageTrading(); break;
    case 'wallet': pageWallet(); break;
    case 'referrals': pageReferrals(); break;
    case 'settings': pageSettings(); break;
  }
}

// ─── Counter ─────────────────────────────────────────────────────
let _counterTimer = null, _counterTarget = 0, _counterDisplay = 0;
function startCounter() {
  if (_counterTimer) clearInterval(_counterTimer);
  async function fetchTotal() {
    try {
      const r = await fetch('/api/stats/public').then(x => x.json());
      _counterTarget = r.total_users || 0;
    } catch {}
  }
  fetchTotal();
  setInterval(fetchTotal, 10000);
  _counterTimer = setInterval(() => {
    const el = document.getElementById('counter-num');
    if (!el) { clearInterval(_counterTimer); return; }
    if (_counterDisplay < _counterTarget) {
      const step = Math.max(1, Math.ceil((_counterTarget - _counterDisplay) / 30));
      _counterDisplay = Math.min(_counterTarget, _counterDisplay + step);
      el.textContent = _counterDisplay.toLocaleString();
      el.classList.add('bump');
      setTimeout(() => el.classList.remove('bump'), 400);
    }
  }, 60);
}

// ─── Landing (로그인 X) ──────────────────────────────────────────
function renderLanding() {
  const main = document.getElementById('main');
  main.innerHTML = `
    <section style="padding: 32px 0 16px; text-align:center;">
      <div style="font-size: 32px; font-weight: 800; letter-spacing: 0.18em;">STREAK</div>
      <div style="color: var(--text-3); margin-top: 4px; font-size: 13px;">${t('tagline')}</div>
    </section>
    <section class="counter">
      <div class="label"><span class="live-dot"></span>${t('home_counter.label')}</div>
      <div class="number" id="counter-num">0</div>
      <div class="subtitle">${t('home_counter.subtitle')}</div>
    </section>
    <section style="padding: 0 4px;">
      <h2 style="font-size:22px; line-height:1.3; letter-spacing:-0.02em;">${t('landing.hero_title')}</h2>
      <p style="font-size:14px; color:var(--text-2); margin-bottom:16px;">${t('landing.hero_sub')}</p>
      <button class="btn" id="btn-start">${t('landing.cta_start')} →</button>
    </section>
    <section style="margin-top:32px;">
      <div class="grid-2" style="grid-template-columns:1fr; gap:12px;">
        <div class="card"><h3>⚡ ${t('landing.feature_1_title')}</h3><p style="margin:4px 0 0;font-size:13px;">${t('landing.feature_1_desc')}</p></div>
        <div class="card"><h3>📊 ${t('landing.feature_2_title')}</h3><p style="margin:4px 0 0;font-size:13px;">${t('landing.feature_2_desc')}</p></div>
        <div class="card"><h3>🎁 ${t('landing.feature_3_title')}</h3><p style="margin:4px 0 0;font-size:13px;">${t('landing.feature_3_desc')}</p></div>
      </div>
    </section>
  `;
  document.getElementById('btn-start').onclick = openSignInModal;
  startCounter();
}

function openSignInModal() {
  const overlay = document.createElement('div');
  overlay.style.cssText = `position:fixed; inset:0; background:rgba(0,0,0,0.5);
    display:flex; align-items:center; justify-content:center; z-index:1000;
    padding:24px; backdrop-filter:blur(4px);`;
  overlay.innerHTML = `
    <div style="background:var(--bg); border-radius:20px; padding:28px;
                max-width:360px; width:100%; box-shadow:var(--shadow-lg);">
      <div style="text-align:center; font-size:40px; margin-bottom:8px;">🚀</div>
      <h2 style="margin:0 0 6px; text-align:center;">${t('auth.choose_method')}</h2>
      <p style="text-align:center; color:var(--text-3); margin:0 0 20px; font-size:13px;">
        ${t('auth.choose_desc')}
      </p>
      <div class="col" style="gap:8px;">
        <button class="btn" id="m-google" style="background:#fff; color:#000; border:1px solid var(--border);">
          <span style="font-size:18px;">G</span> ${t('auth.google')} (+${state.config.signup_bonus_google})
        </button>
        <button class="btn ghost" id="m-anon">
          👤 ${t('auth.anonymous')} (+${state.config.signup_bonus_anon})
        </button>
        <button class="btn ghost" id="m-cancel" style="margin-top:6px;">${t('auth.later')}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  overlay.querySelector('#m-cancel').onclick = close;
  overlay.querySelector('#m-anon').onclick = async () => { close(); await signInAnon(); };
  overlay.querySelector('#m-google').onclick = async () => { close(); await signInGoogle(); };
}

// ─── Init ────────────────────────────────────────────────────────
async function init() {
  await loadI18n(state.lang);
  state.config = await fetch('/api/config').then(r => r.json());

  document.querySelectorAll('#lang-switch button').forEach(b => {
    b.onclick = () => loadI18n(b.dataset.lang).then(render);
  });
  document.querySelectorAll('#bottom-nav a').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); navigate(a.dataset.page); };
  });

  render();  // 첫 화면 (랜딩)

  if (state.config.firebase && state.config.firebase.apiKey) {
    await initFirebase(state.config.firebase);
  } else {
    showToast('Firebase config missing — server admin must set FIREBASE_API_KEY env');
  }
}

init().catch(e => { console.error(e); showToast(t('common.error')); });
})();
