# ═══════════════════════════════════════════════════════════════════════════════
# Three of Spades (Kali Teeri) — Flask + SocketIO Multiplayer Card Game
#
# Architecture overview:
#   • Flask serves the single HTML page at "/"
#   • SocketIO handles all real-time game events (bidding, card plays, etc.)
#   • All game state lives in the "rooms" dict (in-memory, resets on redeploy)
#   • Each room has up to 6 seats; empty seats are filled with bots
#   • The game phases cycle: bidding → set_trump → playing → scoring → (repeat)
#
# Key data structures:
#   rooms[code]  — dict with 'seats', 'host_sid', 'status', 'gs' (game state)
#   sid_room[sid] — maps each socket ID to its room code (for quick lookup)
#   gs (game state) — the full per-round state dict created by create_game_mp()
# ═══════════════════════════════════════════════════════════════════════════════

from flask import Flask, render_template, request, jsonify, make_response
from flask_socketio import SocketIO, emit, join_room as sio_join
import os, random, string, uuid, time

app = Flask(__name__)
# Secret key used to sign session cookies (override via environment variable in production)
app.secret_key = os.environ.get('SECRET_KEY', 'black3_secret_2024_kala_teen')
# gevent async mode is required for socketio.sleep() calls inside game loops
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*', manage_session=False)

# ── Global server state ────────────────────────────────────────────────────────
rooms = {}            # room_code (str) → room dict
sid_room = {}         # socket_id (str) → room_code — lets us find the room on any event
connected_sockets = 0   # count of currently open WebSocket connections

# ── Public rooms ───────────────────────────────────────────────────────────────
# Up to 10 lobby rooms are publicly browseable. Hosts opt-in per room.
# Rooms that exceed the 10-slot cap are queued in public_waitlist (FIFO).
public_rooms_list = []   # room codes currently in the visible public list (max 10)
public_waitlist   = []   # room codes waiting for a slot to open up

# ── Redis (unique device tracking) ────────────────────────────────────────────
# All Redis setup is lazy — nothing runs at import time so startup is never blocked.
# The client is created on first use; if Redis is unavailable for any reason the
# helpers return None/no-op and the app continues normally.
DEVICES_KEY = 'spade3:unique_devices'
_redis_client = None   # populated on first call to _get_redis()

