'use strict';

// ── State ──────────────────────────────────────────────────────
let gs = null;   // last server state
let selectedTrump = null;

const SUIT_COLOR = { s: 'black', h: 'red', d: 'red', c: 'black' };
const SUIT_ORDER = ['s', 'h', 'd', 'c'];

// ── DOM refs ───────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const screens = {
  setup: $('screen-setup'),
  game:  $('screen-game'),
};

function showScreen(name) {
  Object.values(screens).forEach(s => s.classList.remove('active'));
  screens[name].classList.add('active');
}

// ── API ────────────────────────────────────────────────────────
async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) {
    showError(data.error || 'Server error');
    return null;
  }
  return data;
}

function showError(msg) {
  const el = $('trump-error');
  if (el) {
    el.textContent = msg;
    el.classList.remove('hidden');
    setTimeout(() => el.classList.add('hidden'), 4000);
  } else {
    alert(msg);
  }
}

// ── Start game ─────────────────────────────────────────────────
$('btn-start').addEventListener('click', async () => {
  const name = $('player-name').value.trim() || 'You';
  const data = await api('/api/new_game', { player_name: name });
  if (data) { gs = data; render(data); showScreen('game'); }
});
$('player-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('btn-start').click();
});

$('btn-new-game-top').addEventListener('click', () => {
  if (confirm('Start a new game? Your scores will be reset.')) {
    showScreen('setup');
    gs = null;
    selectedTrump = null;
  }
});

// ── Bid buttons ────────────────────────────────────────────────
$('btn-bid-yes').addEventListener('click', async () => {
  const data = await api('/api/action', { action: 'bid', bid_yes: true });
  if (data) { gs = data; render(data); }
});
$('btn-bid-no').addEventListener('click', async () => {
  const data = await api('/api/action', { action: 'bid', bid_yes: false });
  if (data) { gs = data; render(data); }
});

// ── Trump suit selection ───────────────────────────────────────
document.querySelectorAll('.suit-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.suit-btn').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    selectedTrump = btn.dataset.suit;
    populatePartnerSelects(selectedTrump);
  });
});

function populatePartnerSelects(trump) {
  if (!gs) return;
  const myHand = new Set(gs.hand.map(h => h.card));
  const suits = { s: '♠', h: '♥', d: '♦', c: '♣' };
  const ranks = [
    ['a','A'],['k','K'],['q','Q'],['j','J'],['t','10'],
    ['9','9'],['8','8'],['7','7'],['6','6'],['5','5'],['4','4'],['3','3'],
  ];

  const makeOptions = (exclude) => {
    const opts = ['<option value="">-- Select Card --</option>'];
    for (const s of Object.keys(suits)) {
      for (const [r, rd] of ranks) {
        const card = s + r;
        if (!myHand.has(card) && card !== exclude) {
          const color = (s === 'h' || s === 'd') ? 'color:red' : '';
          opts.push(`<option value="${card}" style="${color}">${suits[s]}${rd}</option>`);
        }
      }
    }
    return opts.join('');
  };

  const sel1 = $('partner-1-select');
  const sel2 = $('partner-2-select');
  const cur1 = sel1.value;
  const cur2 = sel2.value;
  sel1.innerHTML = makeOptions(cur2);
  sel2.innerHTML = makeOptions(cur1);
  if (cur1) sel1.value = cur1;
  if (cur2) sel2.value = cur2;
}

// Keep partner selects in sync (prevent same selection)
$('partner-1-select').addEventListener('change', () => populatePartnerSelects(selectedTrump));
$('partner-2-select').addEventListener('change', () => populatePartnerSelects(selectedTrump));

$('btn-set-trump').addEventListener('click', async () => {
  $('trump-error').classList.add('hidden');
  const trump = selectedTrump;
  const p1 = $('partner-1-select').value;
  const p2 = $('partner-2-select').value;
  if (!trump) return showError('Please select a trump suit.');
  if (!p1 || !p2) return showError('Please select both partner cards.');
  if (p1 === p2) return showError('Partner cards must be different.');
  const data = await api('/api/action', { action: 'set_trump', trump, partner_1: p1, partner_2: p2 });
  if (data) { gs = data; render(data); }
});

