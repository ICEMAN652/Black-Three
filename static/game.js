'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let gs = null;
let selectedTrump = null;
let myRoomCode = null;
let mySeat = null;
let myName = 'Player';

const SUIT_COLOR = { s: 'black', h: 'red', d: 'red', c: 'black' };

// ── DOM helpers ────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const screens = {
  setup: $('screen-setup'),
  lobby: $('screen-lobby'),
  game:  $('screen-game'),
};

function showScreen(name) {
  Object.values(screens).forEach(s => s.classList.remove('active'));
  screens[name].classList.add('active');
}

function showError(msg) {
  const el = $('trump-error');
  if (el && !el.closest('.hidden') || el) {
    el.textContent = msg;
    el.classList.remove('hidden');
    setTimeout(() => el.classList.add('hidden'), 4000);
  } else {
    alert(msg);
  }
}

function showSetupError(msg) {
  const el = $('setup-error');
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 4000);
}

// ── Connection loader ──────────────────────────────────────────────────────
let _loaderPct = 0;
let _loaderTimer = null;
let _joinLoaderPct = 0;
let _joinLoaderTimer = null;

function loaderSet(pct) {
  _loaderPct = pct;
  const fill = $('loader-fill');
  if (fill) fill.style.width = pct + '%';
}

function loaderStart(msg) {
  const loader = $('connect-loader');
  const text   = $('loader-text');
  if (loader) loader.style.display = '';
  if (text)   text.textContent = msg || 'Connecting to server…';
  loaderSet(0);
  if (_loaderTimer) clearInterval(_loaderTimer);
  _loaderTimer = setInterval(() => {
    const gap = 99 - _loaderPct;
    const step = Math.max(0.4, gap * 0.06);
    loaderSet(Math.min(99, _loaderPct + step));
  }, 180);
}

function loaderFinish() {
  if (_loaderTimer) { clearInterval(_loaderTimer); _loaderTimer = null; }
  loaderSet(100);
  setTimeout(() => {
    const loader = $('connect-loader');
    if (loader) loader.style.display = 'none';
  }, 500);
}

function joinLoaderStart() {
  const jl = $('join-loader');
  if (jl) jl.classList.remove('hidden');
  _joinLoaderPct = 0;
  const fill = $('join-loader-fill');
  if (fill) fill.style.width = '0%';
  if (_joinLoaderTimer) clearInterval(_joinLoaderTimer);
  _joinLoaderTimer = setInterval(() => {
    const gap = 99 - _joinLoaderPct;
    const step = Math.max(0.5, gap * 0.08);
    _joinLoaderPct = Math.min(99, _joinLoaderPct + step);
    const fill = $('join-loader-fill');
    if (fill) fill.style.width = _joinLoaderPct + '%';
  }, 150);
}

function joinLoaderStop() {
  if (_joinLoaderTimer) { clearInterval(_joinLoaderTimer); _joinLoaderTimer = null; }
  const fill = $('join-loader-fill');
  if (fill) fill.style.width = '100%';
  setTimeout(() => {
    const jl = $('join-loader');
    if (jl) jl.classList.add('hidden');
  }, 400);
}

// ── Socket.IO ──────────────────────────────────────────────────────────────
const socket = io();

// Disable setup buttons and start loader immediately
$('btn-create-room').disabled = true;
$('btn-show-join').disabled = true;
$('btn-join-submit').disabled = true;
loaderStart();

socket.on('connect', () => {
  $('btn-create-room').disabled = false;
  $('btn-show-join').disabled = false;
  $('btn-join-submit').disabled = false;
  loaderFinish();
  // Reconnection recovery: if we were in a room, rejoin it
  if (myRoomCode && mySeat) {
    socket.emit('rejoin_room', { code: myRoomCode, seat: mySeat, name: myName });
  }
});

socket.on('disconnect', () => {
  if (screens.setup.classList.contains('active')) {
    $('btn-create-room').disabled = true;
    $('btn-show-join').disabled = true;
    $('btn-join-submit').disabled = true;
    loaderStart('Reconnecting…');
  }
});

socket.on('lobby_update', (data) => {
  joinLoaderStop();
  myRoomCode = data.room_code;
  mySeat = data.my_seat;
  renderLobby(data);
  showScreen('lobby');
});