def _get_redis():
    """Return a Redis client, creating it lazily on first call. Returns None on any error."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.environ.get('REDIS_URL') or os.environ.get('REDIS_PRIVATE_URL')
    if not url:
        return None
    try:
        import redis as _redis_module
        _redis_client = _redis_module.from_url(url)
    except Exception:
        pass
    return _redis_client

def redis_add_device(device_id):
    """Add a device UUID to the persistent Redis set. No-op if Redis is unavailable."""
    r = _get_redis()
    if r:
        try:
            r.sadd(DEVICES_KEY, device_id)
        except Exception:
            pass

def redis_device_count():
    """Return the number of unique devices stored in Redis, or None if unavailable."""
    r = _get_redis()
    if r:
        try:
            return r.scard(DEVICES_KEY)
        except Exception:
            pass
    return None

# ── Historical counters (reset on redeploy, tracked in-memory) ───────────────
total_rooms_created      = 0   # every time a room is created via create_room_req
total_rounds_played      = 0   # every time a round starts (start_game + new_round_action)
total_multiplayer_rooms  = 0   # rooms that have ever had 2+ humans (counted once per room)

# ─── Card Constants ───────────────────────────────────────────────────────────
# Cards are stored as 2-character strings: suit letter + rank letter
# e.g. 'sa' = Ace of Spades, 'h3' = Three of Hearts, 's3' = the special Black Three
SUITS = ['s', 'h', 'd', 'c']   # spades, hearts, diamonds, clubs
RANKS = ['a', 'k', 'q', 'j', 't', '9', '8', '7', '6', '5', '4', '3']  # high → low
# RANK_ORDER: lower number = stronger card (ace=1 is strongest, 3=12 is weakest)
RANK_ORDER = {r: i + 1 for i, r in enumerate(RANKS)}
SUIT_NAMES   = {'s': 'Spades', 'h': 'Hearts', 'd': 'Diamonds', 'c': 'Clubs'}
SUIT_SYMBOLS = {'s': '♠', 'h': '♥', 'd': '♦', 'c': '♣'}
RANK_DISPLAY = {
    'a': 'A', 'k': 'K', 'q': 'Q', 'j': 'J', 't': '10',
    '9': '9', '8': '8', '7': '7', '6': '6', '5': '5', '4': '4', '3': '3'
}


# ─── Card Helpers ─────────────────────────────────────────────────────────────
def card_sort_key(card):
    """Sort key for dealing/displaying cards: group by suit, then high-to-low rank."""
    return (SUITS.index(card[0]), RANK_ORDER[card[1]])


def card_points(card):
    """Return the point value of a card. Black Three = 30, Ace = 20, K/Q/J/10 = 10, rest = 0."""
    if card == 's3': return 30
    if card[1] == 'a': return 20
    if card[1] in 'kqjt': return 10
    return 0


def beats(challenger, champion, trump, first_suit):
    """
    Return True if 'challenger' beats the current 'champion' card.
    Priority rules:
      1. Trump beats any non-trump.
      2. Between two trumps, higher rank wins.
      3. If neither is trump, a card of the led suit beats an off-suit card.
      4. Between two led-suit cards, higher rank wins.
      5. Off-suit non-trump never beats anything.
    """
    if champion is None: return True   # first card always "beats" nothing
    cT = challenger[0] == trump
    pT = champion[0] == trump
    if cT and not pT: return True       # trump beats non-trump
    if not cT and pT: return False      # non-trump loses to trump
    if cT and pT: return RANK_ORDER[challenger[1]] < RANK_ORDER[champion[1]]  # both trump: higher wins
    if challenger[0] == first_suit and champion[0] != first_suit: return True  # on-suit beats off-suit
    if challenger[0] == first_suit and champion[0] == first_suit:
        return RANK_ORDER[challenger[1]] < RANK_ORDER[champion[1]]  # both on-suit: higher wins
    return False  # off-suit non-trump can never win


def trick_winner_idx(cards, trump, first_suit):
    """Return the index (in the cards list) of the winning card for a completed trick."""
    best = 0
    for i in range(1, len(cards)):
        if beats(cards[i], cards[best], trump, first_suit):
            best = i
    return best


def valid_cards(hand, first_suit):
    """
    Return the subset of hand cards that are legal to play.
    If the player has any card in the led suit, they must play one of those.
    If they have none, they can play anything (including trump or discards).
    """
    if first_suit is None: return list(hand)   # leading: any card is fine
    same = [c for c in hand if c[0] == first_suit]
    return same if same else list(hand)


def get_trick_play_order(leader):
    """
    Return the list of seat numbers in clockwise play order starting from 'leader'.
    Seats are numbered 1-6; wraps around using modulo.
    """
    return [(leader - 1 + i) % 6 + 1 for i in range(6)]


# ─── AI Logic ─────────────────────────────────────────────────────────────────
def ai_max_bid(hand):
    """
    Estimate the maximum bid a bot should make given its hand.
    Starts at a base of 60 (floor for a weak hand) and adds:
      • 45 pts per Ace, 35 per King, 25 per Queen
      • Length bonus if the best suit has 4+ cards (suits that suit as trump)
      • Extra 20 if holding the Black Three in the trump suit candidate
    Returns an integer — the bot will bid up to this value.
    """
    points = 60
    suit_count = {s: 0 for s in SUITS}
    has_black3 = False
    for card in hand:
        r, s = card[1], card[0]
        if r == 'a': points += 45
        elif r == 'k': points += 35
        elif r == 'q': points += 25
        suit_count[s] += 1
        if card == 's3': has_black3 = True
    best = max(suit_count, key=suit_count.get)
    if suit_count[best] >= 4:
        points += suit_count[best] * 10
        if best == 's' and has_black3: points += 20
    return points


def ai_choose_trump(hand):
    """
    Pick the best trump suit for a bot: the suit with the most cards,
    with a tiebreaker bonus for holding the ace (+0.5) or king (+0.25) in that suit.
    """
    suit_score = {}
    for s in SUITS:
        cnt = sum(1 for c in hand if c[0] == s)
        bonus = sum(0.5 if c[1] == 'a' else 0.25 if c[1] == 'k' else 0
                    for c in hand if c[0] == s)
        suit_score[s] = cnt + bonus
    return max(suit_score, key=suit_score.get)


def ai_choose_partners(hand, trump):
    """
    Pick two partner cards the bot doesn't hold itself.
    Priority: trump A/K first (partners who hold key trump), then off-suit aces,
    then off-suit kings, then face cards, then anything — guaranteeing 2 unique cards.
    """
    has = set(hand)
    priority = []
    for r in ['a', 'k']:
        if trump + r not in has: priority.append(trump + r)
    for s in SUITS:
        if s != trump and s + 'a' not in has: priority.append(s + 'a')
    for s in SUITS:
        if s != trump and s + 'k' not in has: priority.append(s + 'k')
    for r in ['q', 'j', 't']:
        for s in SUITS:
            if s + r not in has: priority.append(s + r)
    for s in SUITS:
        for r in RANKS:
            c = s + r
            if c not in has: priority.append(c)
    chosen = []
    for c in priority:
        if c not in chosen: chosen.append(c)
        if len(chosen) == 2: break
    return chosen[0], chosen[1]


# ─── AI Helpers ───────────────────────────────────────────────────────────────
def _low(cards):
    """Lowest-ranked card (highest RANK_ORDER value = worst rank)."""
    return max(cards, key=lambda c: RANK_ORDER[c[1]])


def _high(cards):
    """Highest-ranked card (lowest RANK_ORDER value = best rank)."""
    return min(cards, key=lambda c: RANK_ORDER[c[1]])


def _ace_gone(all_played, suit):
    """Return True if the ace of this suit has already been played in a previous trick."""
    return (suit + 'a') in all_played


def _trumps_gone(all_played, trump):
    """Count how many trump cards have already been played across all previous tricks."""
    return sum(1 for c in all_played if c[0] == trump)


def _throwaway(hand, trump):
    """Safest discard: zero-point non-trump > any non-trump > lowest trump."""
    non_trump = [c for c in hand if c[0] != trump]
    pool = non_trump if non_trump else list(hand)
    zero = [c for c in pool if card_points(c) == 0]
    return _low(zero if zero else pool)


def _best_point_card(hand, all_played, trump):
    """Best safe lead: non-trump ace, then king if ace of that suit is gone."""
    for s in SUITS:
        if s != trump and (s + 'a') in hand:
            return s + 'a'
    for s in SUITS:
        if s != trump and (s + 'k') in hand and _ace_gone(all_played, s):
            return s + 'k'
    return None


def ai_play_card(player_num, hand, played_order, played_cards, trump,
                 bidder_num, p1_num, p2_num, first_suit, trick_num,
                 all_played, partner_1_card, partner_2_card):
    """
    Main AI entry point — picks which card a bot should play.
    Determines the bot's role (bidder / partner / opponent), then delegates
    to either _ai_lead (bot is first to play this trick) or
    _ai_follow (other cards already on the table).
    """
    is_bidder = player_num == bidder_num
    is_partner = player_num in (p1_num, p2_num)
    is_bidder_team = is_bidder or is_partner
    trump_in_hand = [c for c in hand if c[0] == trump]

    if first_suit is None:
        # No card has been led yet — this bot leads the trick
        return _ai_lead(player_num, hand, trump, trump_in_hand, is_bidder,
                        is_partner, p1_num, trick_num,
                        all_played, partner_1_card, partner_2_card)
    # Cards are already on the table — bot must follow or trump
    return _ai_follow(player_num, hand, played_order, played_cards, trump,
                      first_suit, trump_in_hand, is_bidder, is_partner,
                      is_bidder_team, bidder_num, p1_num, p2_num, trick_num,
                      partner_1_card, partner_2_card)


def _ai_lead(player_num, hand, trump, trump_in_hand, is_bidder, is_partner,
             p1_num, trick_num, all_played, p1c, p2c):
    """Route the lead decision to the correct role-specific strategy."""
    if is_bidder:
        return _bidder_lead(hand, trump, trump_in_hand, trick_num, all_played, p1c, p2c)
    if is_partner:
        # Pass the *other* partner card so this partner can try to draw it out
        other_pc = p2c if player_num == p1_num else p1c
        return _partner_lead(hand, trump, trump_in_hand, trick_num, all_played, other_pc)
    return _opponent_lead(hand, trump, all_played)


def _bidder_lead(hand, trump, trump_in_hand, trick_num, all_played, p1c, p2c):
    """
    Lead strategy for the bidder (the player who won the auction).
    Tricks 1-3: lead trump to "clear" it — draw out opponents' trumps so the
    bidder's team can win later tricks safely.
    Tricks 4+: shift to maximising points (lead aces/kings of non-trump suits).
    """
    has_ta = (trump + 'a') in hand   # does bidder hold the trump ace?
    has_tk = (trump + 'k') in hand   # does bidder hold the trump king?

    if trump_in_hand:
        if trick_num == 1:
            if has_ta and has_tk:
                return trump + 'a'
            if not has_ta:
                # Partner holds trump ace — lead a sub-ace/king trump to draw it
                lower = [c for c in trump_in_hand if c[1] not in ('a', 'k')]
                return _low(lower) if lower else _low([c for c in trump_in_hand if c[1] != 'a'])
            # Has ace but not king — lead low trump to draw partner's king
            lower = [c for c in trump_in_hand if c[1] not in ('a', 'k')]
            return _low(lower) if lower else _low([c for c in trump_in_hand if c[1] != 'k'])

        if trick_num == 2:
            if has_tk:
                return trump + 'k'
            if has_ta:
                return trump + 'a'
            return _high(trump_in_hand)

        if trick_num == 3:
            gone = _trumps_gone(all_played, trump)
            if gone + len(trump_in_hand) < 12:
                return _high(trump_in_hand)
            # All trump accounted for — fall through to point play

    # Tricks 4+: draw any remaining trump before going for points.
    # If the bidder has regained the lead and opponents still hold trump, clear it now
    # so later point-tricks can't be ruffed away.
    if trump_in_hand:
        gone = _trumps_gone(all_played, trump)
        if gone + len(trump_in_hand) < 12:   # some trump still unaccounted for
            return _high(trump_in_hand)

    # Point maximisation — lead aces/kings of non-trump suits
    high = _best_point_card(hand, all_played, trump)
    if high:
        return high

    # Lead low of a partner-card suit to signal partner to play their card
    for pc in (p1c, p2c):
        if pc:
            suit_low = [c for c in hand if c[0] == pc[0] and c[0] != trump and c != pc]
            if suit_low:
                return _low(suit_low)

    if trick_num >= 7 and trump_in_hand:
        return _high(trump_in_hand)

    return _throwaway(hand, trump)


def _partner_lead(hand, trump, trump_in_hand, trick_num, all_played, other_pc):
    # Tricks 1-3: keep clearing trump
    if trick_num <= 3 and trump_in_hand:
        if trick_num == 2:
            # Lead highest non-ace trump to set up bidder's king/ace
            non_ace = [c for c in trump_in_hand if c[1] != 'a']
            return _high(non_ace) if non_ace else _high(trump_in_hand)
        return _high(trump_in_hand)

    # No trump or tricks 4+: point play, then draw other partner's card
    high = _best_point_card(hand, all_played, trump)
    if high:
        return high
    if other_pc:
        suit_low = [c for c in hand if c[0] == other_pc[0] and c[0] != trump and c != other_pc]
        if suit_low:
            return _low(suit_low)
    return _throwaway(hand, trump)


def _opponent_lead(hand, trump, all_played):
    """
    Lead strategy for opponents (trying to score points against the bidder's team).
    Lead aces/kings whenever safe, otherwise throw away the lowest-value card.
    """
    high = _best_point_card(hand, all_played, trump)
    if high:
        return high
    return _throwaway(hand, trump)


def _ai_follow(player_num, hand, played_order, played_cards, trump,
               first_suit, trump_in_hand, is_bidder, is_partner,
               is_bidder_team, bidder_num, p1_num, p2_num, trick_num,
               p1c, p2c):
    """
    Following strategy — called when at least one card is already on the table.
    Key decisions:
      • If following in a trump trick (tricks 1-3): use coordinated plays to
        efficiently clear the trump suit.
      • Otherwise: follow suit if possible; if winning is useful, try to win with
        the lowest card that still beats the current best; if not useful, discard cheap.
    """
    my_valid = valid_cards(hand, first_suit)
    same_suit = [c for c in my_valid if c[0] == first_suit]  # cards matching led suit

    # Who is currently winning the trick and with what card?
    win_idx = trick_winner_idx(played_cards, trump, first_suit)
    cur_winner = played_order[win_idx]
    cur_best = played_cards[win_idx]
    winner_is_team = cur_winner in (bidder_num, p1_num, p2_num)  # is a teammate winning?

    # ── Trump clearing tricks 1-3 ─────────────────────────────────────────────
    if first_suit == trump and trick_num <= 3:
        if same_suit:
            # Bidder following: play king then ace to win the trick
            if is_bidder and trick_num >= 2:
                if (trump + 'k') in hand:
                    return trump + 'k'
                if (trump + 'a') in hand:
                    return trump + 'a'

            # Partner following: play partner card at the right moment
            if is_partner:
                my_pc = p1c if player_num == p1_num else p2c
                if my_pc and my_pc in hand and my_pc[0] == trump:
                    bidder_has_ta = (trump + 'a') not in (p1c, p2c)
                    bidder_has_tk = (trump + 'k') not in (p1c, p2c)
                    if my_pc == trump + 'a' and not bidder_has_ta:
                        # Play ace if bidder has king (case 2), or trick 2+ (case 4)
                        if bidder_has_tk or trick_num >= 2:
                            return trump + 'a'
                    if my_pc == trump + 'k' and not bidder_has_tk:
                        return trump + 'k'   # cases 3 & 4: draw king now

            return _low(same_suit)
        else:
            # Can't follow trump — ruff only if team isn't winning
            if is_bidder_team and not winner_is_team:
                can_beat = [c for c in trump_in_hand if beats(c, cur_best, trump, first_suit)]
                if can_beat:
                    return _low(can_beat)
            return _throwaway(hand, trump)

    # ── All other tricks (non-trump lead, or tricks 4+) ───────────────────────
    pts_in_trick = sum(card_points(c) for c in played_cards)

    if same_suit:
        # Player can follow suit
        if is_bidder_team:
            # Partner: if the bidder led this trick in the same suit as our partner card,
            # play the partner card — this is the bidder "calling" for us to reveal.
            if is_partner:
                my_pc = p1c if player_num == p1_num else p2c
                bidder_led = played_order and played_order[0] == bidder_num
                if my_pc and my_pc in same_suit and bidder_led:
                    return my_pc   # reveal partner card on bidder's call

            if winner_is_team and cur_winner != player_num:
                # A teammate is already winning — play low to avoid wasting a good card
                return _low(same_suit)
            can_beat = [c for c in same_suit if beats(c, cur_best, trump, first_suit)]
            if can_beat:
                return _low(can_beat)   # win with the cheapest winning card
            return _low(same_suit)      # can't win — play lowest to minimise loss
        else:
            # Opponent following suit
            if not winner_is_team:
                return _high(same_suit)   # opponent is winning — play highest to keep winning
            can_beat = [c for c in same_suit if beats(c, cur_best, trump, first_suit)]
            if can_beat:
                return _low(can_beat)     # steal the trick from the bidder's team
            return _low(same_suit)        # can't steal — play low
    else:
        # Player can't follow suit — can trump or discard
        if is_bidder_team:
            if not winner_is_team:
                # Teammate is losing — try to ruff (trump) to save the trick
                can_beat = [c for c in trump_in_hand if beats(c, cur_best, trump, first_suit)]
                if can_beat:
                    return _low(can_beat)
            return _throwaway(hand, trump)   # teammate already winning — just discard
        else:
            # Opponent: only worth ruffing if the trick has lots of points AND bidder's team is taking it
            if winner_is_team and pts_in_trick >= 20:
                can_beat = [c for c in trump_in_hand if beats(c, cur_best, trump, first_suit)]
                if can_beat:
                    return _low(can_beat)
            return _throwaway(hand, trump)


# ─── Room Management ──────────────────────────────────────────────────────────
def generate_room_code():
    """Generate a unique 4-letter uppercase room code (e.g. 'ABCD')."""
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:   # keep trying until we get one that isn't taken
            return code


def get_human_seats(room):
    """Return the set of seat numbers that are occupied by real (non-bot) players."""
    return {seat for seat, p in room['seats'].items() if not p['is_bot']}


# ── Public rooms helpers ───────────────────────────────────────────────────────

def _public_room_info(code):
    """Return a display dict for a public room, or None if it shouldn't be shown."""
    r = rooms.get(code)
    if not r or r['status'] != 'lobby':
        return None
    human_count = sum(
        1 for p in r['seats'].values()
        if not p['is_bot'] and not p.get('is_disconnected')
    )
    if human_count >= 6:
        return None
    # Resolve host name — host may be temporarily disconnected, so sid_to_seat won't
    # have their SID. Fall back to scanning seats for _disconnected_sid match.
    host_sid = r.get('host_sid')
    host_seat = r['sid_to_seat'].get(host_sid) if host_sid else None
    if host_seat is None and host_sid:
        for s, p in r['seats'].items():
            if p.get('_disconnected_sid') == host_sid:
                host_seat = s
                break
    host_name = r['seats'].get(host_seat, {}).get('name', 'Unknown') if host_seat else 'Unknown'
    return {'code': code, 'player_count': human_count, 'host_name': host_name}


def _broadcast_public_rooms():
    """Push the current public rooms list to every connected client."""
    visible = [info for info in (_public_room_info(c) for c in list(public_rooms_list)) if info]
    socketio.emit('public_rooms_update', {'rooms': visible})


def _add_to_public(code):
    """Add a room to the public list (or waitlist if full). No-op if already tracked."""
    if code in public_rooms_list or code in public_waitlist:
        return
    # Fill any gaps caused by stale deletions before checking capacity
    valid_count = sum(1 for c in public_rooms_list if c in rooms)
    if valid_count < 10:
        public_rooms_list.append(code)
        _broadcast_public_rooms()
    else:
        public_waitlist.append(code)


def _remove_from_public(code):
    """Remove a room from the public list and promote the next waitlisted room."""
    removed = False
    if code in public_rooms_list:
        public_rooms_list.remove(code)
        removed = True
    if code in public_waitlist:
        public_waitlist.remove(code)
    if removed:
        # Promote from waitlist if a slot opened
        while public_waitlist:
            candidate = public_waitlist.pop(0)
            if candidate in rooms and rooms[candidate]['status'] == 'lobby':
                public_rooms_list.append(candidate)
                break
        _broadcast_public_rooms()


def _connected_human_count(code):
    """Return number of connected (non-disconnected) human players in a lobby room."""
    r = rooms.get(code)
    if not r:
        return 0
    return sum(1 for p in r['seats'].values() if not p['is_bot'] and not p.get('is_disconnected'))


def _maybe_evict_empty_public_room():
    """
    If a 0-person public room is taking a slot that a people-having room needs,
    evict one randomly-chosen 0-person room and promote the first waitlist room
    that actually has people.

    Only evicts when the total number of rooms (in list + waitlist) that have
    connected players exceeds 10, meaning the 10-slot cap is the bottleneck.
    """
    # Count all rooms (in list OR waitlist) that have connected humans
    rooms_with_people = [
        c for c in (list(public_rooms_list) + list(public_waitlist))
        if c in rooms and rooms[c]['status'] == 'lobby' and _connected_human_count(c) > 0
    ]
    if len(rooms_with_people) <= 10:
        return  # no pressure — 0-person rooms can stay

    # Check there's actually a waitlist room with people to promote
    waitlist_with_people = [
        c for c in public_waitlist
        if c in rooms and rooms[c]['status'] == 'lobby' and _connected_human_count(c) > 0
    ]
    if not waitlist_with_people:
        return

    # Find 0-person rooms currently in the public list
    empty_in_list = [c for c in public_rooms_list if _connected_human_count(c) == 0]
    if not empty_in_list:
        return

    # Evict one at random
    victim = random.choice(empty_in_list)
    public_rooms_list.remove(victim)

    # Promote the first waitlist room that has people
    promoted = waitlist_with_people[0]
    public_waitlist.remove(promoted)
    public_rooms_list.append(promoted)
    _broadcast_public_rooms()


def broadcast_lobby(room_code):
    """
    Send a 'lobby_update' event to every human player currently in this room's lobby.
    Each player gets a personalised payload: is_host, my_seat, vote-kick tally, etc.
    Called whenever the lobby roster changes (join, leave, kick).
    """
    room = rooms[room_code]
    # Build the player list visible to everyone (sorted by seat number)
    players_list = [
        {'seat': s, 'name': room['seats'][s]['name'], 'is_bot': room['seats'][s]['is_bot']}
        for s in sorted(room['seats'])
        if not room['seats'][s].get('is_disconnected')
    ]
    host_seat = room['sid_to_seat'].get(room['host_sid'])
    host_name = room['seats'].get(host_seat, {}).get('name', 'Host')
    # Vote-kick: majority of non-host humans needed to remove the host
    non_host_humans = [s for s, p in room['seats'].items()
                       if not p['is_bot'] and not p.get('is_disconnected') and s != host_seat]
    vote_needed = len(non_host_humans) // 2 + 1
    vote_count = len(room.get('vote_kick_votes', set()))
    is_public   = room.get('is_public', False)
    in_waitlist = room_code in public_waitlist
    for seat, p in room['seats'].items():
        if not p['is_bot'] and p.get('sid') and not p.get('is_disconnected'):
            socketio.emit('lobby_update', {
                'room_code': room_code,
                'players': players_list,
                'is_host': p['sid'] == room['host_sid'],
                'my_seat': seat,
                'vote_kick_count': vote_count,
                'vote_kick_needed': vote_needed,
                'my_vote_cast': seat in room.get('vote_kick_votes', set()),
                'host_name': host_name,
                'is_public': is_public,
                'in_waitlist': in_waitlist,
            }, to=p['sid'])
    # Push updated public rooms list to all clients whenever a public room changes
    if is_public:
        _broadcast_public_rooms()


def broadcast_game_state(room_code):
    """
    Send each human player their own personalised 'game_state' snapshot.
    Each player sees different data (e.g. only they see their own hand, partner
    secrecy is filtered per-viewer). Bots are skipped — they don't have sockets.
    """
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs:
        return
    for seat, p in room['seats'].items():
        if not p['is_bot'] and p.get('sid'):
            socketio.emit('game_state', client_state_mp(gs, seat, room), to=p['sid'])


# ─── Game State ───────────────────────────────────────────────────────────────
def create_game_mp(names_dict, game_scores=None, start_seat=1):
    """
    Create a fresh game-state dict for a new round.
    - Shuffles and deals 8 cards to each of the 6 seats.
    - bid_order is everyone except start_seat (they all bid first); start_seat
      holds the mandatory 170 and is processed last.
    - game_scores carries over from the previous round if supplied.
    - opening_done starts False and is flipped on the very first call to
      _process_bidding_mp, so we only show the mandatory-opening choice once.
    """
    deck = [s + r for s in SUITS for r in RANKS]
    random.shuffle(deck)
    # Deal 8 cards to each seat, sorted for display
    hands = {i + 1: sorted(deck[i * 8:(i + 1) * 8], key=card_sort_key) for i in range(6)}
    # bid_order: seats 2-6 relative to start_seat bid first; start_seat bids last (mandatory)
    bid_order = [(start_seat - 1 + i) % 6 + 1 for i in range(1, 7)]
    gs = {
        'phase': 'bidding',         # current game phase
        'player_names': names_dict, # seat_num -> display name
        'hands': hands,             # seat_num -> list of card strings
        'bid': 170,                 # current highest bid (starts at mandatory 170)
        'last_bidder': start_seat,  # who last raised the bid (starts as mandatory holder)
        'start_seat': start_seat,   # seat that holds the mandatory 170 this round
        'passed': {i: False for i in range(1, 7)},  # who has passed already
        'pass_count': 0,            # how many players have passed (bidding ends at 5)
        'bid_pos': 0,               # position in bid_order we're currently processing
        'bid_order': bid_order,     # order in which seats are asked to bid
        'bidding_seat': None,       # seat currently waiting for a human bid (None = bot/auto)
        'bidder': None,             # seat that won the auction (set in _finish_bidding_mp)
        'trump': None,              # trump suit letter chosen by bidder
        'partner_1': None,          # first partner card string (e.g. 'ha' = ♥A)
        'partner_2': None,          # second partner card string
        'partner_1_player': None,   # seat that holds partner_1 (revealed at game start)
        'partner_2_player': None,   # seat that holds partner_2
        'trick_num': 1,             # current trick number (1-8)
        'trick_leader': None,       # seat that leads the current trick
        'trick_play_order': [],     # clockwise play order for the current trick
        'trick_cards': [],          # cards played so far in the current trick
        'trick_played_by': [],      # which seat played each card in trick_cards
        'first_suit': None,         # suit of the first card played this trick (led suit)
        'tricks_won': {i: [] for i in range(1, 7)},  # seat -> list of cards won
        'last_trick_info': None,    # summary of the just-finished trick (shown as banner)
        'game_scores': game_scores or {i: 0 for i in range(1, 7)},  # cumulative scores
        'round_result': None,       # scoring breakdown (set in _calculate_scores)
        'log': [],                  # list of log message strings shown in the sidebar
        'message': '',              # short status message shown above the trick table
    }
    gs['opening_done'] = False          # guard: opening choice has NOT been offered yet
    gs['partner_1_revealed'] = False    # partner cards start secret until played
    gs['partner_2_revealed'] = False
    gs['timer_seat'] = None             # seat currently waiting for action (for client countdown)
    gs['timer_started_ms'] = None       # Unix timestamp ms when the current turn timer started
    return gs


# ─── Bidding Logic ────────────────────────────────────────────────────────────
def _process_bidding_mp(room):
    """
    Drive the bidding loop forward by one step.
    Called repeatedly (each human action and each bot turn calls it again).
    Returns early (suspending the loop) whenever it needs to wait for a human
    to act — SocketIO will call on_bid_action() which then calls this again.

    Flow:
      1. On the very first call (opening_done=False): offer the mandatory-bid
         holder a choice to open at 170 or jump straight to 270.
      2. After that: iterate through bid_order, skipping passed players.
         - Human seat → pause, broadcast state, and wait for bid_action event.
         - Bot seat   → auto-bid or pass based on ai_max_bid(), then continue.
      3. When 5 players have passed (only one left), call _finish_bidding_mp.
    """
    gs = room['gs']
    human_seats = get_human_seats(room)
    order = gs['bid_order']

    # ── Step 1: Mandatory opening choice (runs exactly once per round) ─────────
    if not gs.get('opening_done', False):
        gs['opening_done'] = True
        start = gs['start_seat']
        name = gs['player_names'][start]
        if start in human_seats:
            # Human holds the mandatory — pause and show them the opening panel
            gs['bidding_seat'] = start
            gs['mandatory_opening'] = True
            gs['message'] = "You hold the mandatory 170. Open at 170 or jump to 270 now?"
            broadcast_game_state(room['code'])
            return
        else:
            # Bot: jump to 270 if strong enough, otherwise log mandatory open and continue
            if ai_max_bid(gs['hands'][start]) >= 270:
                gs['bid'] = 270
                gs['last_bidder'] = start
                gs['log'].append(f"{name} bids 270!")
                _finish_bidding_mp(room)
                return
            gs['log'].append(f"{name} opens with the mandatory bid of 170.")

    # ── Step 2: Main bidding loop — process each player in bid_order ──────────
    while gs['pass_count'] < 5:
        pos = gs['bid_pos'] % len(order)
        player = order[pos]
        gs['bid_pos'] += 1

        if gs['passed'][player]:
            continue    # already passed — skip

        if player in human_seats:
            # Pause: human must choose bid or pass via the UI
            gs['bidding_seat'] = player
            name = gs['player_names'][player]
            gs['message'] = f"{name}: bid {gs['bid'] + 10} or pass?"
            broadcast_game_state(room['code'])
            return

        # ── Bot turn ──────────────────────────────────────────────────────────
        max_b = ai_max_bid(gs['hands'][player])
        next_bid = gs['bid'] + 10
        name = gs['player_names'][player]

        # Bot with a strong enough hand jumps directly to 270 (skips incremental bids)
        if max_b >= 270 and gs['bid'] < 270:
            gs['bid'] = 270
            gs['last_bidder'] = player
            gs['log'].append(f"{name} bids 270!")
            _finish_bidding_mp(room)
            return

        if next_bid <= max_b and next_bid <= 270:
            # Bot can afford the next increment — bid it
            gs['bid'] = next_bid
            gs['last_bidder'] = player
            gs['log'].append(f"{name} bids {next_bid}.")
            if gs['bid'] == 270:
                _finish_bidding_mp(room)
                return
        else:
            # Bot's hand isn't worth the next bid — pass
            gs['passed'][player] = True
            gs['pass_count'] += 1
            gs['log'].append(f"{name} passes.")
            if gs['pass_count'] >= 5:
                _finish_bidding_mp(room)
                return

    _finish_bidding_mp(room)


def _finish_bidding_mp(room):
    """
    Conclude the bidding phase: record the winner, then either wait for a
    human bidder to choose trump/partners or let a bot do it automatically.
    """
    gs = room['gs']
    gs['bidding_seat'] = None
    gs['bidder'] = gs['last_bidder']   # whoever last raised the bid wins
    bidder_name = gs['player_names'][gs['bidder']]
    gs['log'].append(f"─── {bidder_name} wins the bid at {gs['bid']}! ───")

    if gs['bidder'] in get_human_seats(room):
        # Human bidder — show the trump/partner selection panel
        gs['phase'] = 'set_trump'
        gs['message'] = f"{bidder_name}, choose trump suit and partner cards."
        broadcast_game_state(room['code'])
    else:
        # Bot bidder — choose trump/partners immediately, but briefly broadcast
        # the set_trump phase so clients see the transition and trigger the popup.
        # Without this, clients jump bidding→playing in one update and never set
        # prevPhase = 'set_trump', so showGameStartModal never fires.
        trump = ai_choose_trump(gs['hands'][gs['bidder']])
        p1, p2 = ai_choose_partners(gs['hands'][gs['bidder']], trump)
        gs['phase'] = 'set_trump'
        gs['trump'] = trump       # pre-populate so the broadcast includes them
        gs['partner_1'] = p1
        gs['partner_2'] = p2
        broadcast_game_state(room['code'])
        socketio.sleep(1.5)       # short pause; clients record prevPhase = 'set_trump'
        _apply_trump_partners_mp(room, trump, p1, p2, auto=True)


# ─── Trump / Partners ─────────────────────────────────────────────────────────
def _apply_trump_partners_mp(room, trump, partner_1, partner_2, auto=False):
    """
    Lock in the trump suit and partner cards, then transition to the playing phase.
    Scans all hands to discover which seats hold each partner card (those seats
    become the bidder's secret allies).
    auto=True means a bot made this choice — the log message reflects that.
    """
    gs = room['gs']
    gs['trump'] = trump
    gs['partner_1'] = partner_1
    gs['partner_2'] = partner_2

    # Discover which seats hold the partner cards
    for pnum in range(1, 7):
        hand = gs['hands'][pnum]
        if partner_1 in hand: gs['partner_1_player'] = pnum
        if partner_2 in hand: gs['partner_2_player'] = pnum

    bidder_name = gs['player_names'][gs['bidder']]
    p1_disp = SUIT_SYMBOLS[partner_1[0]] + RANK_DISPLAY[partner_1[1]]
    p2_disp = SUIT_SYMBOLS[partner_2[0]] + RANK_DISPLAY[partner_2[1]]
    label = "(Bot) " if auto else ""
    gs['log'].append(
        f"{bidder_name} {label}chose trump: {SUIT_NAMES[trump]}, "
        f"partners: {p1_disp} & {p2_disp}."
    )

    # Move to playing phase — bidder leads the first trick
    gs['phase'] = 'playing'
    gs['trick_leader'] = gs['bidder']
    gs['trick_play_order'] = get_trick_play_order(gs['trick_leader'])
    gs['message'] = "Game on!"
    gs['log'].append(f"─── Playing begins. {bidder_name} leads. ───")
    _process_trick_auto_mp(room)


def _validate_trump_partners(gs, trump, p1, p2, bidder_seat):
    """
    Server-side validation for the human bidder's trump/partner choices.
    Returns an error string if invalid, or None if everything is fine.
    Prevents cheating or accidental selection of cards the bidder already holds.
    """
    if trump not in SUITS: return "Invalid trump suit."
    all_cards = [s + r for s in SUITS for r in RANKS]
    if p1 not in all_cards: return f"Invalid partner card: {p1}"
    if p2 not in all_cards: return f"Invalid partner card: {p2}"
    if p1 == p2: return "Partner cards must be different."
    hand = gs['hands'][bidder_seat]
    if p1 in hand: return f"You already hold {p1}."
    if p2 in hand: return f"You already hold {p2}."
    return None


# ─── Trick Logic ──────────────────────────────────────────────────────────────
def _process_trick_auto_mp(room):
    """
    Advance the current trick as far as possible without needing human input.
    Runs in a while loop — each iteration either:
      - Plays a bot card and pauses 0.5s (so humans can see bot moves), OR
      - Hits a human's turn and returns early (waiting for play_card_action), OR
      - Finishes a full 6-card trick (5s pause so humans can read it), then
        starts the next trick or moves to scoring.

    socketio.sleep() is non-blocking in gevent — other requests still process.
    """
    gs = room['gs']
    human_seats = get_human_seats(room)
    has_humans = bool(human_seats)

    while gs['phase'] == 'playing':
        pos = len(gs['trick_cards'])
        if pos >= 6:
            # All 6 players have played — show the completed trick briefly then resolve it
            if has_humans:
                broadcast_game_state(room['code'])
                socketio.sleep(5)   # 5-second pause so humans can read who won
            _finish_trick(gs)
            if gs['phase'] != 'playing':
                # Round ended (scoring phase) — broadcast final state and stop
                broadcast_game_state(room['code'])
                return
            if gs['trick_leader'] in human_seats:
                leader_name = gs['player_names'][gs['trick_leader']]
                gs['message'] = f"{leader_name} leads this trick."
                broadcast_game_state(room['code'])
                return
            continue

        next_player = gs['trick_play_order'][pos]
        if next_player in human_seats:
            first_suit = gs['first_suit']
            if first_suit:
                if any(c[0] == first_suit for c in gs['hands'][next_player]):
                    gs['message'] = f"Follow {SUIT_NAMES[first_suit]} suit."
                else:
                    gs['message'] = "No cards of led suit. Play any."
            else:
                gs['message'] = "Your lead."
            broadcast_game_state(room['code'])
            return

        # Bot turn
        card = ai_play_card(
            player_num=next_player,
            hand=gs['hands'][next_player],
            played_order=list(gs['trick_played_by']),
            played_cards=list(gs['trick_cards']),
            trump=gs['trump'],
            bidder_num=gs['bidder'],
            p1_num=gs['partner_1_player'],
            p2_num=gs['partner_2_player'],
            first_suit=gs['first_suit'],
            trick_num=gs['trick_num'],
            all_played=[c for cards in gs['tricks_won'].values() for c in cards],
            partner_1_card=gs.get('partner_1'),
            partner_2_card=gs.get('partner_2'),
        )
        gs['hands'][next_player].remove(card)
        if not gs['trick_cards']:
            gs['first_suit'] = card[0]
        if card == gs.get('partner_1'): gs['partner_1_revealed'] = True
        if card == gs.get('partner_2'): gs['partner_2_revealed'] = True
        gs['trick_cards'].append(card)
        gs['trick_played_by'].append(next_player)
        name = gs['player_names'][next_player]
        disp = SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]]
        gs['log'].append(f"  {name} plays {disp}")
        if has_humans:
            broadcast_game_state(room['code'])
            socketio.sleep(0.5)