// ── Next round ─────────────────────────────────────────────────
$('btn-next-round').addEventListener('click', async () => {
  selectedTrump = null;
  const data = await api('/api/action', { action: 'new_round' });
  if (data) { gs = data; render(data); }
});

// ── Card click ─────────────────────────────────────────────────
function onCardClick(card) {
  return async () => {
    const data = await api('/api/action', { action: 'play_card', card });
    if (data) { gs = data; render(data); }
  };
}

// ── Main render ────────────────────────────────────────────────
function render(state) {
  gs = state;

  renderTopBar(state);
  renderOpponents(state);
  renderTrick(state);
  renderLastTrick(state);
  renderPlayerHand(state);
  renderLog(state);
  renderPhasePanel(state);
  renderPartnerInfo(state);
  updateOpponentHighlights(state);
}

function renderTopBar(s) {
  const bid = $('info-bid');
  const trump = $('info-trump');
  const trick = $('info-trick');

  if (s.phase === 'bidding') {
    bid.innerHTML = `Bid: <strong>${s.bid}</strong>`;
    trump.innerHTML = '';
    trick.innerHTML = '';
  } else {
    bid.innerHTML = `Bid: <strong>${s.bid}</strong>` +
      (s.bidder_name ? ` by <strong>${s.bidder_name}</strong>` : '');
    trump.innerHTML = s.trump
      ? `Trump: <strong style="color:${SUIT_COLOR[s.trump]==='red'?'#f88':'#eee'}">${s.trump_symbol} ${s.trump_name}</strong>`
      : '';
    trick.innerHTML = s.phase === 'playing'
      ? `Trick: <strong>${s.trick_num}/8</strong>` : '';
  }
}

function renderOpponents(s) {
  const names = s.player_names || {};
  for (let p = 2; p <= 6; p++) {
    const name = names[p] || `Player ${p}`;
    const score = (s.game_scores || {})[name] || 0;
    const el = $(`seat-${p}`);
    $(`opp-name-${p}`).textContent = name;
    $(`opp-score-${p}`).textContent = `${score} pts`;
    // Show how many cards remain
    const hand = (s.hands || {})[p];
    const cardsArea = el.querySelector('.opp-cards');
    const count = hand ? hand.length : 8;
    const backs = Array.from({length: Math.min(count, 5)}, () => '<span class="card-back">🂠</span>').join('');
    cardsArea.innerHTML = backs || '–';
  }
}

function renderTrick(s) {
  // Clear all slots
  for (let i = 1; i <= 6; i++) {
    const slot = $(`trick-slot-${i}`);
    slot.innerHTML = '';
  }

  if (!s.trick_display || !s.trick_leader) return;

  // Map player → slot position (1-indexed)
  // Slot 1 = leader (trick_leader), going around
  const leader = s.trick_leader;
  const orderMap = {};  // player_num -> slot (1-6)
  if (leader) {
    for (let i = 0; i < 6; i++) {
      const pnum = ((leader - 1 + i) % 6) + 1;
      orderMap[pnum] = i + 1;
    }
  }

  // Find winning card index among played cards
  let winIdx = -1;
  if (s.trick_display.length > 0 && s.trump) {
    let bestCard = null;
    let bestIdx = -1;
    const firstSuit = s.trick_display[0].suit;
    for (let i = 0; i < s.trick_display.length; i++) {
      const card = s.trick_display[i].card;
      if (jsBeats(card, bestCard, s.trump, firstSuit)) {
        bestCard = card;
        bestIdx = i;
      }
    }
    winIdx = bestIdx;
  }

  s.trick_display.forEach((entry, idx) => {
    const slot = orderMap[entry.player_num];
    if (!slot) return;
    const slotEl = $(`trick-slot-${slot}`);
    const isWinning = (idx === winIdx);
    const color = SUIT_COLOR[entry.suit] || 'black';
    slotEl.innerHTML = `
      <div class="trick-slot-label">${escHtml(entry.player_name)}</div>
      <div class="trick-card ${color}-suit${isWinning ? ' winning' : ''}">${escHtml(entry.card_disp)}</div>
    `;
  });

  // Show empty slot labels for positions not yet played
  if (leader) {
    for (let i = 0; i < 6; i++) {
      const pnum = ((leader - 1 + i) % 6) + 1;
      const slot = i + 1;
      const slotEl = $(`trick-slot-${slot}`);
      if (!slotEl.innerHTML) {
        const name = (s.player_names || {})[pnum] || `P${pnum}`;
        const isCurrent = (s.current_player === pnum);
        slotEl.innerHTML = `
          <div class="trick-slot-label">${escHtml(name)}</div>
          <div class="trick-card" style="opacity:0.15;border-style:dashed;${isCurrent ? 'border-color:#ffdd55;opacity:0.4' : ''}"></div>
        `;
      }
    }
  }
}