socket.on('game_state', (data) => {
  gs = data;
  selectedTrump = null;
  myRoomCode = data.room_code;
  mySeat = data.my_seat;
  render(data);
  showScreen('game');
});

socket.on('join_error', (data) => {
  joinLoaderStop();
  $('btn-join-submit').disabled = false;
  showSetupError(data.msg);
});

socket.on('room_lost', () => {
  myRoomCode = null;
  mySeat = null;
  showScreen('setup');
  showSetupError('Room no longer exists. Please create or join a new room.');
});

socket.on('game_error', (data) => {
  showError(data.msg);
});

// ── Setup screen ───────────────────────────────────────────────────────────
$('btn-create-room').addEventListener('click', () => {
  myName = $('player-name').value.trim() || 'Player';
  socket.emit('create_room', { name: myName });
});

$('btn-show-join').addEventListener('click', () => {
  $('join-section').classList.toggle('hidden');
  $('btn-show-join').textContent =
    $('join-section').classList.contains('hidden') ? 'Join Room' : 'Cancel';
});

$('btn-join-submit').addEventListener('click', () => {
  myName = $('player-name').value.trim() || 'Player';
  const code = $('join-code-input').value.trim().toUpperCase();
  if (!code) { showSetupError('Enter a room code.'); return; }
  $('btn-join-submit').disabled = true;
  joinLoaderStart();
  socket.emit('join_room_req', { name: myName, code });
});

$('join-code-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('btn-join-submit').click();
});

$('player-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') $('btn-create-room').click();
});

// ── Lobby screen ───────────────────────────────────────────────────────────
$('btn-start-game').addEventListener('click', () => {
  socket.emit('start_game', {});
});

$('btn-leave-lobby').addEventListener('click', () => {
  myRoomCode = null; mySeat = null;
  socket.disconnect();
  socket.connect();
  showScreen('setup');
});

function renderLobby(data) {
  $('lobby-room-code').textContent = data.room_code;
  const list = $('lobby-player-list');
  list.innerHTML = '';
  data.players.forEach(p => {
    const li = document.createElement('li');
    li.textContent = `Seat ${p.seat}: ${escHtml(p.name)}`;
    if (p.is_bot) li.textContent += ' (Bot)';
    list.appendChild(li);
  });

  const emptySlots = 6 - data.players.length;
  if (emptySlots > 0) {
    const li = document.createElement('li');
    li.className = 'empty-slots';
    li.textContent = `+ ${emptySlots} empty slot${emptySlots > 1 ? 's' : ''} (will be filled by bots)`;
    list.appendChild(li);
  }

  $('btn-start-game').style.display = data.is_host ? 'inline-block' : 'none';
  $('lobby-waiting-msg').style.display = data.is_host ? 'none' : 'block';
}

// ── Top bar ────────────────────────────────────────────────────────────────
$('btn-toggle-log').addEventListener('click', () => {
  const logArea = document.querySelector('.log-area');
  logArea.classList.toggle('log-hidden-mobile');
  $('btn-toggle-log').textContent = logArea.classList.contains('log-hidden-mobile') ? 'Log' : 'Hide Log';
});

$('btn-new-game-top').addEventListener('click', () => {
  if (confirm('Leave game and go back to setup?')) {
    myRoomCode = null; mySeat = null;
    socket.disconnect();
    socket.connect();
    gs = null;
    selectedTrump = null;
    showScreen('setup');
  }
});

// ── Bid buttons ────────────────────────────────────────────────────────────
$('btn-bid-yes').addEventListener('click', () => {
  socket.emit('bid_action', { bid_yes: true });
});
$('btn-bid-no').addEventListener('click', () => {
  socket.emit('bid_action', { bid_yes: false });
});

// ── Trump suit selection ───────────────────────────────────────────────────
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

$('partner-1-select').addEventListener('change', () => populatePartnerSelects(selectedTrump));
$('partner-2-select').addEventListener('change', () => populatePartnerSelects(selectedTrump));