def _finish_trick(gs):
    """
    Resolve a completed 6-card trick:
      1. Find the winning card using beats() logic.
      2. Award all 6 cards to the winner's tricks_won pile (scored at end of round).
      3. Log the result and save last_trick_info (used for the "last trick" banner in the UI).
      4. Advance trick_num; if that was trick 8, jump to scoring.
      5. Otherwise set the trick_leader to the winner (they lead next).
    """
    cards = gs['trick_cards']
    first_suit = cards[0][0] if cards else gs['first_suit']
    win_idx = trick_winner_idx(cards, gs['trump'], first_suit)
    winner_num = gs['trick_played_by'][win_idx]
    winner_name = gs['player_names'][winner_num]

    # Give all cards in this trick to the winner
    for card in cards:
        gs['tricks_won'][winner_num].append(card)

    pts = sum(card_points(c) for c in cards)
    win_disp = SUIT_SYMBOLS[cards[win_idx][0]] + RANK_DISPLAY[cards[win_idx][1]]
    gs['log'].append(f"Trick {gs['trick_num']}: {winner_name} wins with {win_disp} ({pts} pts)")

    # Save for the last-trick banner shown at the top of the next trick
    gs['last_trick_info'] = {
        'order': list(gs['trick_played_by']),
        'cards': list(cards),
        'winner': winner_num,
        'winner_name': winner_name,
    }
    gs['trick_num'] += 1
    gs['trick_leader'] = winner_num
    gs['trick_cards'] = []
    gs['trick_played_by'] = []
    gs['first_suit'] = None

    if gs['trick_num'] > 8:
        _calculate_scores(gs)   # all 8 tricks done — tally points
        return
    gs['trick_play_order'] = get_trick_play_order(gs['trick_leader'])