// JS mirror of Python beats() for client-side win detection
function jsBeats(challenger, champion, trump, firstSuit) {
  if (!champion) return true;
  const cT = challenger[0] === trump;
  const pT = champion[0] === trump;
  const RANK = 'akqjt98765 43'; // unused; use order array instead
  const RO = {a:1,k:2,q:3,j:4,t:5,9:6,8:7,7:8,6:9,5:10,4:11,3:12};
  if (cT && !pT) return true;
  if (!cT && pT) return false;
  if (cT && pT) return RO[challenger[1]] < RO[champion[1]];
  if (challenger[0] === firstSuit && champion[0] !== firstSuit) return true;
  if (challenger[0] === firstSuit && champion[0] === firstSuit)
    return RO[challenger[1]] < RO[champion[1]];
  return false;
}

function renderLastTrick(s) {
  const banner = $('last-trick-banner');
  if (!s.last_trick) {
    banner.classList.add('hidden');
    return;
  }
  banner.classList.remove('hidden');
  const lt = s.last_trick;
  const cards = lt.cards.map(c =>
    `<span style="color:${SUIT_COLOR[c.suit]==='red'?'#f88':'#eee'}">${escHtml(c.disp)}</span>`
  ).join(' ');
  banner.innerHTML = `Last trick won by <strong>${escHtml(lt.winner_name)}</strong>: ${cards}`;
}

function renderPlayerHand(s) {
  const handEl = $('player-hand');
  handEl.innerHTML = '';
  $('player-name-label').textContent = s.player_name || 'You';
  const score = (s.game_scores || {})[s.player_name] || 0;
  $('player-score-label').textContent = `${score} pts`;

  if (!s.hand || s.hand.length === 0) {
    handEl.innerHTML = '<span style="color:#777;font-size:0.85rem">No cards</span>';
    return;
  }

  const isPlayingPhase = (s.phase === 'playing');
  const isMyTurn = (s.current_player === 1);

  let lastSuit = null;
  s.hand.forEach(item => {
    // Add spacer between suit groups
    if (lastSuit !== null && item.card[0] !== lastSuit) {
      const spacer = document.createElement('div');
      spacer.className = 'card-group-spacer';
      handEl.appendChild(spacer);
    }
    lastSuit = item.card[0];

    const el = document.createElement('div');
    const suit = item.card[0];
    const rank = item.card[1];
    const isBlack3 = (item.card === 's3');
    const colorClass = SUIT_COLOR[suit] === 'red' ? 'red-suit' : 'black-suit';

    el.className = `card ${colorClass}`;
    if (isBlack3) el.classList.add('black3-card');

    if (isPlayingPhase && isMyTurn) {
      if (item.valid) {
        el.classList.add('playable');
        el.addEventListener('click', onCardClick(item.card));
      } else {
        el.classList.add('not-valid');
      }
    }

    const SUIT_SYM = { s: '♠', h: '♥', d: '♦', c: '♣' };
    const RANK_D = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};
    el.innerHTML = `<div class="card-rank">${RANK_D[rank]||rank}</div><div class="card-suit">${SUIT_SYM[suit]}</div>`;
    el.title = `${RANK_D[rank]||rank} of ${suit === 's' ? 'Spades' : suit === 'h' ? 'Hearts' : suit === 'd' ? 'Diamonds' : 'Clubs'}` + (isBlack3 ? ' (30 pts!)' : '');

    handEl.appendChild(el);
  });
}

function renderLog(s) {
  const logEl = $('game-log');
  logEl.innerHTML = '';
  (s.log || []).forEach(line => {
    const div = document.createElement('div');
    div.className = 'log-line' + (line.startsWith('─') ? ' separator' : '');
    div.textContent = line;
    logEl.appendChild(div);
  });
  logEl.scrollTop = logEl.scrollHeight;
}

