'use strict';

// ══════════════════════════════════════════════════════════════════════════════
// game.js — Three of Spades client-side logic
//
// This file handles everything the browser does:
//   • Connects to the server via Socket.IO
//   • Stores the latest game state (gs) received from the server
//   • Renders the full UI on every state update (render() is the main entry point)
//   • Sends player actions back to the server (bid, play card, set trump, etc.)
//
// The server is the single source of truth — we never change gs locally.
// Every action emits a socket event; the server responds with a new game_state.
// ══════════════════════════════════════════════════════════════════════════════

// ── State ──────────────────────────────────────────────────────────────────
let gs = null;             // latest game state received from the server
let selectedTrump = null;  // trump suit the user has clicked in the set-trump panel
let selectedP1 = null;     // partner card 1 the user has clicked
let selectedP2 = null;     // partner card 2 the user has clicked
let prevPhase = null;      // used to detect when the phase changes (e.g. set_trump → playing)
let myRoomCode = null;     // stored in memory for reconnection recovery
let mySeat = null;         // my seat number (1-6), set when we join a room
let myName = 'Player';     // my display name, used for rejoin recovery

// Maps suit letter to CSS color class (used when rendering cards)
const SUIT_COLOR = { s: 'black', h: 'red', d: 'red', c: 'black' };

// ── DOM helpers ────────────────────────────────────────────────────────────
// Shorthand for document.getElementById — used everywhere below
const $ = id => document.getElementById(id);

// The three top-level screen divs; only one is 'active' (visible) at a time
const screens = {
  setup: $('screen-setup'),
  lobby: $('screen-lobby'),
  game:  $('screen-game'),
};

function showScreen(name) {
  // Hide all screens, then show the requested one
  Object.values(screens).forEach(s => s.classList.remove('active'));
  screens[name].classList.add('active');
}

function showError(msg) {
  // Show an in-game error (e.g. "Must follow Hearts suit") in the trump-error div
  const el = $('trump-error');
  if (el && !el.closest('.hidden') || el) {
    el.textContent = msg;
    el.classList.remove('hidden');
    setTimeout(() => el.classList.add('hidden'), 4000);
  } else {
    alert(msg);   // fallback if element isn't present
  }
}

function showSetupError(msg) {
  // Show an error on the setup/lobby screen (e.g. "Room not found")
  const el = $('setup-error');
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 4000);
}

// ── Connection loader ──────────────────────────────────────────────────────
// The setup screen shows an animated progress bar while connecting to the server.
// loaderStart() begins an easing animation toward 99%; loaderFinish() snaps to 100%.
// A separate join loader appears when submitting the join-room form.
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
  // Ease toward 99% — never actually hits 100% until loaderFinish() is called
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
  // Show the join-room progress bar and animate it toward 99%
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
  // Snap to 100% and hide the join loader after a short delay
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

// Buttons are disabled immediately on page load until the socket connects
// (prevents actions before the server is ready)
$('btn-create-room').disabled = true;
$('btn-show-join').disabled = true;
$('btn-join-submit').disabled = true;
loaderStart();

socket.on('connect', () => {
  // Socket is live — enable buttons and finish the loading bar
  $('btn-create-room').disabled = false;
  $('btn-show-join').disabled = false;
  $('btn-join-submit').disabled = false;
  loaderFinish();
  // Reconnection recovery: if we remember a room from before the page refresh,
  // ask the server to restore our seat (on_rejoin_room in app.py)
  if (myRoomCode && mySeat) {
    socket.emit('rejoin_room', { code: myRoomCode, seat: mySeat, name: myName });
  }
});

socket.on('disconnect', () => {
  // If user is on the setup screen when they lose connection, show the reconnecting bar
  if (screens.setup.classList.contains('active')) {
    $('btn-create-room').disabled = true;
    $('btn-show-join').disabled = true;
    $('btn-join-submit').disabled = true;
    loaderStart('Reconnecting…');
  }
});

socket.on('lobby_update', (data) => {
  // Server sends this when the lobby roster changes; renders lobby and switches screen
  joinLoaderStop();
  myRoomCode = data.room_code;
  mySeat = data.my_seat;
  renderLobby(data);
  showScreen('lobby');
});