def _calculate_scores(gs):
    """
    Tally the round results and update cumulative game_scores.

    Scoring rules:
      • Team = bidder + partner_1_player + partner_2_player
      • Bidder's team wins if their combined trick points >= the bid.
        - Win:  bidder earns bid×2 (or 1000 if bid was 270), each partner earns bid.
        - Lose: each opponent earns bid, bidder LOSES bid (deducted).
      • p1p / p2p may be the same seat if the bidder named a card they both hold
        (edge case: treated as one partner, counted once).
    """
    gs['phase'] = 'scoring'
    # Sum up each player's trick-point haul from the cards they won
    player_pts = {pnum: sum(card_points(c) for c in gs['tricks_won'][pnum])
                  for pnum in range(1, 7)}
    bidder = gs['bidder']
    p1p = gs['partner_1_player']
    p2p = gs['partner_2_player']
    bid = gs['bid']

    # Combine the bidder's team's points (avoid double-counting if partners share a seat)
    team_pts = player_pts[bidder]
    if p1p: team_pts += player_pts[p1p]
    if p2p and p2p != p1p: team_pts += player_pts[p2p]

    bidder_won = team_pts >= bid
    # Store the full breakdown for the scoring panel in the UI
    gs['round_result'] = {
        'bidder': bidder,
        'bidder_name': gs['player_names'][bidder],
        'bid': bid,
        'team_pts': team_pts,
        'bidder_won': bidder_won,
        'player_pts': player_pts,
        'partner_1_player': p1p,
        'partner_2_player': p2p,
    }

    if bidder_won:
        # Special 270 bonus: winning a maximum bid earns 1000 pts instead of bid×2
        bidder_award = 1000 if bid == 270 else bid * 2
        gs['game_scores'][bidder] += bidder_award
        if p1p: gs['game_scores'][p1p] += bid
        if p2p and p2p != p1p: gs['game_scores'][p2p] += bid
        suffix = " — MAXIMUM BID! ───" if bid == 270 else " ───"
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team wins! ({team_pts} ≥ {bid}){suffix}"
        )
    else:
        # Bidder's team fell short — opponents each earn the bid; bidder loses the bid
        for pnum in range(1, 7):
            if pnum not in (bidder, p1p, p2p):
                gs['game_scores'][pnum] += bid
        gs['game_scores'][bidder] -= bid
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team falls short! ({team_pts} < {bid}) ───"
        )
    gs['message'] = "Round over!"