$('btn-set-trump').addEventListener('click', () => {
  $('trump-error').classList.add('hidden');
  const trump = selectedTrump;
  const p1 = $('partner-1-select').value;
  const p2 = $('partner-2-select').value;
  if (!trump) return showError('Please select a trump suit.');
  if (!p1 || !p2) return showError('Please select both partner cards.');
  if (p1 === p2) return showError('Partner cards must be different.');
  socket.emit('set_trump_action', { trump, partner_1: p1, partner_2: p2 });
});

// ── Next round ─────────────────────────────────────────────────────────────
$('btn-next-round').addEventListener('click', () => {
  selectedTrump = null;
  socket.emit('new_round_action', {});
});

// ── Card click ─────────────────────────────────────────────────────────────
function onCardClick(card) {
  return () => socket.emit('play_card_action', { card });
}

// ── Main render ────────────────────────────────────────────────────────────
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
  const hasBots = s.seat_types && Object.values(s.seat_types).some(v => v === true);
  const rcEl = $('info-room-code');
  if (s.room_code && hasBots) {
    rcEl.innerHTML = `Room: <strong>${escHtml(s.room_code)}</strong>`;
    rcEl.title = 'Share this code — open bot seats can be joined mid-game';
  } else {
    rcEl.innerHTML = '';
  }
  $('info-bid').innerHTML = `Bid: <strong>${s.bid}</strong>` +
    (s.bidder_name && s.phase !== 'bidding' ? ` by <strong>${escHtml(s.bidder_name)}</strong>` : '');
  $('info-trump').innerHTML = s.trump
    ? `Trump: <strong style="color:${SUIT_COLOR[s.trump]==='red'?'#f88':'#eee'}">${s.trump_symbol} ${s.trump_name}</strong>`
    : '';
  $('info-trick').innerHTML = s.phase === 'playing'
    ? `Trick: <strong>${s.trick_num}/8</strong>` : '';

  const pEl = $('info-partners');
  if (s.partner_1 && s.partner_2 && s.phase !== 'bidding' && s.phase !== 'set_trump') {
    const SYMI = { s:'♠', h:'♥', d:'♦', c:'♣' };
    const RD = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};
    const p1d = SYMI[s.partner_1[0]] + RD[s.partner_1[1]];
    const p2d = SYMI[s.partner_2[0]] + RD[s.partner_2[1]];
    const p1c = (s.partner_1[0]==='h'||s.partner_1[0]==='d') ? '#f88' : '#eee';
    const p2c = (s.partner_2[0]==='h'||s.partner_2[0]==='d') ? '#f88' : '#eee';
    const p1name = s.partner_1_player ? ` (${escHtml(s.player_names[s.partner_1_player])})` : (s.partner_1_revealed ? ' (played)' : ' (?)')  ;
    const p2name = s.partner_2_player ? ` (${escHtml(s.player_names[s.partner_2_player])})` : (s.partner_2_revealed ? ' (played)' : ' (?)');
    pEl.innerHTML = `Partners: <strong style="color:${p1c}">${p1d}</strong><span style="color:#888;font-size:0.8em">${p1name}</span> &amp; <strong style="color:${p2c}">${p2d}</strong><span style="color:#888;font-size:0.8em">${p2name}</span>`;
  } else {
    pEl.innerHTML = '';
  }
}

function renderOpponents(s) {
  const oppDisplay = s.opp_display || [2, 3, 4, 5, 6];
  const names = s.player_names || {};
  const scores = s.game_scores || {};
  const counts = s.hand_counts || {};

  for (let slot = 2; slot <= 6; slot++) {
    const seat = oppDisplay[slot - 2];
    const name = names[seat] || `Seat ${seat}`;
    const score = scores[name] || 0;
    const count = counts[String(seat)] ?? 8;

    $(`opp-name-${slot}`).textContent = name;
    $(`opp-score-${slot}`).textContent = `${score} pts`;

    const cardsArea = document.querySelector(`#seat-${slot} .opp-cards`);
    const backs = Array.from({ length: Math.min(count, 5) }, () => '<span class="card-back">🂠</span>').join('');
    cardsArea.innerHTML = backs || '–';

    // Store actual seat on the element for highlight lookup
    const seatEl = $(`seat-${slot}`);
    seatEl.dataset.actualSeat = seat;
  }
}