socket.on('game_state', (data) => {
  // Main game update — received after every player action or bot move
  gs = data;
  selectedTrump = null;  // reset trump selection on each state refresh
  myRoomCode = data.room_code;
  mySeat = data.my_seat;
  render(data);
  showScreen('game');
});

socket.on('join_error', (data) => {
  // Server rejected the join request (room full, not found, etc.)
  joinLoaderStop();
  $('btn-join-submit').disabled = false;
  showSetupError(data.msg);
});

socket.on('room_lost', () => {
  // Room was deleted (everyone left, or server restarted) — send back to setup
  myRoomCode = null;
  mySeat = null;
  showScreen('setup');
  showSetupError('Room no longer exists. Please create or join a new room.');
});

socket.on('kicked', () => {
  // This player was kicked by the host or by vote — send back to setup
  myRoomCode = null;
  mySeat = null;
  showScreen('setup');
  showSetupError('You were kicked from the room by the host.');
});

socket.on('game_error', (data) => {
  // Server rejected an in-game action (e.g. played wrong suit, not your turn)
  showError(data.msg);
});

socket.on('public_rooms_update', (data) => {
  renderPublicRooms(data.rooms || []);
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

// ── Browse public rooms ────────────────────────────────────────────────────
$('btn-browse-rooms').addEventListener('click', () => {
  $('public-rooms-modal').classList.remove('hidden');
  socket.emit('get_public_rooms', {});
});

$('btn-close-pub-modal').addEventListener('click', () => {
  $('public-rooms-modal').classList.add('hidden');
});

$('public-rooms-modal').addEventListener('click', (e) => {
  if (e.target === $('public-rooms-modal')) $('public-rooms-modal').classList.add('hidden');
});

function renderPublicRooms(rooms) {
  const list = $('pub-rooms-list');
  if (!rooms.length) {
    list.innerHTML = '<p class="pub-empty-msg">No public rooms open right now. Create one and make it public!</p>';
    return;
  }
  list.innerHTML = '';
  rooms.forEach(r => {
    const card = document.createElement('div');
    card.className = 'pub-room-card';
    card.innerHTML =
      `<div class="pub-room-info">
         <span class="pub-host-name">${escHtml(r.host_name)}'s room</span>
         <span class="pub-player-count">${r.player_count}/6 players</span>
       </div>
       <button class="btn btn-primary btn-sm pub-join-btn" data-code="${escHtml(r.code)}">Join</button>`;
    card.querySelector('.pub-join-btn').addEventListener('click', () => {
      myName = $('player-name').value.trim() || 'Player';
      $('public-rooms-modal').classList.add('hidden');
      $('btn-join-submit').disabled = true;
      joinLoaderStart();
      socket.emit('join_room_req', { name: myName, code: r.code });
    });
    list.appendChild(card);
  });
}

// ── Lobby screen ───────────────────────────────────────────────────────────
$('btn-start-game').addEventListener('click', () => {
  socket.emit('start_game', {});
});

$('btn-make-public').addEventListener('click', () => {
  const isCurrentlyPublic = $('btn-make-public').dataset.public === 'true';
  socket.emit('make_room_public', { public: !isCurrentlyPublic });
});

$('btn-leave-lobby').addEventListener('click', () => {
  myRoomCode = null; mySeat = null;
  socket.disconnect();
  socket.connect();
  showScreen('setup');
});

$('btn-vote-kick-lobby').addEventListener('click', () => {
  socket.emit('vote_kick_host', {});
});

$('btn-vote-kick-game').addEventListener('click', () => {
  socket.emit('vote_kick_host', {});
});

function renderLobby(data) {
  $('lobby-room-code').textContent = data.room_code;
  const list = $('lobby-player-list');
  list.innerHTML = '';
  data.players.forEach(p => {
    const li = document.createElement('li');
    li.textContent = `Seat ${p.seat}: ${escHtml(p.name)}`;
    if (p.is_bot) li.textContent += ' (Bot)';
    if (data.is_host && !p.is_bot && p.seat !== data.my_seat) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-danger btn-sm kick-btn';
      btn.textContent = 'Kick';
      btn.onclick = () => socket.emit('kick_player', { seat: p.seat });
      li.appendChild(btn);
    }
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

  // Public toggle — host only
  const pubBtn = $('btn-make-public');
  if (data.is_host) {
    pubBtn.style.display = 'inline-block';
    pubBtn.dataset.public = data.is_public ? 'true' : 'false';
    if (data.is_public && data.in_waitlist) {
      pubBtn.textContent = 'On Waitlist…';
      pubBtn.classList.add('btn-public-waiting');
      pubBtn.classList.remove('btn-public-on');
    } else if (data.is_public) {
      pubBtn.textContent = 'Listed – Make Private';
      pubBtn.classList.add('btn-public-on');
      pubBtn.classList.remove('btn-public-waiting');
    } else {
      pubBtn.textContent = 'Make Public';
      pubBtn.classList.remove('btn-public-on', 'btn-public-waiting');
    }
  } else {
    pubBtn.style.display = 'none';
  }

  const vkBox = $('lobby-vote-kick');
  if (!data.is_host && (data.vote_kick_needed || 0) > 0) {
    vkBox.classList.remove('hidden');
    $('vk-host-name-lobby').textContent = data.host_name || 'Host';
    $('vk-count-lobby').textContent = data.vote_kick_count || 0;
    $('vk-needed-lobby').textContent = data.vote_kick_needed || 1;
    const btn = $('btn-vote-kick-lobby');
    btn.textContent = data.my_vote_cast ? 'Vote Cast ✓' : 'Vote Kick';
    btn.disabled = !!data.my_vote_cast;
  } else {
    vkBox.classList.add('hidden');
  }
}

// ── Top bar ────────────────────────────────────────────────────────────────
$('btn-rules').addEventListener('click', () => {
  $('rules-modal').classList.remove('hidden');
});
$('btn-close-rules').addEventListener('click', () => {
  $('rules-modal').classList.add('hidden');
});
$('rules-modal').addEventListener('click', (e) => {
  if (e.target === $('rules-modal')) $('rules-modal').classList.add('hidden');
});

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
    selectedP1 = null;
    selectedP2 = null;
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
$('btn-bid-270').addEventListener('click', () => {
  socket.emit('bid_action', { bid_yes: true, jump_to_270: true });
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
  // Build the partner-card picker grids for the set_trump panel.
  // Only shows cards the bidder doesn't hold in their own hand (can't name a card you have).
  // Each picker also excludes the card already selected in the OTHER picker (no duplicates).
  // Clicking a card sets selectedP1 or selectedP2 and re-renders both pickers.
  if (!gs) return;
  const myHand = new Set(gs.hand.map(h => h.card));
  const SUIT_ORDER = ['s','h','d','c'];
  const SUIT_SYM2 = { s:'♠', h:'♥', d:'♦', c:'♣' };
  const SUIT_CLS = { s:'black-suit', h:'red-suit', d:'red-suit', c:'black-suit' };
  const RANKS2 = [['a','A'],['k','K'],['q','Q'],['j','J'],['t','10'],
                  ['9','9'],['8','8'],['7','7'],['6','6'],['5','5'],['4','4'],['3','3']];

  const renderPicker = (pickerId, selVal, excludeVal) => {
    const picker = $(pickerId);
    picker.innerHTML = '';
    for (const suit of SUIT_ORDER) {
      // Only include cards not in bidder's hand and not already chosen in the other picker
      const available = RANKS2.filter(([r]) => {
        const card = suit + r;
        return !myHand.has(card) && card !== excludeVal;
      });
      if (available.length === 0) continue;
      const row = document.createElement('div');
      row.className = 'picker-suit-row';
      const sym = document.createElement('span');
      sym.className = `picker-suit-sym ${SUIT_CLS[suit]}`;
      sym.textContent = SUIT_SYM2[suit];
      row.appendChild(sym);
      for (const [r, rd] of available) {
        const card = suit + r;
        const btn = document.createElement('button');
        btn.className = `picker-card ${SUIT_CLS[suit]}${card === selVal ? ' selected' : ''}`;
        btn.textContent = rd;
        btn.addEventListener('click', () => {
          if (pickerId === 'partner-1-picker') selectedP1 = card;
          else selectedP2 = card;
          populatePartnerSelects(trump);   // re-render both pickers to update selection state
        });
        row.appendChild(btn);
      }
      picker.appendChild(row);
    }
  };

  renderPicker('partner-1-picker', selectedP1, selectedP2);
  renderPicker('partner-2-picker', selectedP2, selectedP1);
}

$('btn-set-trump').addEventListener('click', () => {
  $('trump-error').classList.add('hidden');
  const trump = selectedTrump;
  const p1 = selectedP1;
  const p2 = selectedP2;
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

// ── Game-start popup ────────────────────────────────────────────────────────
function showGameStartModal(s) {
  const SYMI = { s:'♠', h:'♥', d:'♦', c:'♣' };
  const RD = {a:'A',k:'K',q:'Q',j:'J',t:'10',9:'9',8:'8',7:'7',6:'6',5:'5',4:'4',3:'3'};
  const suitCls = su => (su==='h'||su==='d') ? 'gs-red' : 'gs-white';
  const cardHtml = c => `<span class="${suitCls(c[0])}">${SYMI[c[0]]}${RD[c[1]]}</span>`;
  const trumpCls = suitCls(s.trump);
  const body = $('game-start-body');
  body.innerHTML = `
    <div class="gs-trump">Trump: <span class="${trumpCls}">${s.trump_symbol} ${s.trump_name}</span></div>
    <div class="gs-partners">Partner cards: ${cardHtml(s.partner_1)} &amp; ${cardHtml(s.partner_2)}</div>
    <div style="margin-top:8px;color:#aaa;font-size:0.85rem">${escHtml(s.bidder_name)} leads the first trick.</div>
  `;
  const modal = $('game-start-modal');
  modal.classList.remove('hidden');
  const dismiss = () => modal.classList.add('hidden');
  const t = setTimeout(dismiss, 5000);
  modal.dataset.timer = t;
}

$('btn-close-game-start').addEventListener('click', () => {
  const modal = $('game-start-modal');
  clearTimeout(modal.dataset.timer);
  modal.classList.add('hidden');
});

// ── Main render ────────────────────────────────────────────────────────────
function render(state) {
  // Called every time a new game_state arrives from the server.
  // Calls each sub-renderer in order; each one is responsible for one area of the UI.
  // wasSetTrump detects the transition from set_trump → playing to show the startup modal.
  const wasSetTrump = prevPhase === 'set_trump';
  prevPhase = state.phase;
  gs = state;
  renderTopBar(state);           // room code, bid, trump, trick counter in the top bar
  renderOpponents(state);        // opponent seats (names, card-back counts, scores)
  renderTrick(state);            // cards currently on the table
  renderLastTrick(state);        // banner showing who won the last trick
  renderPlayerHand(state);       // the human player's own cards at the bottom
  renderLog(state);              // scrolling game log on the right
  renderPhasePanel(state);       // bidding / set-trump / scoring panels in center
  renderPartnerInfo(state);      // partner card reminder box at the bottom of the log
  renderVoteKick(state);         // vote-kick host box (hidden for the host themselves)
  updateOpponentHighlights(state); // ring/glow on active, bidder, and partner seats
  if (wasSetTrump && state.phase === 'playing') {
    showGameStartModal(state);   // popup showing trump and partner cards at game start
  }
}

function renderTopBar(s) {
  // Show the room code only if there are bot seats (which can be taken by new joiners mid-game)
  const hasBots = s.seat_types && Object.values(s.seat_types).some(v => v === true);
  const rcEl = $('info-room-code');
  if (s.room_code && hasBots) {
    rcEl.innerHTML = `Room: <strong>${escHtml(s.room_code)}</strong>`;
    rcEl.title = 'Share this code — open bot seats can be joined mid-game';
  } else {
    rcEl.innerHTML = '';
  }
  // Show "Current Bid: 170 (Mandatory)" or "Current Bid: 180 (Manav)"
  const bidWho = s.is_mandatory_bid
    ? '<span style="color:#aaa"> (Mandatory)</span>'
    : (s.last_bidder_name ? ` <span style="color:#aaa">(${escHtml(s.last_bidder_name)})</span>` : '');
  $('info-bid').innerHTML = `Current Bid: <strong>${s.bid}</strong>${bidWho}`;
  // Trump suit display (red for hearts/diamonds, light for spades/clubs)
  $('info-trump').innerHTML = s.trump
    ? `Trump: <strong style="color:${SUIT_COLOR[s.trump]==='red'?'#f88':'#eee'}">${s.trump_symbol} ${s.trump_name}</strong>`
    : '';
  // Trick counter only shown during the playing phase
  $('info-trick').innerHTML = s.phase === 'playing'
    ? `Trick: <strong>${s.trick_num}/8</strong>` : '';

  const pEl = $('info-partners');
  // Partner cards shown in top bar once trump is set, until bidding/set_trump
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
  // The server sends opp_display: an ordered list of 5 seat numbers to show in slots 2-6.
  // This rotates clockwise from the viewer's perspective so the player to their left
  // is always in the first opponent slot.
  const oppDisplay = s.opp_display || [2, 3, 4, 5, 6];
  const names = s.player_names || {};
  const scores = s.game_scores || {};
  const counts = s.hand_counts || {};  // how many cards each seat currently holds

  for (let slot = 2; slot <= 6; slot++) {
    const seat = oppDisplay[slot - 2];
    const name = names[seat] || `Seat ${seat}`;
    const score = scores[name] || 0;
    const count = counts[String(seat)] ?? 8;

    const nameEl = $(`opp-name-${slot}`);
    nameEl.textContent = name;
    // Remove any existing kick button before re-rendering (prevents duplicates)
    const existingKick = nameEl.querySelector('.kick-btn');
    if (existingKick) existingKick.remove();
    // Host gets a ✕ kick button next to each human opponent's name
    const isHuman = s.seat_types && s.seat_types[String(seat)] === false;
    if (s.is_host && isHuman) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-danger btn-sm kick-btn';
      btn.textContent = '✕';
      btn.title = `Kick ${name}`;
      btn.onclick = () => socket.emit('kick_player', { seat });
      nameEl.appendChild(btn);
    }
    $(`opp-score-${slot}`).textContent = `${score} pts`;

    // Show up to 5 card-back icons to represent how many cards this opponent holds
    const cardsArea = document.querySelector(`#seat-${slot} .opp-cards`);
    const backs = Array.from({ length: Math.min(count, 5) }, () => '<span class="card-back">🂠</span>').join('');
    cardsArea.innerHTML = backs || '–';

    // Store the actual seat number on the DOM element so updateOpponentHighlights()
    // can look it up later without recalculating the layout
    const seatEl = $(`seat-${slot}`);
    seatEl.dataset.actualSeat = seat;
  }
}

function renderTrick(s) {
  // Clear all 6 trick slots first
  for (let i = 1; i <= 6; i++) $(`trick-slot-${i}`).innerHTML = '';
  if (!s.trick_display || !s.trick_leader) return;

  // Build a map from player seat → display slot (slot 1 = leader, slots 2-6 clockwise)
  const leader = s.trick_leader;
  const orderMap = {};
  for (let i = 0; i < 6; i++) {
    const pnum = ((leader - 1 + i) % 6) + 1;
    orderMap[pnum] = i + 1;
  }

  // Client-side trick winner detection (mirrors server beats() logic) so we can
  // highlight the currently winning card with a glow before the trick fully resolves
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

  // Render each played card in its slot; add 'winning' class to current best card
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

  // Fill empty slots with dashed placeholder cards; highlight the current player's slot
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
  // Client-side mirror of the server's beats() function.
  // Used to highlight the currently winning card in the trick table.
  // Must stay in sync with app.py's beats() logic.
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
  const cards = lt.cards.map(c => {
    const cls = SUIT_COLOR[c.suit] === 'red' ? 'lt-card lt-red' : 'lt-card lt-black';
    return `<span class="${cls}">${escHtml(c.disp)}</span>`;
  }).join(' ');
  banner.innerHTML = `Last trick won by <strong>${escHtml(lt.winner_name)}</strong>: ${cards}`;
}

function renderPlayerHand(s) {
  // Render the human player's hand at the bottom of the screen.
  // Cards are sorted by suit (using getSuitOrder for alternating colour layout)
  // then by rank high-to-low within each suit.
  // During the playing phase and on the player's turn, valid cards get the
  // 'playable' class (hover/click effect); invalid cards get 'not-valid' (greyed out).
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

  // Determine suit display order based on which suits are actually in hand
  const presentSuits = new Set(s.hand.map(item => item.card[0]));
  const suitOrderArr = getSuitOrder(presentSuits);
  const SUIT_ORD = Object.fromEntries(suitOrderArr.map((suit, i) => [suit, i]));
  const RANK_ORD = { a:0,k:1,q:2,j:3,t:4,9:5,8:6,7:7,6:8,5:9,4:10,3:11 };

  const sortedHand = [...s.hand].sort((a, b) => {
    const sd = SUIT_ORD[a.card[0]] - SUIT_ORD[b.card[0]];
    if (sd !== 0) return sd;
    return RANK_ORD[a.card[1]] - RANK_ORD[b.card[1]];  // high rank first within suit
  });

  let lastSuit = null;
  sortedHand.forEach(item => {
    // Insert a visual spacer between suit groups
    if (lastSuit !== null && item.card[0] !== lastSuit) {
      const spacer = document.createElement('div');
      spacer.className = 'card-group-spacer';
      handEl.appendChild(spacer);
    }
    lastSuit = item.card[0];

    const el = document.createElement('div');
    const suit = item.card[0];
    const rank = item.card[1];
    const isBlack3 = item.card === 's3';   // Black Three gets a special gold glow
    el.className = `card ${SUIT_COLOR[suit] === 'red' ? 'red-suit' : 'black-suit'}`;
    if (isBlack3) el.classList.add('black3-card');

    if (isPlayingPhase && isMyTurn) {
      if (item.valid) {
        el.classList.add('playable');
        el.addEventListener('click', onCardClick(item.card));   // emit play_card_action
      } else {
        el.classList.add('not-valid');   // greyed out — must follow a different suit
      }
    }

    el.innerHTML = `<div class="card-rank">${RANK_D[rank]||rank}</div><div class="card-suit">${SUIT_SYM[suit]}</div>`;
    el.title = `${RANK_D[rank]||rank} of ${suit==='s'?'Spades':suit==='h'?'Hearts':suit==='d'?'Diamonds':'Clubs'}` + (isBlack3 ? ' (30 pts!)' : '');
    handEl.appendChild(el);
  });
}

function colorizeCards(text) {
  // Wrap card symbols in coloured spans so they render correctly in the game log.
  // Hearts/Diamonds → red span; Spades/Clubs → white span.
  // Applied to all log lines; on trick lines the parent div's blue color is inherited
  // by plain text but overridden by these explicit span colors on the card symbols.
  return escHtml(text)
    .replace(/(♥|♦)(10|[A-Z0-9])/g, '<span class="log-red">$1$2</span>')
    .replace(/(♠|♣)(10|[A-Z0-9])/g, '<span class="log-white">$1$2</span>');
}

function renderLog(s) {
  // Render the last 20 log lines in reverse (newest at the top).
  // Line types:
  //   separator  — "──── … ────" section dividers (gold/dim styling)
  //   trick-line — "Trick N: ..." lines (blue base text, card symbols keep their own color)
  //   plain      — player actions, bids, passes, etc.
  const logEl = $('game-log');
  logEl.innerHTML = '';
  const lines = [...(s.log || [])].reverse();
  lines.forEach(line => {
    const div = document.createElement('div');
    const isTrick = /^Trick \d+:/.test(line);
    div.className = 'log-line' +
      (line.startsWith('─') ? ' separator' : '') +
      (isTrick ? ' trick-line' : '');
    div.innerHTML = colorizeCards(line);
    logEl.appendChild(div);
  });
  logEl.scrollTop = 0;   // keep newest entries visible at the top
}

function renderPhasePanel(s) {
  // Show exactly one center panel depending on the current phase and whose turn it is.
  // All panels are hidden first, then the correct one is shown.
  $('panel-bidding').classList.add('hidden');
  $('panel-set-trump').classList.add('hidden');
  $('panel-scoring').classList.add('hidden');
  $('panel-waiting').classList.add('hidden');

  // The message-box shows play instructions ("Follow Hearts suit", "Your lead", etc.)
  // but only to the player whose turn it is — others see nothing to avoid confusion.
  const msgEl = $('message-box');
  const playDirected = msg => msg.startsWith('Follow') || msg.startsWith('No cards of led') || msg === 'Your lead.';
  msgEl.textContent = (playDirected(s.message || '') && !s.is_my_play_turn) ? '' : (s.message || '');

  if (s.phase === 'bidding') {
    if (s.is_my_bid_turn) {
      // It's this player's turn to bid — show the bidding panel
      $('panel-bidding').classList.remove('hidden');
      // Update the "Current bid: X (who)" line at the top of the panel
      const bidWhoPanel = s.is_mandatory_bid
        ? ' <span style="color:#aaa;font-weight:400">(Mandatory)</span>'
        : (s.last_bidder_name ? ` <span style="color:#aaa;font-weight:400">(${escHtml(s.last_bidder_name)})</span>` : '');
      document.querySelector('.bid-current').innerHTML =
        `Current bid: <strong id="bid-amount">${s.bid}</strong>${bidWhoPanel}`;

      if (s.mandatory_opening) {
        // Special UI for the mandatory bid holder at the start of bidding:
        // hide the normal "Bid X" button; show "Open at 170" and "Bid 270!" buttons
        document.querySelector('.bid-next').innerHTML =
          'Open at <strong>170</strong> (others can bid), or go <strong>270</strong> now?';
        $('btn-bid-yes').style.display = 'none';
        $('btn-bid-no').textContent = 'Open at 170';
        $('btn-bid-270').style.display = '';
      } else {
        // Normal bid turn: show "Bid X", "Pass", and "Bid 270!" (if 270 isn't reached yet)
        const nextBid = s.bid + 10;
        document.querySelector('.bid-next').innerHTML =
          `Bid <strong id="bid-next-amount">${nextBid}</strong> or pass?`;
        $('btn-bid-yes').style.display = '';
        $('btn-bid-yes').disabled = s.bid >= 270;  // can't bid above 270
        $('bid-yes-label').textContent = nextBid;
        $('btn-bid-no').textContent = 'Pass';
        // Hide the "Bid 270!" jump button once the bid is already 260 (next step is 270 anyway)
        $('btn-bid-270').style.display = s.bid < 260 ? '' : 'none';
      }
    } else {
      // Not this player's turn — show a spinner with whose turn it is
      $('panel-waiting').classList.remove('hidden');
      const bName = s.bidding_seat ? (s.player_names[s.bidding_seat] || '...') : '...';
      $('waiting-msg').textContent = `Waiting for ${bName} to bid…`;
    }

  } else if (s.phase === 'set_trump') {
    if (s.is_my_trump_turn) {
      // The bidder picks trump and partner cards
      $('panel-set-trump').classList.remove('hidden');
      if (!selectedTrump) {
        // Reset selections if trump wasn't chosen yet (e.g. on page reconnect)
        document.querySelectorAll('.suit-btn').forEach(b => b.classList.remove('selected'));
        selectedP1 = null;
        selectedP2 = null;
      }
      populatePartnerSelects(selectedTrump);   // re-render the card picker grids
    } else {
      $('panel-waiting').classList.remove('hidden');
      $('waiting-msg').textContent = `Waiting for ${escHtml(s.bidder_name || '...')} to set trump…`;
    }

  } else if (s.phase === 'scoring') {
    // Round is over — show the scoring breakdown
    $('panel-scoring').classList.remove('hidden');
    renderScoringPanel(s);
  }
}

function renderScoringPanel(s) {
  // Fills the end-of-round scoring panel with:
  //   • A green/red headline (win/lose)
  //   • Bid, team points, and bidder/partner names
  //   • A gold "+1000 pts!" badge for a 270 win
  //   • Cumulative game scores table sorted high-to-low; viewer's row is highlighted
  const rr = s.round_result;
  if (!rr) return;

  const headline = $('score-headline');
  if (rr.bidder_won) {
    headline.textContent = rr.bid === 270
      ? `${rr.bidder_name}'s team wins — MAXIMUM BID!`
      : `${rr.bidder_name}'s team wins!`;
    headline.style.color = '#6eff6e';
  } else {
    headline.textContent = `${rr.bidder_name}'s team fails!`;
    headline.style.color = '#ff7070';
  }

  $('score-details').innerHTML = `
    Bid: <strong>${rr.bid}</strong> &nbsp;|&nbsp;
    Team collected: <strong>${rr.team_pts}</strong> pts<br>
    Bidder: <strong>${escHtml(rr.bidder_name)}</strong>
    ${rr.bid === 270 && rr.bidder_won ? '<span style="color:var(--gold);font-weight:700"> (+1000 pts!)</span>' : ''}<br>
    Partners: <strong>${escHtml(rr.partner_1_name)}</strong> &amp; <strong>${escHtml(rr.partner_2_name)}</strong>
  `;

  // Build the cumulative scores table, sorted descending; highlight the viewer's own row
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
    const p1c = (s.partner_1[0]==='h'||s.partner_1[0]==='d') ? '#f77' : '#ddd';
    const p2c = (s.partner_2[0]==='h'||s.partner_2[0]==='d') ? '#f77' : '#ddd';
    box.innerHTML = `<div>Partners: <strong style="color:${p1c}">${escHtml(p1d)}</strong> (${p1n}) &amp; <strong style="color:${p2c}">${escHtml(p2d)}</strong> (${p2n})</div>`;
  } else {
    box.classList.add('hidden');
  }
}

function renderVoteKick(s) {
  const box = $('game-vote-kick');
  if (!s.is_host && (s.vote_kick_needed || 0) > 0) {
    box.classList.remove('hidden');
    $('vk-host-name-game').textContent = s.host_name || 'Host';
    $('vk-count-game').textContent = s.vote_kick_count || 0;
    $('vk-needed-game').textContent = s.vote_kick_needed || 1;
    const btn = $('btn-vote-kick-game');
    btn.textContent = s.my_vote_cast ? 'Vote Cast ✓' : 'Vote Kick';
    btn.disabled = !!s.my_vote_cast;
  } else {
    box.classList.add('hidden');
  }
}

function updateOpponentHighlights(s) {
  // Add CSS classes to opponent seat elements to visually highlight:
  //   active-turn  — it's this opponent's turn to play/bid (yellow ring)
  //   bidder-seat  — this player won the auction (gold border)
  //   partner-seat — this player holds a partner card (blue tint, once revealed)
  // Uses dataset.actualSeat set by renderOpponents() to map slot → real seat number
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
// Returns the suit display order based on which suits are present in hand.
// All 4 suits: ♠♥♦♣ (B-R-R-B). Missing a black → R-B-R. Missing a red → B-R-B.
function getSuitOrder(present) {
  const reds = ['h', 'd'].filter(s => present.has(s));
  const blacks = ['s', 'c'].filter(s => present.has(s));
  if (reds.length === 2 && blacks.length === 2) return ['s', 'h', 'd', 'c'];
  const order = [];
  if (reds.length >= blacks.length) {
    // R-B-R: interleave reds around blacks
    if (reds[0]) order.push(reds[0]);
    if (blacks[0]) order.push(blacks[0]);
    if (reds[1]) order.push(reds[1]);
  } else {
    // B-R-B: interleave blacks around reds
    if (blacks[0]) order.push(blacks[0]);
    if (reds[0]) order.push(reds[0]);
    if (blacks[1]) order.push(blacks[1]);
  }
  return order;
}

function escHtml(str) {
  // Escape HTML special characters before inserting user-supplied strings into innerHTML.
  // Always use this for player names, card displays, or any server-provided text.
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