def _validate_play(gs, card, seat):
    """
    Server-side validation before accepting a human's card play.
    Returns an error string if the play is illegal, or None if it's fine.
    Checks: correct phase, correct turn, card in hand, suit-following rule.
    """
    if gs['phase'] != 'playing': return "Not in playing phase."
    pos = len(gs['trick_cards'])
    order = gs.get('trick_play_order', [])
    if not order or order[pos] != seat: return "Not your turn."
    hand = gs['hands'][seat]
    if card not in hand: return "You don't hold that card."
    vc = valid_cards(hand, gs.get('first_suit'))
    if card not in vc: return f"Must follow {SUIT_NAMES.get(gs['first_suit'], '')} suit."
    return None


# ─── Client State ─────────────────────────────────────────────────────────────
def client_state_mp(gs, viewer_seat, room):
    """
    Build the JSON payload sent to one specific human player (viewer_seat).
    Each player gets a personalised view — they only see:
      • Their own hand (with 'valid' flags for playable cards)
      • Partner identities only if they are the bidder, are that partner, or
        the card has been played and revealed
    Everything else (opponent hand counts, trick display, scores, etc.) is
    the same for all players.
    """
    hand = gs['hands'].get(viewer_seat, [])
    first_suit = gs.get('first_suit')
    # During playing phase, mark which cards are legal to play (suit-following rule)
    vc = valid_cards(hand, first_suit) if gs['phase'] == 'playing' else hand

    # Enrich each card with its display symbol and whether it can be played now
    hand_display = [{
        'card': c,
        'disp': SUIT_SYMBOLS[c[0]] + RANK_DISPLAY[c[1]],
        'suit': c[0],
        'valid': c in vc,
    } for c in hand]

    trick_display = [{
        'player_num': pnum,
        'player_name': gs['player_names'][pnum],
        'card': card,
        'card_disp': SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]],
        'suit': card[0],
    } for pnum, card in zip(gs['trick_played_by'], gs['trick_cards'])]

    pos = len(gs['trick_cards'])
    current_player = None
    if gs['phase'] == 'playing' and pos < 6:
        order = gs.get('trick_play_order', [])
        if order: current_player = order[pos]

    p1_disp = (SUIT_SYMBOLS[gs['partner_1'][0]] + RANK_DISPLAY[gs['partner_1'][1]]
               if gs.get('partner_1') else None)
    p2_disp = (SUIT_SYMBOLS[gs['partner_2'][0]] + RANK_DISPLAY[gs['partner_2'][1]]
               if gs.get('partner_2') else None)

    round_result = None
    if gs.get('round_result'):
        rr = gs['round_result']
        round_result = {
            'bidder_name': rr['bidder_name'],
            'bid': rr['bid'],
            'team_pts': rr['team_pts'],
            'bidder_won': rr['bidder_won'],
            'partner_1_name': gs['player_names'].get(rr['partner_1_player'], '?'),
            'partner_2_name': gs['player_names'].get(rr['partner_2_player'], '?'),
            'player_pts': {gs['player_names'][p]: v for p, v in rr['player_pts'].items()},
        }

    last_trick = None
    if gs.get('last_trick_info'):
        lt = gs['last_trick_info']
        last_trick = {
            'winner_name': lt['winner_name'],
            'cards': [{'player_name': gs['player_names'][lt['order'][i]],
                       'disp': SUIT_SYMBOLS[c[0]] + RANK_DISPLAY[c[1]], 'suit': c[0]}
                      for i, c in enumerate(lt['cards'])],
        }

    # Opponent display order: 5 seats clockwise after viewer_seat
    opp_display = [((viewer_seat - 1 + i) % 6) + 1 for i in range(1, 6)]

    # Which seats in room are bots vs humans
    seat_types = {s: p['is_bot'] for s, p in room['seats'].items()}

    # Partner secrecy: only reveal partner player if viewer is bidder,
    # viewer IS that partner, or the card has already been played
    p1_revealed = gs.get('partner_1_revealed', False)
    p2_revealed = gs.get('partner_2_revealed', False)
    p1_player = gs.get('partner_1_player')
    p2_player = gs.get('partner_2_player')
    p1_visible = p1_player if (viewer_seat == p1_player or p1_revealed) else None
    p2_visible = p2_player if (viewer_seat == p2_player or p2_revealed) else None

    viewer_sid = room['seats'][viewer_seat].get('sid')
    is_host = viewer_sid == room.get('host_sid')
    host_seat_v = room['sid_to_seat'].get(room.get('host_sid'))
    host_name_v = gs['player_names'].get(host_seat_v, 'Host')
    non_host_humans_v = [s for s, pl in room['seats'].items() if not pl['is_bot'] and s != host_seat_v]
    vote_needed_v = len(non_host_humans_v) // 2 + 1 if non_host_humans_v else 0
    vote_count_v = len(room.get('vote_kick_votes', set()))

    return {
        'phase': gs['phase'],
        'my_seat': viewer_seat,
        'my_name': gs['player_names'].get(viewer_seat),
        'is_host': is_host,
        'player_names': {str(k): v for k, v in gs['player_names'].items()},
        'seat_types': {str(k): v for k, v in seat_types.items()},
        'hand': hand_display,
        'bid': gs['bid'],
        'bidder': gs['bidder'],
        'bidder_name': gs['player_names'].get(gs['bidder']) if gs['bidder'] else None,
        'bidding_seat': gs.get('bidding_seat'),
        'is_my_bid_turn': gs.get('bidding_seat') == viewer_seat and gs['phase'] == 'bidding',
        'mandatory_opening': gs.get('mandatory_opening', False),
        'last_bidder_name': gs['player_names'].get(gs.get('last_bidder')) if gs.get('last_bidder') else None,
        'is_mandatory_bid': gs['bid'] == 170 and gs.get('last_bidder') == gs.get('start_seat'),
        'is_my_trump_turn': gs.get('bidder') == viewer_seat and gs['phase'] == 'set_trump',
        'trump': gs.get('trump'),
        'trump_name': SUIT_NAMES.get(gs['trump']) if gs.get('trump') else None,
        'trump_symbol': SUIT_SYMBOLS.get(gs['trump']) if gs.get('trump') else None,
        'partner_1': gs.get('partner_1'),
        'partner_2': gs.get('partner_2'),
        'partner_1_disp': p1_disp,
        'partner_2_disp': p2_disp,
        'partner_1_revealed': p1_revealed,
        'partner_2_revealed': p2_revealed,
        'partner_1_player': p1_visible,
        'partner_2_player': p2_visible,
        'trick_num': gs['trick_num'],
        'trick_display': trick_display,
        'current_player': current_player,
        'is_my_play_turn': current_player == viewer_seat,
        'trick_leader': gs.get('trick_leader'),
        'first_suit': first_suit,
        'first_suit_name': SUIT_NAMES.get(first_suit) if first_suit else None,
        'passed': {str(k): v for k, v in gs['passed'].items()},
        'pass_count': gs['pass_count'],
        'message': gs.get('message', ''),
        'log': gs['log'][-20:],
        'round_result': round_result,
        'game_scores': {gs['player_names'][p]: v for p, v in gs['game_scores'].items()},
        'last_trick': last_trick,
        'hand_counts': {str(i): len(gs['hands'].get(i, [])) for i in range(1, 7)},
        'opp_display': opp_display,
        'room_code': room['code'],
        'vote_kick_count': vote_count_v,
        'vote_kick_needed': vote_needed_v,
        'my_vote_cast': viewer_seat in room.get('vote_kick_votes', set()),
        'host_name': host_name_v,
    }