function renderTrick(s) {
  for (let i = 1; i <= 6; i++) $(`trick-slot-${i}`).innerHTML = '';
  if (!s.trick_display || !s.trick_leader) return;

  const leader = s.trick_leader;
  const orderMap = {};
  for (let i = 0; i < 6; i++) {
    const pnum = ((leader - 1 + i) % 6) + 1;
    orderMap[pnum] = i + 1;
  }

  let winIdx = -1;
  if (s.trick_display.length > 0 && s.trump) {
    let bestCard = null, bestIdx = -1;
    const firstSuit = s.trick_display[0].suit;
    s.trick_display.forEach((entry, i) => {
      if (jsBeats(entry.card, bestCard, s.trump, firstSuit)) {
        bestCard = entry.card; bestIdx = i;
      }
    });
    winIdx = bestIdx;
  }

  s.trick_display.forEach((entry, idx) => {
    const slot = orderMap[entry.player_num];
    if (!slot) return;
    const slotEl = $(`trick-slot-${slot}`);
    const isWinning = idx === winIdx;
    const color = SUIT_COLOR[entry.suit] || 'black';
    slotEl.innerHTML = `
      <div class="trick-slot-label">${escHtml(entry.player_name)}</div>
      <div class="trick-card ${color}-suit${isWinning ? ' winning' : ''}">${escHtml(entry.card_disp)}</div>
    `;
  });

  if (leader) {
    for (let i = 0; i < 6; i++) {
      const pnum = ((leader - 1 + i) % 6) + 1;
      const slot = i + 1;
      const slotEl = $(`trick-slot-${slot}`);
      if (!slotEl.innerHTML) {
        const name = (s.player_names || {})[pnum] || `P${pnum}`;
        const isCurrent = s.current_player === pnum;
        slotEl.innerHTML = `
          <div class="trick-slot-label">${escHtml(name)}</div>
          <div class="trick-card" style="opacity:0.15;border-style:dashed;${isCurrent ? 'border-color:#ffdd55;opacity:0.4' : ''}"></div>
        `;
      }
    }
  }
}

function jsBeats(challenger, champion, trump, firstSuit) {
  if (!champion) return true;
  const RO = {a:1,k:2,q:3,j:4,t:5,9:6,8:7,7:8,6:9,5:10,4:11,3:12};
  const cT = challenger[0] === trump, pT = champion[0] === trump;
  if (cT && !pT) return true;
  if (!cT && pT) return false;
  if (cT && pT) return RO[challenger[1]] < RO[champion[1]];
  if (challenger[0] === firstSuit && champion[0] !== firstSuit) return true;
  if (challenger[0] === firstSuit && champion[0] === firstSuit) return RO[challenger[1]] < RO[champion[1]];
  return false;
}