function renderPhasePanel(s) {
  // Hide all panels
  $('panel-bidding').classList.add('hidden');
  $('panel-set-trump').classList.add('hidden');
  $('panel-scoring').classList.add('hidden');

  if (s.phase === 'bidding') {
    $('panel-bidding').classList.remove('hidden');
    const nextBid = s.bid + 10;
    $('bid-amount').textContent = s.bid;
    $('bid-next-amount').textContent = nextBid;
    $('bid-yes-label').textContent = nextBid;
    // Disable bid button if already at 270
    $('btn-bid-yes').disabled = (s.bid >= 270);

    // Is it actually player 1's turn?
    const myTurn = !s.passed[1];
    $('btn-bid-yes').style.opacity = myTurn ? '1' : '0.5';
    $('btn-bid-no').style.opacity = myTurn ? '1' : '0.5';

  } else if (s.phase === 'set_trump' && s.bidder === 1) {
    $('panel-set-trump').classList.remove('hidden');
    // Reset suit selection
    if (!selectedTrump) {
      document.querySelectorAll('.suit-btn').forEach(b => b.classList.remove('selected'));
    }
    populatePartnerSelects(selectedTrump);

  } else if (s.phase === 'scoring') {
    $('panel-scoring').classList.remove('hidden');
    renderScoringPanel(s);
  }
}

function renderScoringPanel(s) {
  const rr = s.round_result;
  if (!rr) return;

  const headline = $('score-headline');
  if (rr.bidder_won) {
    headline.textContent = `🎉 ${rr.bidder_name}'s team wins!`;
    headline.style.color = '#6eff6e';
  } else {
    headline.textContent = `😔 ${rr.bidder_name}'s team fails!`;
    headline.style.color = '#ff7070';
  }

  $('score-details').innerHTML = `
    Bid: <strong>${rr.bid}</strong> &nbsp;|&nbsp;
    Team collected: <strong>${rr.team_pts}</strong> pts<br>
    Bidder: <strong>${escHtml(rr.bidder_name)}</strong> &nbsp;|&nbsp;
    Partners: <strong>${escHtml(rr.partner_1_name)}</strong> &amp; <strong>${escHtml(rr.partner_2_name)}</strong>
  `;

  const tbody = $('score-tbody');
  tbody.innerHTML = '';
  const scores = s.game_scores || {};
  const sorted = Object.entries(scores).sort((a, b) => b[1] - a[1]);
  sorted.forEach(([name, pts]) => {
    const tr = document.createElement('tr');
    if (name === s.player_name) tr.className = 'winner-row';
    tr.innerHTML = `<td>${escHtml(name)}</td><td>${pts}</td>`;
    tbody.appendChild(tr);
  });
}

function renderPartnerInfo(s) {
  const box = $('partner-info-box');
  if (s.phase === 'playing' && s.partner_1 && s.partner_2) {
    box.classList.remove('hidden');
    const p1n = s.player_names[s.partner_1_player] || '?';
    const p2n = s.player_names[s.partner_2_player] || '?';
    const symi = { s:'♠', h:'♥', d:'♦', c:'♣' };
    const rdisp = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};
    const p1d = symi[s.partner_1[0]] + rdisp[s.partner_1[1]];
    const p2d = symi[s.partner_2[0]] + rdisp[s.partner_2[1]];
    box.innerHTML = `<div id="partner-info-text">Partners: ${escHtml(p1d)} (${escHtml(p1n)}) &amp; ${escHtml(p2d)} (${escHtml(p2n)})</div>`;
  } else {
    box.classList.add('hidden');
  }
}

function updateOpponentHighlights(s) {
  for (let p = 2; p <= 6; p++) {
    const el = $(`seat-${p}`);
    el.classList.remove('active-turn', 'bidder-seat', 'partner-seat');
    if (s.current_player === p) el.classList.add('active-turn');
    if (s.bidder === p) el.classList.add('bidder-seat');
    if (s.partner_1_player === p || s.partner_2_player === p) el.classList.add('partner-seat');
  }
}

// ── Utility ────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