# ─── HTTP Routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    """
    Serve the game page. On the first visit from a browser, set a UUID cookie
    that persists for 1 year — this lets us count unique devices on /stats
    without requiring login. Returning visitors reuse their existing cookie.
    """
    device_id = request.cookies.get('device_id')
    resp = make_response(render_template('index.html'))
    if not device_id:
        device_id = str(uuid.uuid4())
        resp.set_cookie('device_id', device_id, max_age=60*60*24*365, samesite='Lax')
    # Only count real browsers. Crawlers (Googlebot, Bingbot, etc.) include "Mozilla"
    # but also "bot"/"spider"/"crawler", and never store cookies so each visit inflates the count.
    ua = request.headers.get('User-Agent', '')
    ua_lower = ua.lower()
    is_real_browser = 'mozilla' in ua_lower and not any(
        kw in ua_lower for kw in ('bot', 'spider', 'crawler', 'headless', 'python', 'curl', 'wget', 'scraper')
    )
    if is_real_browser:
        redis_add_device(device_id)   # persists across deploys via Redis
    return resp

@app.route('/stats')
def stats():
    # Derive connected users from sid_room (always accurate — never drifts like a counter).
    # sid_room maps socket_id → room_code; every active in-room connection has an entry.
    connected_users = len(sid_room)

    # Only count a human seat as "active" if their socket is still in sid_room.
    # This prevents stale seats (connections that dropped without clean disconnect)
    # from inflating the count.
    active_players = sum(
        sum(
            1 for sid, seat in r.get('sid_to_seat', {}).items()
            if sid in sid_room and not r['seats'].get(seat, {}).get('is_bot', True)
        )
        for r in rooms.values()
    )

    # Count rooms that currently have 2 or more connected human players.
    multiplayer_rooms = sum(
        1 for r in rooms.values()
        if sum(
            1 for sid, seat in r.get('sid_to_seat', {}).items()
            if sid in sid_room and not r['seats'].get(seat, {}).get('is_bot', True)
        ) >= 2
    )

    # Public rooms snapshot
    pub_listed   = sum(1 for c in public_rooms_list if c in rooms and rooms[c]['status'] == 'lobby')
    pub_playing  = sum(1 for r in rooms.values() if r.get('is_public') and r['status'] == 'playing')
    pub_waitlist_len = len([c for c in public_waitlist if c in rooms])

    fmt = request.args.get('fmt', '')
    data = {
        'connected_users': connected_users,
        'active_rooms': len(rooms),
        'multiplayer_rooms': multiplayer_rooms,
        'active_players': active_players,
        'total_rooms_created': total_rooms_created,
        'total_multiplayer_rooms': total_multiplayer_rooms,
        'total_rounds_played': total_rounds_played,
        'unique_devices': redis_device_count(),
        'public_listed': pub_listed,
        'public_playing': pub_playing,
        'public_waitlist': pub_waitlist_len,
    }
    if fmt == 'json':
        return jsonify(data)
    return (
        f"<html><head><title>Three of Spades – Stats</title>"
        f"<meta http-equiv='refresh' content='10'>"
        f"<style>"
        f"body{{font-family:monospace;background:#111;color:#eee;padding:2rem}}"
        f"h1{{color:#a855f7}}"
        f"h2{{color:#888;font-size:0.9rem;text-transform:uppercase;letter-spacing:2px;margin-top:2rem;margin-bottom:0.3rem}}"
        f"td{{padding:4px 20px 4px 0}}"
        f"strong{{color:#6aacff}}"
        f".playing{{color:#ff6b6b;font-weight:bold;font-size:1.05em}}"
        f".dim{{color:#555}}"
        f"</style></head>"
        f"<body><h1>Three of Spades – Live Stats</h1>"
        f"<h2>&#x25CF; Live</h2>"
        f"<table>"
        f"<tr><td>Connected users</td><td><strong>{connected_users}</strong></td></tr>"
        f"<tr><td>Active rooms</td><td><strong>{len(rooms)}</strong>"
        f"  <span class='dim'>({multiplayer_rooms} multiplayer)</span></td></tr>"
        f"<tr><td>Active human players</td><td><strong>{active_players}</strong></td></tr>"
        f"</table>"
        f"<h2>&#x1F310; Public Rooms</h2>"
        f"<table>"
        f"<tr><td>Listed (open lobby)</td><td><strong>{pub_listed}</strong>"
        f"  <span class='dim'>/ 10 slots</span></td></tr>"
        f"<tr><td>&#x1F525; Currently PLAYING</td><td><span class='playing'>{pub_playing}</span>"
        f"  <span class='dim'>(active games that started as public)</span></td></tr>"
        f"<tr><td>On waitlist</td><td><strong>{pub_waitlist_len}</strong></td></tr>"
        f"</table>"
        f"<h2>&#x25B3; Historical (persists across deploys)</h2>"
        f"<table>"
        f"<tr><td>Unique devices</td><td><strong>"
        f"{redis_device_count() if redis_device_count() is not None else '<span style=color:#f88>N/A</span>'}"
        f"</strong></td></tr>"
        f"</table>"
        f"<h2>&#x25B3; This session (resets on deploy)</h2>"
        f"<table>"
        f"<tr><td>Rooms created</td><td><strong>{total_rooms_created}</strong></td></tr>"
        f"<tr><td>Multiplayer rooms</td><td><strong>{total_multiplayer_rooms}</strong>"
        f"  <span class='dim'>(ever had 2+ humans)</span></td></tr>"
        f"<tr><td>Rounds played</td><td><strong>{total_rounds_played}</strong></td></tr>"
        f"</table>"
        f"<p style='color:#555;font-size:0.8rem;margin-top:1.5rem'>Auto-refreshes every 10 s &nbsp;|&nbsp; "
        f"<a href='/stats?fmt=json' style='color:#777'>JSON</a></p>"
        f"</body></html>"
    )


# ─── Socket Events ────────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    """Track every new WebSocket connection for the /stats live counter."""
    global connected_sockets
    connected_sockets += 1


@socketio.on('rejoin_room')
def on_rejoin_room(data):
    """
    Handle page refresh / reconnect recovery.
    The client stores its room code, seat number, and name in localStorage and
    sends them on reconnect. If we can match the seat (by name), we swap the
    old socket ID for the new one and restore the player's full game view.
    If we can't find the room/seat, we send 'room_lost' to redirect to setup.
    """
    sid = request.sid
    code = (data.get('code') or '').strip().upper()
    my_seat = data.get('seat')
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'

    if not code or code not in rooms:
        emit('room_lost', {})
        return

    room = rooms[code]

    # Match by seat number AND name (human_name used if seat was botted during disconnect)
    found_seat = None
    if my_seat and my_seat in room['seats']:
        p = room['seats'][my_seat]
        if p.get('name') == name or p.get('human_name') == name:
            found_seat = my_seat

    if found_seat is None:
        emit('room_lost', {})
        return

    # Re-associate new socket with the seat, clean up the old socket mapping.
    # For lobby disconnects, p['sid'] is None but _disconnected_sid holds the original SID.
    p = room['seats'][found_seat]
    old_sid = p.get('sid') or p.pop('_disconnected_sid', None)
    if old_sid:
        sid_room.pop(old_sid, None)
        room['sid_to_seat'].pop(old_sid, None)

    p['sid'] = sid
    p['is_bot'] = False
    p['name'] = name            # restore display name (may have been replaced by "Bot X")
    p.pop('human_name', None)   # remove the backup name stored on disconnect
    p.pop('is_disconnected', None)
    p.pop('disconnected_at', None)
    if room.get('gs'):
        room['gs']['player_names'][found_seat] = name   # sync game state name
    room['sid_to_seat'][sid] = found_seat
    sid_room[sid] = code

    # Re-assign host if the original host is rejoining (their sid changed on reconnect)
    if old_sid == room.get('host_sid'):
        room['host_sid'] = sid

    sio_join(code)   # re-subscribe to the SocketIO room for broadcasts

    if room['status'] == 'lobby':
        broadcast_lobby(code)
    else:
        broadcast_game_state(code)


_LOBBY_REJOIN_GRACE = 120  # seconds a disconnected lobby player has to reconnect


def _cleanup_stale_lobby_seats(room_code):
    """Remove disconnected lobby seats past the reconnection window; delete the room if empty."""
    room = rooms.get(room_code)
    if not room or room['status'] != 'lobby':
        return
    now = time.time()
    stale = [
        s for s, p in room['seats'].items()
        if p.get('is_disconnected') and now - p.get('disconnected_at', now) > _LOBBY_REJOIN_GRACE
    ]
    for s in stale:
        old_sid = room['seats'][s].get('_disconnected_sid')
        if old_sid:
            sid_room.pop(old_sid, None)
            room['sid_to_seat'].pop(old_sid, None)
        del room['seats'][s]
    if not room['seats']:
        _remove_from_public(room_code)
        del rooms[room_code]


@socketio.on('disconnect')
def on_disconnect():
    """
    Handle a player closing their tab or losing connection.

    Mid-game: the seat is converted to a bot so the game can continue.
              The player's real name is saved as 'human_name' so they can
              rejoin and reclaim their seat. If ALL humans disconnect, the
              room is deleted (no point keeping a fully-bot game).

    In lobby: the seat is simply removed. If the room becomes empty, delete it.
              If the host left, assign a new host to the next human in the room.
    """
    global connected_sockets
    connected_sockets = max(0, connected_sockets - 1)
    sid = request.sid
    room_code = sid_room.pop(sid, None)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    seat = room['sid_to_seat'].get(sid)
    if not seat:
        return
    p = room['seats'].get(seat)
    if not p or p['is_bot']:
        return  # already a bot seat — nothing to do
    p['sid'] = None
    if room.get('gs') and room['status'] == 'playing':
        # Mid-game disconnect: bot takes over the seat
        p['_disconnected_sid'] = sid   # preserve so on_rejoin_room can clean up stale mapping
        del room['sid_to_seat'][sid]   # clear stale sid→seat entry immediately
        gs = room['gs']
        p['is_bot'] = True
        old_name = p['name']
        p['human_name'] = old_name  # preserve for rejoin matching
        bot_name = f"Bot {seat}"
        p['name'] = bot_name
        gs['player_names'][seat] = bot_name
        gs['log'].append(f"{old_name} disconnected (bot takes over).")
        # Delete the room if no humans remain (ghost room prevention)
        if not any(not pl['is_bot'] for pl in room['seats'].values()):
            del rooms[room_code]
            return
        _process_trick_auto_mp(room)
    else:
        # Lobby disconnect: keep the seat alive so the player can reconnect quickly
        # (mobile browsers drop the WebSocket when switching apps).
        # The seat is marked disconnected and cleaned up after _LOBBY_REJOIN_GRACE seconds.
        p['is_disconnected'] = True
        p['disconnected_at'] = time.time()
        p['_disconnected_sid'] = sid  # preserved so on_rejoin_room can restore host status
        del room['sid_to_seat'][sid]  # old SID is no longer valid
        # Promote the next connected human to host if the host just disconnected
        if room['host_sid'] == sid:
            for remaining in room['seats'].values():
                if not remaining['is_bot'] and not remaining.get('is_disconnected') and remaining.get('sid'):
                    room['host_sid'] = remaining['sid']
                    break
        # Broadcast to anyone still connected in the lobby
        if any(not pl['is_bot'] and pl.get('sid') and not pl.get('is_disconnected')
               for pl in room['seats'].values()):
            broadcast_lobby(room_code)
        # If this was a public room and it now has 0 connected humans, check whether
        # a waitlisted room with people should take the slot instead.
        if room.get('is_public') and room_code in public_rooms_list:
            if _connected_human_count(room_code) == 0:
                _maybe_evict_empty_public_room()
            else:
                # Player count changed — push updated public list
                _broadcast_public_rooms()


@socketio.on('kick_player')
def on_kick_player(data):
    """
    Host-initiated kick. Only the host can call this.
    Sends 'kicked' to the target's socket (client redirects them to setup),
    then replaces the seat with a bot mid-game or removes it in the lobby.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    if room['host_sid'] != sid:
        return  # only the host can kick
    target_seat = data.get('seat')
    if not target_seat or target_seat not in room['seats']:
        return
    p = room['seats'][target_seat]
    if p.get('is_bot'):
        return  # can't kick a bot
    host_seat = room['sid_to_seat'].get(sid)
    if host_seat == target_seat:
        return  # host can't kick themselves

    # Notify and disconnect the kicked player's socket
    target_sid = p.get('sid')
    if target_sid:
        socketio.emit('kicked', {}, to=target_sid)
        sid_room.pop(target_sid, None)
        room['sid_to_seat'].pop(target_sid, None)

    if room.get('gs') and room['status'] == 'playing':
        # Mid-game: replace with a bot so the round can continue
        gs = room['gs']
        p['is_bot'] = True
        old_name = p['name']
        bot_name = f"Bot {target_seat}"
        p['name'] = bot_name
        gs['player_names'][target_seat] = bot_name
        gs['log'].append(f"{old_name} was kicked (bot takes over).")
        broadcast_game_state(room_code)
        _process_trick_auto_mp(room)
    else:
        # Lobby: just remove the seat
        del room['seats'][target_seat]
        broadcast_lobby(room_code)