function renderLastTrick(s) {
  const banner = $('last-trick-banner');
  if (!s.last_trick) { banner.classList.add('hidden'); return; }
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
  $('player-name-label').textContent = s.my_name || 'You';
  const score = (s.game_scores || {})[s.my_name] || 0;
  $('player-score-label').textContent = `${score} pts`;

  if (!s.hand || s.hand.length === 0) {
    handEl.innerHTML = '<span style="color:#777;font-size:0.85rem">No cards</span>';
    return;
  }

  const isPlayingPhase = s.phase === 'playing';
  const isMyTurn = s.is_my_play_turn;
  const SUIT_SYM = { s: '♠', h: '♥', d: '♦', c: '♣' };
  const RANK_D = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};

  let lastSuit = null;
  s.hand.forEach(item => {
    if (lastSuit !== null && item.card[0] !== lastSuit) {
      const spacer = document.createElement('div');
      spacer.className = 'card-group-spacer';
      handEl.appendChild(spacer);
    }
    lastSuit = item.card[0];

    const el = document.createElement('div');
    const suit = item.card[0];
    const rank = item.card[1];
    const isBlack3 = item.card === 's3';
    el.className = `card ${SUIT_COLOR[suit] === 'red' ? 'red-suit' : 'black-suit'}`;
    if (isBlack3) el.classList.add('black3-card');

    if (isPlayingPhase && isMyTurn) {
      if (item.valid) {
        el.classList.add('playable');
        el.addEventListener('click', onCardClick(item.card));
      } else {
        el.classList.add('not-valid');
      }
    }

    el.innerHTML = `<div class="card-rank">${RANK_D[rank]||rank}</div><div class="card-suit">${SUIT_SYM[suit]}</div>`;
    el.title = `${RANK_D[rank]||rank} of ${suit==='s'?'Spades':suit==='h'?'Hearts':suit==='d'?'Diamonds':'Clubs'}` + (isBlack3 ? ' (30 pts!)' : '');
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
  $('panel-bidding').classList.add('hidden');
  $('panel-set-trump').classList.add('hidden');
  $('panel-scoring').classList.add('hidden');
  $('panel-waiting').classList.add('hidden');

  const msgEl = $('message-box');
  msgEl.textContent = s.message || '';

  if (s.phase === 'bidding') {
    if (s.is_my_bid_turn) {
      $('panel-bidding').classList.remove('hidden');
      const nextBid = s.bid + 10;
      $('bid-amount').textContent = s.bid;
      $('bid-next-amount').textContent = nextBid;
      $('bid-yes-label').textContent = nextBid;
      $('btn-bid-yes').disabled = s.bid >= 270;
    } else {
      $('panel-waiting').classList.remove('hidden');
      const bName = s.bidding_seat ? (s.player_names[s.bidding_seat] || '...') : '...';
      $('waiting-msg').textContent = `Waiting for ${bName} to bid…`;
    }

  } else if (s.phase === 'set_trump') {
    if (s.is_my_trump_turn) {
      $('panel-set-trump').classList.remove('hidden');
      if (!selectedTrump) {
        document.querySelectorAll('.suit-btn').forEach(b => b.classList.remove('selected'));
      }
      populatePartnerSelects(selectedTrump);
    } else {
      $('panel-waiting').classList.remove('hidden');
      $('waiting-msg').textContent = `Waiting for ${escHtml(s.bidder_name || '...')} to set trump…`;
    }

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
    headline.textContent = `${rr.bidder_name}'s team wins!`;
    headline.style.color = '#6eff6e';
  } else {
    headline.textContent = `${rr.bidder_name}'s team fails!`;
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
  const sorted = Object.entries(s.game_scores || {}).sort((a, b) => b[1] - a[1]);
  sorted.forEach(([name, pts]) => {
    const tr = document.createElement('tr');
    if (name === s.my_name) tr.className = 'winner-row';
    tr.innerHTML = `<td>${escHtml(name)}</td><td>${pts}</td>`;
    tbody.appendChild(tr);
  });
}

function renderPartnerInfo(s) {
  const box = $('partner-info-box');
  if (s.phase === 'playing' && s.partner_1 && s.partner_2) {
    box.classList.remove('hidden');
    const symi = { s:'♠', h:'♥', d:'♦', c:'♣' };
    const rdisp = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};
    const p1d = symi[s.partner_1[0]] + rdisp[s.partner_1[1]];
    const p2d = symi[s.partner_2[0]] + rdisp[s.partner_2[1]];
    const p1n = s.partner_1_player ? escHtml(s.player_names[s.partner_1_player]) : (s.partner_1_revealed ? 'revealed' : '?');
    const p2n = s.partner_2_player ? escHtml(s.player_names[s.partner_2_player]) : (s.partner_2_revealed ? 'revealed' : '?');
    box.innerHTML = `<div>Partners: ${escHtml(p1d)} (${p1n}) &amp; ${escHtml(p2d)} (${p2n})</div>`;
  } else {
    box.classList.add('hidden');
  }
}

function updateOpponentHighlights(s) {
  for (let slot = 2; slot <= 6; slot++) {
    const el = $(`seat-${slot}`);
    const actualSeat = parseInt(el.dataset.actualSeat || slot);
    el.classList.remove('active-turn', 'bidder-seat', 'partner-seat');
    if (s.current_player === actualSeat) el.classList.add('active-turn');
    if (s.bidder === actualSeat) el.classList.add('bidder-seat');
    if (s.partner_1_player === actualSeat || s.partner_2_player === actualSeat) el.classList.add('partner-seat');
  }
}

// ── Utility ────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