@socketio.on('vote_kick_host')
def on_vote_kick_host(data=None):
    """
    Non-host players can call a vote to remove the host.
    Each eligible player (non-host, non-bot, connected) can cast one vote.
    Once a simple majority of non-host humans have voted, the kick executes:
      - Host is sent 'kicked' and disconnected.
      - Seat is botted mid-game or removed in lobby.
      - The lowest-numbered remaining human becomes the new host.
    Before the threshold is reached, all players are updated with the new tally.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    seat = room['sid_to_seat'].get(sid)
    if not seat:
        return
    host_seat = room['sid_to_seat'].get(room['host_sid'])
    if seat == host_seat:
        return  # host can't vote to kick themselves
    p = room['seats'].get(seat)
    if not p or p.get('is_bot'):
        return  # bots don't vote

    if 'vote_kick_votes' not in room:
        room['vote_kick_votes'] = set()
    room['vote_kick_votes'].add(seat)   # set prevents double-voting

    # Need more than half of connected non-host humans (e.g. 2-of-3, 3-of-5)
    non_host_humans = [
        s for s, pl in room['seats'].items()
        if not pl['is_bot'] and not pl.get('is_disconnected') and s != host_seat
    ]
    needed = len(non_host_humans) // 2 + 1

    if len(room['vote_kick_votes']) >= needed:
        # Threshold reached — execute the vote-kick
        room['vote_kick_votes'] = set()   # reset for next potential vote
        host_p = room['seats'].get(host_seat)
        host_name = host_p.get('name', 'Host') if host_p else 'Host'
        host_sid = room['host_sid']

        if host_sid:
            socketio.emit('kicked', {}, to=host_sid)
            sid_room.pop(host_sid, None)
            room['sid_to_seat'].pop(host_sid, None)

        # Assign new host to first remaining human
        new_host_sid = None
        for s in sorted(room['seats'].keys()):
            pl = room['seats'][s]
            if not pl['is_bot'] and pl.get('sid') and pl['sid'] != host_sid:
                new_host_sid = pl['sid']
                break
        room['host_sid'] = new_host_sid

        if room.get('gs') and room['status'] == 'playing':
            gs = room['gs']
            if host_seat and host_p:
                host_p['is_bot'] = True
                host_p['human_name'] = host_name
                host_p['name'] = f'Bot {host_seat}'
                gs['player_names'][host_seat] = f'Bot {host_seat}'
                gs['log'].append(f'{host_name} was vote-kicked. Bot takes over.')
            if not any(not pl['is_bot'] for pl in room['seats'].values()):
                del rooms[room_code]
                return
            broadcast_game_state(room_code)
            _process_trick_auto_mp(room)
        else:
            if host_seat and host_seat in room['seats']:
                del room['seats'][host_seat]
            if new_host_sid:
                broadcast_lobby(room_code)
            else:
                _remove_from_public(room_code)
                del rooms[room_code]
    else:
        if room.get('gs') and room['status'] == 'playing':
            broadcast_game_state(room_code)
        else:
            broadcast_lobby(room_code)


@socketio.on('make_room_public')
def on_make_room_public(data):
    """Host toggles their lobby room between public (browseable) and private."""
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    if room['host_sid'] != sid or room['status'] != 'lobby':
        return
    make_public = bool(data.get('public', True))
    if make_public and not room.get('is_public'):
        room['is_public'] = True
        _add_to_public(room_code)
    elif not make_public and room.get('is_public'):
        room['is_public'] = False
        _remove_from_public(room_code)
    broadcast_lobby(room_code)


@socketio.on('get_public_rooms')
def on_get_public_rooms(data=None):
    """Client requests the current public room list (e.g. on opening the browse panel)."""
    visible = [info for info in (_public_room_info(c) for c in list(public_rooms_list)) if info]
    emit('public_rooms_update', {'rooms': visible})


@socketio.on('create_room')
def on_create_room(data):
    """
    Create a new room. The creator automatically becomes seat 1 and the host.
    A fresh room starts in 'lobby' status with no game state yet.
    """
    sid = request.sid
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'
    code = generate_room_code()
    global total_rooms_created
    rooms[code] = {
        'code': code,
        'host_sid': sid,        # sid of whoever can start the game / kick players
        'status': 'lobby',      # 'lobby' or 'playing'
        'seats': {1: {'name': name, 'sid': sid, 'is_bot': False}},
        'sid_to_seat': {sid: 1},  # reverse map: sid -> seat number
        'gs': None,               # no game state yet — filled by start_game
        'vote_kick_votes': set(), # set of seat numbers that have voted to kick the host
        'counted_multiplayer': False, # True once we've counted this room in total_multiplayer_rooms
        'is_public': False,       # True when host has opted in to public browsing
        'spectators': {},         # {sid: {'name': str, 'spec_id': int}}
        'spec_id_counter': 0,     # monotonic counter for stable spectator IDs
        'spec_id_to_sid': {},     # spec_id -> sid reverse map (for kick/pick)
        'seat_offer_seq': 0,      # incremented each time a seat is offered/filled/expired
        'turn_seq': 0,            # incremented each time a human turn timer starts or player acts
    }
    total_rooms_created += 1
    sid_room[sid] = code
    sio_join(code)         # subscribe this socket to the SocketIO room channel
    broadcast_lobby(code)


def _check_multiplayer_milestone(room):
    """Increment total_multiplayer_rooms the first time a room reaches 2+ humans."""
    global total_multiplayer_rooms
    if room.get('counted_multiplayer'):
        return
    human_count = sum(1 for p in room['seats'].values() if not p['is_bot'])
    if human_count >= 2:
        room['counted_multiplayer'] = True
        total_multiplayer_rooms += 1


@socketio.on('join_room_req')
def on_join_room(data):
    """
    Handle a player joining an existing room by code.
    If the game is already in progress, the player can only join by taking over
    a bot seat (mid-game join). If the lobby is full (6 humans), reject the join.
    """
    sid = request.sid
    code = (data.get('code') or '').strip().upper()
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'

    if code not in rooms:
        emit('join_error', {'msg': 'Room not found. Check the code.'})
        return

    _cleanup_stale_lobby_seats(code)  # purge expired disconnected seats before assigning a new one

    if code not in rooms:
        emit('join_error', {'msg': 'Room not found. Check the code.'})
        return
    room = rooms[code]

    # Prevent duplicate joins from the same socket (e.g. double-clicking Join)
    if sid in room['sid_to_seat']:
        broadcast_lobby(code)
        return

    if room['status'] == 'playing':
        # Prefer to restore the player to their original seat if they disconnected mid-game
        reclaim_seat = next(
            (s for s, pl in room['seats'].items() if pl['is_bot'] and pl.get('human_name') == name),
            None
        )
        bot_seat = reclaim_seat if reclaim_seat else next(
            (s for s, pl in sorted(room['seats'].items()) if pl['is_bot']),
            None
        )
        if bot_seat is None:
            emit('join_error', {'msg': 'Game in progress and no open seats.'})
            return
        p = room['seats'][bot_seat]
        p['name'] = name
        p['sid'] = sid
        p['is_bot'] = False
        p.pop('human_name', None)   # clear backup name if reclaiming original seat
        room['sid_to_seat'][sid] = bot_seat
        sid_room[sid] = code
        sio_join(code)
        if room.get('gs'):
            room['gs']['player_names'][bot_seat] = name
            action = 'rejoined' if reclaim_seat else f'joined and took over Bot seat'
            room['gs']['log'].append(f"{name} {action} {bot_seat}.")
        _check_multiplayer_milestone(room)
        broadcast_game_state(code)
        return

    # Normal lobby join: assign the next available seat number
    occupied = set(room['seats'].keys())
    next_seat = next((s for s in range(1, 7) if s not in occupied), None)
    if next_seat is None:
        emit('join_error', {'msg': 'Room is full (6 players).'})
        return

    room['seats'][next_seat] = {'name': name, 'sid': sid, 'is_bot': False}
    room['sid_to_seat'][sid] = next_seat
    sid_room[sid] = code
    sio_join(code)
    _check_multiplayer_milestone(room)
    # If this was a public room and all 6 seats are now taken, free the slot
    if room.get('is_public'):
        human_count = sum(1 for p in room['seats'].values() if not p['is_bot'] and not p.get('is_disconnected'))
        if human_count >= 6:
            _remove_from_public(code)
    broadcast_lobby(code)


@socketio.on('start_game')
def on_start_game(_data):
    """
    Host starts the game. Fills any empty seats with bots (numbered 1-6),
    resets vote-kick state, creates a fresh game state, and kicks off bidding.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        emit('room_lost', {})
        return
    room = rooms[room_code]
    if room['host_sid'] != sid:
        emit('game_error', {'msg': 'Only the host can start.'})
        return

    # Fill every seat that has no human player (or a disconnected lobby player) with a bot
    bot_num = 1
    for s in range(1, 7):
        if s not in room['seats'] or room['seats'][s].get('is_disconnected'):
            room['seats'][s] = {'name': f'Bot {bot_num}', 'sid': None, 'is_bot': True}
            bot_num += 1

    _remove_from_public(room_code)   # game started — no longer joinable via browse
    global total_rounds_played
    room['status'] = 'playing'
    room['vote_kick_votes'] = set()   # clear any stale votes from the lobby
    names_dict = {seat: p['name'] for seat, p in room['seats'].items()}
    room['gs'] = create_game_mp(names_dict)
    total_rounds_played += 1
    _process_bidding_mp(room)


@socketio.on('bid_action')
def on_bid_action(data):
    """
    Receive a human player's bid decision.
    Expected payload: { bid_yes: bool, jump_to_270: bool (optional) }

    Two cases:
      1. mandatory_opening=True: player chooses to open at 170 (continue bidding)
         or jump straight to 270 and lock it in.
      2. Normal bid: player either raises by 10 (or jumps to 270) or passes.
    In both cases, after recording the action, call _process_bidding_mp to
    continue the automated bidding loop.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs or gs['phase'] != 'bidding':
        return
    seat = room['sid_to_seat'].get(sid)
    if seat != gs.get('bidding_seat'):
        emit('game_error', {'msg': 'Not your turn to bid.'})
        return

    # ── Case 1: Mandatory opening choice ──────────────────────────────────────
    if gs.get('mandatory_opening'):
        gs.pop('mandatory_opening', None)
        gs['bidding_seat'] = None
        name = gs['player_names'][seat]
        if data.get('jump_to_270', False):
            gs['bid'] = 270
            gs['last_bidder'] = seat
            gs['log'].append(f"{name} bids 270!")
            _finish_bidding_mp(room)
        else:
            # Open at 170 and allow others to bid
            gs['log'].append(f"{name} opens with the mandatory bid of 170.")
            _process_bidding_mp(room)
        return

    # ── Case 2: Regular bid or pass ───────────────────────────────────────────
    bid_yes = data.get('bid_yes', False)
    jump_to_270 = data.get('jump_to_270', False)
    name = gs['player_names'][seat]

    if bid_yes:
        # Raise the bid (by 10, or directly to 270 if jump button was pressed)
        new_bid = 270 if jump_to_270 else min(gs['bid'] + 10, 270)
        gs['bid'] = new_bid
        gs['last_bidder'] = seat
        gs['log'].append(f"{name} bids 270!" if new_bid == 270 else f"{name} bids {new_bid}.")
        if new_bid == 270:
            gs['bidding_seat'] = None
            _finish_bidding_mp(room)
            return
    else:
        # Player passes — increment pass counter
        gs['passed'][seat] = True
        gs['pass_count'] += 1
        gs['log'].append(f"{name} passes.")
        if gs['pass_count'] >= 5:
            # 5 players have passed — the last bidder wins by default
            gs['bidding_seat'] = None
            _finish_bidding_mp(room)
            return

    gs['bidding_seat'] = None
    _process_bidding_mp(room)   # continue the loop to the next player


@socketio.on('set_trump_action')
def on_set_trump(data):
    """
    Receive the bidder's trump suit and two partner card choices.
    Validates them server-side (suit valid, cards not in bidder's hand, not duplicates),
    then calls _apply_trump_partners_mp to lock them in and start the playing phase.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs or gs['phase'] != 'set_trump':
        return
    seat = room['sid_to_seat'].get(sid)
    if seat != gs['bidder']:
        emit('game_error', {'msg': 'Only the bidder sets trump.'})
        return

    trump = data.get('trump', '')
    p1 = data.get('partner_1', '')
    p2 = data.get('partner_2', '')
    err = _validate_trump_partners(gs, trump, p1, p2, seat)
    if err:
        emit('game_error', {'msg': err})
        return
    _apply_trump_partners_mp(room, trump, p1, p2, auto=False)


@socketio.on('play_card_action')
def on_play_card(data):
    """
    Receive a human player's card play. Validates the play (turn order, suit following),
    then adds the card to the trick and continues the automated trick loop.
    Playing a partner card here reveals that player's team identity.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs or gs['phase'] != 'playing':
        return
    seat = room['sid_to_seat'].get(sid)
    card = data.get('card', '')
    err = _validate_play(gs, card, seat)
    if err:
        emit('game_error', {'msg': err})
        return

    gs['hands'][seat].remove(card)
    if not gs['trick_cards']:
        gs['first_suit'] = card[0]   # first card played sets the led suit
    # If a partner card is played, mark it as revealed for all players
    if card == gs.get('partner_1'): gs['partner_1_revealed'] = True
    if card == gs.get('partner_2'): gs['partner_2_revealed'] = True
    gs['trick_cards'].append(card)
    gs['trick_played_by'].append(seat)
    name = gs['player_names'][seat]
    disp = SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]]
    gs['log'].append(f"  {name} plays {disp}")
    _process_trick_auto_mp(room)   # resume the loop for bot plays / trick resolution


@socketio.on('new_round_action')
def on_new_round(_data):
    """
    Start the next round after the scoring screen.
    Carries over cumulative game_scores and player names, advances the mandatory
    start_seat by 1 (rotates clockwise), and launches fresh bidding.
    """
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs or gs['phase'] != 'scoring':
        return
    global total_rounds_played
    game_scores = gs['game_scores']
    names_dict = gs['player_names']
    # Rotate mandatory bid seat clockwise for the next round
    next_seat = gs['start_seat'] % 6 + 1
    room['gs'] = create_game_mp(names_dict, game_scores, start_seat=next_seat)
    total_rounds_played += 1
    _process_bidding_mp(room)


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Railway sets the PORT env var; fall back to 5050 for local development
    port = int(os.environ.get('PORT', 5050))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
