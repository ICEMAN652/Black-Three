from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room as sio_join
import os, random, string

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'black3_secret_2024_kala_teen')
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*', manage_session=False)

rooms = {}     # room_code -> room dict
sid_room = {}  # socket_id -> room_code
visit_count = 0
connected_sockets = 0  # live WebSocket connections

# ─── Card Constants ───────────────────────────────────────────────────────────
SUITS = ['s', 'h', 'd', 'c']
RANKS = ['a', 'k', 'q', 'j', 't', '9', '8', '7', '6', '5', '4', '3']
RANK_ORDER = {r: i + 1 for i, r in enumerate(RANKS)}
SUIT_NAMES = {'s': 'Spades', 'h': 'Hearts', 'd': 'Diamonds', 'c': 'Clubs'}
SUIT_SYMBOLS = {'s': '♠', 'h': '♥', 'd': '♦', 'c': '♣'}
RANK_DISPLAY = {
    'a': 'A', 'k': 'K', 'q': 'Q', 'j': 'J', 't': '10',
    '9': '9', '8': '8', '7': '7', '6': '6', '5': '5', '4': '4', '3': '3'
}


# ─── Card Helpers ─────────────────────────────────────────────────────────────
def card_sort_key(card):
    return (SUITS.index(card[0]), RANK_ORDER[card[1]])


def card_points(card):
    if card == 's3': return 30
    if card[1] == 'a': return 20
    if card[1] in 'kqjt': return 10
    return 0


def beats(challenger, champion, trump, first_suit):
    if champion is None: return True
    cT = challenger[0] == trump
    pT = champion[0] == trump
    if cT and not pT: return True
    if not cT and pT: return False
    if cT and pT: return RANK_ORDER[challenger[1]] < RANK_ORDER[champion[1]]
    if challenger[0] == first_suit and champion[0] != first_suit: return True
    if challenger[0] == first_suit and champion[0] == first_suit:
        return RANK_ORDER[challenger[1]] < RANK_ORDER[champion[1]]
    return False


def trick_winner_idx(cards, trump, first_suit):
    best = 0
    for i in range(1, len(cards)):
        if beats(cards[i], cards[best], trump, first_suit):
            best = i
    return best


def valid_cards(hand, first_suit):
    if first_suit is None: return list(hand)
    same = [c for c in hand if c[0] == first_suit]
    return same if same else list(hand)


def get_trick_play_order(leader):
    return [(leader - 1 + i) % 6 + 1 for i in range(6)]


# ─── AI Logic ─────────────────────────────────────────────────────────────────
def ai_max_bid(hand):
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
    suit_score = {}
    for s in SUITS:
        cnt = sum(1 for c in hand if c[0] == s)
        bonus = sum(0.5 if c[1] == 'a' else 0.25 if c[1] == 'k' else 0
                    for c in hand if c[0] == s)
        suit_score[s] = cnt + bonus
    return max(suit_score, key=suit_score.get)


def ai_choose_partners(hand, trump):
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
    return (suit + 'a') in all_played


def _trumps_gone(all_played, trump):
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
    is_bidder = player_num == bidder_num
    is_partner = player_num in (p1_num, p2_num)
    is_bidder_team = is_bidder or is_partner
    trump_in_hand = [c for c in hand if c[0] == trump]

    if first_suit is None:
        return _ai_lead(player_num, hand, trump, trump_in_hand, is_bidder,
                        is_partner, p1_num, trick_num,
                        all_played, partner_1_card, partner_2_card)
    return _ai_follow(player_num, hand, played_order, played_cards, trump,
                      first_suit, trump_in_hand, is_bidder, is_partner,
                      is_bidder_team, bidder_num, p1_num, p2_num, trick_num,
                      partner_1_card, partner_2_card)


def _ai_lead(player_num, hand, trump, trump_in_hand, is_bidder, is_partner,
             p1_num, trick_num, all_played, p1c, p2c):
    if is_bidder:
        return _bidder_lead(hand, trump, trump_in_hand, trick_num, all_played, p1c, p2c)
    if is_partner:
        other_pc = p2c if player_num == p1_num else p1c
        return _partner_lead(hand, trump, trump_in_hand, trick_num, all_played, other_pc)
    return _opponent_lead(hand, trump, all_played)


def _bidder_lead(hand, trump, trump_in_hand, trick_num, all_played, p1c, p2c):
    has_ta = (trump + 'a') in hand
    has_tk = (trump + 'k') in hand

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

    # Tricks 4-8 (or after clearing): point maximisation
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
    high = _best_point_card(hand, all_played, trump)
    if high:
        return high
    return _throwaway(hand, trump)


def _ai_follow(player_num, hand, played_order, played_cards, trump,
               first_suit, trump_in_hand, is_bidder, is_partner,
               is_bidder_team, bidder_num, p1_num, p2_num, trick_num,
               p1c, p2c):
    my_valid = valid_cards(hand, first_suit)
    same_suit = [c for c in my_valid if c[0] == first_suit]

    win_idx = trick_winner_idx(played_cards, trump, first_suit)
    cur_winner = played_order[win_idx]
    cur_best = played_cards[win_idx]
    winner_is_team = cur_winner in (bidder_num, p1_num, p2_num)

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

    # ── All other tricks ──────────────────────────────────────────────────────
    pts_in_trick = sum(card_points(c) for c in played_cards)

    if same_suit:
        if is_bidder_team:
            if winner_is_team and cur_winner != player_num:
                return _low(same_suit)
            can_beat = [c for c in same_suit if beats(c, cur_best, trump, first_suit)]
            if can_beat:
                return _low(can_beat)
            return _low(same_suit)
        else:
            if not winner_is_team:
                return _high(same_suit)
            can_beat = [c for c in same_suit if beats(c, cur_best, trump, first_suit)]
            if can_beat:
                return _low(can_beat)
            return _low(same_suit)
    else:
        if is_bidder_team:
            if not winner_is_team:
                can_beat = [c for c in trump_in_hand if beats(c, cur_best, trump, first_suit)]
                if can_beat:
                    return _low(can_beat)
            return _throwaway(hand, trump)
        else:
            # Opponent: ruff if trick is valuable and team is winning it
            if winner_is_team and pts_in_trick >= 20:
                can_beat = [c for c in trump_in_hand if beats(c, cur_best, trump, first_suit)]
                if can_beat:
                    return _low(can_beat)
            return _throwaway(hand, trump)


# ─── Room Management ──────────────────────────────────────────────────────────
def generate_room_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


def get_human_seats(room):
    return {seat for seat, p in room['seats'].items() if not p['is_bot']}


def broadcast_lobby(room_code):
    room = rooms[room_code]
    players_list = [
        {'seat': s, 'name': room['seats'][s]['name'], 'is_bot': room['seats'][s]['is_bot']}
        for s in sorted(room['seats'])
    ]
    for seat, p in room['seats'].items():
        if not p['is_bot'] and p.get('sid'):
            socketio.emit('lobby_update', {
                'room_code': room_code,
                'players': players_list,
                'is_host': p['sid'] == room['host_sid'],
                'my_seat': seat,
            }, to=p['sid'])


def broadcast_game_state(room_code):
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs:
        return
    for seat, p in room['seats'].items():
        if not p['is_bot'] and p.get('sid'):
            socketio.emit('game_state', client_state_mp(gs, seat, room), to=p['sid'])


# ─── Game State ───────────────────────────────────────────────────────────────
def create_game_mp(names_dict, game_scores=None, start_seat=1):
    deck = [s + r for s in SUITS for r in RANKS]
    random.shuffle(deck)
    hands = {i + 1: sorted(deck[i * 8:(i + 1) * 8], key=card_sort_key) for i in range(6)}
    # bid_order: everyone else bids first, start_seat holds mandatory 170 at end
    bid_order = [(start_seat - 1 + i) % 6 + 1 for i in range(1, 7)]
    gs = {
        'phase': 'bidding',
        'player_names': names_dict,
        'hands': hands,
        'bid': 170,
        'last_bidder': start_seat,
        'start_seat': start_seat,
        'passed': {i: False for i in range(1, 7)},
        'pass_count': 0,
        'bid_pos': 0,
        'bid_order': bid_order,
        'bidding_seat': None,
        'bidder': None,
        'trump': None,
        'partner_1': None,
        'partner_2': None,
        'partner_1_player': None,
        'partner_2_player': None,
        'trick_num': 1,
        'trick_leader': None,
        'trick_play_order': [],
        'trick_cards': [],
        'trick_played_by': [],
        'first_suit': None,
        'tricks_won': {i: [] for i in range(1, 7)},
        'last_trick_info': None,
        'game_scores': game_scores or {i: 0 for i in range(1, 7)},
        'round_result': None,
        'log': [],
        'message': '',
    }
    gs['opening_done'] = False
    gs['partner_1_revealed'] = False
    gs['partner_2_revealed'] = False
    return gs


# ─── Bidding Logic ────────────────────────────────────────────────────────────
def _process_bidding_mp(room):
    gs = room['gs']
    human_seats = get_human_seats(room)
    order = gs['bid_order']

    # First call: give the mandatory-bid holder a chance to jump to 270 immediately
    if not gs.get('opening_done', False):
        gs['opening_done'] = True
        start = gs['start_seat']
        name = gs['player_names'][start]
        if start in human_seats:
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

    while gs['pass_count'] < 5:
        pos = gs['bid_pos'] % len(order)
        player = order[pos]
        gs['bid_pos'] += 1

        if gs['passed'][player]:
            continue

        if player in human_seats:
            gs['bidding_seat'] = player
            name = gs['player_names'][player]
            gs['message'] = f"{name}: bid {gs['bid'] + 10} or pass?"
            broadcast_game_state(room['code'])
            return

        # Bot turn
        max_b = ai_max_bid(gs['hands'][player])
        next_bid = gs['bid'] + 10
        name = gs['player_names'][player]

        # Bot with a strong enough hand jumps directly to 270
        if max_b >= 270 and gs['bid'] < 270:
            gs['bid'] = 270
            gs['last_bidder'] = player
            gs['log'].append(f"{name} bids 270!")
            _finish_bidding_mp(room)
            return

        if next_bid <= max_b and next_bid <= 270:
            gs['bid'] = next_bid
            gs['last_bidder'] = player
            gs['log'].append(f"{name} bids {next_bid}.")
            if gs['bid'] == 270:
                _finish_bidding_mp(room)
                return
        else:
            gs['passed'][player] = True
            gs['pass_count'] += 1
            gs['log'].append(f"{name} passes.")
            if gs['pass_count'] >= 5:
                _finish_bidding_mp(room)
                return

    _finish_bidding_mp(room)


def _finish_bidding_mp(room):
    gs = room['gs']
    gs['bidding_seat'] = None
    gs['bidder'] = gs['last_bidder']
    bidder_name = gs['player_names'][gs['bidder']]
    gs['log'].append(f"─── {bidder_name} wins the bid at {gs['bid']}! ───")

    if gs['bidder'] in get_human_seats(room):
        gs['phase'] = 'set_trump'
        gs['message'] = f"{bidder_name}, choose trump suit and partner cards."
        broadcast_game_state(room['code'])
    else:
        gs['phase'] = 'set_trump'
        trump = ai_choose_trump(gs['hands'][gs['bidder']])
        p1, p2 = ai_choose_partners(gs['hands'][gs['bidder']], trump)
        _apply_trump_partners_mp(room, trump, p1, p2, auto=True)


# ─── Trump / Partners ─────────────────────────────────────────────────────────
def _apply_trump_partners_mp(room, trump, partner_1, partner_2, auto=False):
    gs = room['gs']
    gs['trump'] = trump
    gs['partner_1'] = partner_1
    gs['partner_2'] = partner_2

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

    gs['phase'] = 'playing'
    gs['trick_leader'] = gs['bidder']
    gs['trick_play_order'] = get_trick_play_order(gs['trick_leader'])
    gs['message'] = "Game on!"
    gs['log'].append(f"─── Playing begins. {bidder_name} leads. ───")
    _process_trick_auto_mp(room)


def _validate_trump_partners(gs, trump, p1, p2, bidder_seat):
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
    gs = room['gs']
    human_seats = get_human_seats(room)
    has_humans = bool(human_seats)

    while gs['phase'] == 'playing':
        pos = len(gs['trick_cards'])
        if pos >= 6:
            # Show completed trick for 5s before clearing
            if has_humans:
                broadcast_game_state(room['code'])
                socketio.sleep(5)
            _finish_trick(gs)
            if gs['phase'] != 'playing':
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
    cards = gs['trick_cards']
    first_suit = cards[0][0] if cards else gs['first_suit']
    win_idx = trick_winner_idx(cards, gs['trump'], first_suit)
    winner_num = gs['trick_played_by'][win_idx]
    winner_name = gs['player_names'][winner_num]

    for card in cards:
        gs['tricks_won'][winner_num].append(card)

    pts = sum(card_points(c) for c in cards)
    win_disp = SUIT_SYMBOLS[cards[win_idx][0]] + RANK_DISPLAY[cards[win_idx][1]]
    gs['log'].append(f"Trick {gs['trick_num']}: {winner_name} wins with {win_disp} ({pts} pts)")

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
        _calculate_scores(gs)
        return
    gs['trick_play_order'] = get_trick_play_order(gs['trick_leader'])


def _calculate_scores(gs):
    gs['phase'] = 'scoring'
    player_pts = {pnum: sum(card_points(c) for c in gs['tricks_won'][pnum])
                  for pnum in range(1, 7)}
    bidder = gs['bidder']
    p1p = gs['partner_1_player']
    p2p = gs['partner_2_player']
    bid = gs['bid']

    team_pts = player_pts[bidder]
    if p1p: team_pts += player_pts[p1p]
    if p2p and p2p != p1p: team_pts += player_pts[p2p]

    bidder_won = team_pts >= bid
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
        bidder_award = 1000 if bid == 270 else bid * 2
        gs['game_scores'][bidder] += bidder_award
        if p1p: gs['game_scores'][p1p] += bid
        if p2p and p2p != p1p: gs['game_scores'][p2p] += bid
        suffix = " — MAXIMUM BID! ───" if bid == 270 else " ───"
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team wins! ({team_pts} ≥ {bid}){suffix}"
        )
    else:
        for pnum in range(1, 7):
            if pnum not in (bidder, p1p, p2p):
                gs['game_scores'][pnum] += bid
        gs['game_scores'][bidder] -= bid
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team falls short! ({team_pts} < {bid}) ───"
        )
    gs['message'] = "Round over!"


def _validate_play(gs, card, seat):
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
    hand = gs['hands'].get(viewer_seat, [])
    first_suit = gs.get('first_suit')
    vc = valid_cards(hand, first_suit) if gs['phase'] == 'playing' else hand

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
    }


# ─── HTTP Routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    global visit_count
    visit_count += 1
    return render_template('index.html')

@app.route('/stats')
def stats():
    active_players = sum(
        sum(1 for p in r['seats'].values() if not p['is_bot'])
        for r in rooms.values()
    )
    fmt = request.args.get('fmt', '')
    data = {
        'connected_users': connected_sockets,
        'visits': visit_count,
        'active_rooms': len(rooms),
        'active_players': active_players,
    }
    if fmt == 'json':
        return jsonify(data)
    return (
        f"<html><head><title>Three of Spades – Stats</title>"
        f"<meta http-equiv='refresh' content='10'>"
        f"<style>body{{font-family:monospace;background:#111;color:#eee;padding:2rem}}"
        f"h1{{color:#a855f7}}td{{padding:4px 20px 4px 0}}strong{{color:#6aacff}}</style></head>"
        f"<body><h1>Three of Spades – Live Stats</h1>"
        f"<table>"
        f"<tr><td>Connected users</td><td><strong>{connected_sockets}</strong></td></tr>"
        f"<tr><td>Total page visits</td><td><strong>{visit_count}</strong></td></tr>"
        f"<tr><td>Active rooms</td><td><strong>{len(rooms)}</strong></td></tr>"
        f"<tr><td>Active human players</td><td><strong>{active_players}</strong></td></tr>"
        f"</table>"
        f"<p style='color:#666;font-size:0.8rem'>Auto-refreshes every 10 s &nbsp;|&nbsp; "
        f"<a href='/stats?fmt=json' style='color:#888'>JSON</a></p>"
        f"</body></html>"
    )


# ─── Socket Events ────────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    global connected_sockets
    connected_sockets += 1


@socketio.on('rejoin_room')
def on_rejoin_room(data):
    sid = request.sid
    code = (data.get('code') or '').strip().upper()
    my_seat = data.get('seat')
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'

    if not code or code not in rooms:
        emit('room_lost', {})
        return

    room = rooms[code]

    # Find the player's seat by seat number + matching name
    found_seat = None
    if my_seat and my_seat in room['seats']:
        p = room['seats'][my_seat]
        if p.get('name') == name or p.get('human_name') == name:
            found_seat = my_seat

    if found_seat is None:
        emit('room_lost', {})
        return

    # Re-associate this socket with the seat
    p = room['seats'][found_seat]
    old_sid = p.get('sid')
    if old_sid:
        sid_room.pop(old_sid, None)
        room['sid_to_seat'].pop(old_sid, None)

    p['sid'] = sid
    p['is_bot'] = False
    p['name'] = name  # restore original name
    p.pop('human_name', None)
    if room.get('gs'):
        room['gs']['player_names'][found_seat] = name
    room['sid_to_seat'][sid] = found_seat
    sid_room[sid] = code

    # Re-assign host if the original host is rejoining
    if old_sid == room.get('host_sid'):
        room['host_sid'] = sid

    sio_join(code)

    if room['status'] == 'lobby':
        broadcast_lobby(code)
    else:
        broadcast_game_state(code)


@socketio.on('disconnect')
def on_disconnect():
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
        return
    p['sid'] = None
    if room.get('gs') and room['status'] == 'playing':
        gs = room['gs']
        p['is_bot'] = True
        old_name = p['name']
        p['human_name'] = old_name  # preserve for rejoin matching
        bot_name = f"Bot {seat}"
        p['name'] = bot_name
        gs['player_names'][seat] = bot_name
        gs['log'].append(f"{old_name} disconnected (bot takes over).")
        # Clean up room if no humans remain
        if not any(not pl['is_bot'] for pl in room['seats'].values()):
            del rooms[room_code]
            return
        _process_trick_auto_mp(room)
    else:
        del room['seats'][seat]
        del room['sid_to_seat'][sid]
        if room['seats']:
            # Assign new host if needed
            if room['host_sid'] == sid:
                for remaining in room['seats'].values():
                    if not remaining['is_bot'] and remaining.get('sid'):
                        room['host_sid'] = remaining['sid']
                        break
            broadcast_lobby(room_code)
        else:
            del rooms[room_code]


@socketio.on('kick_player')
def on_kick_player(data):
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    if room['host_sid'] != sid:
        return
    target_seat = data.get('seat')
    if not target_seat or target_seat not in room['seats']:
        return
    p = room['seats'][target_seat]
    if p.get('is_bot'):
        return
    host_seat = room['sid_to_seat'].get(sid)
    if host_seat == target_seat:
        return

    target_sid = p.get('sid')
    if target_sid:
        socketio.emit('kicked', {}, to=target_sid)
        sid_room.pop(target_sid, None)
        room['sid_to_seat'].pop(target_sid, None)

    if room.get('gs') and room['status'] == 'playing':
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
        del room['seats'][target_seat]
        broadcast_lobby(room_code)


@socketio.on('create_room')
def on_create_room(data):
    sid = request.sid
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'
    code = generate_room_code()
    rooms[code] = {
        'code': code,
        'host_sid': sid,
        'status': 'lobby',
        'seats': {1: {'name': name, 'sid': sid, 'is_bot': False}},
        'sid_to_seat': {sid: 1},
        'gs': None,
    }
    sid_room[sid] = code
    sio_join(code)
    broadcast_lobby(code)


@socketio.on('join_room_req')
def on_join_room(data):
    sid = request.sid
    code = (data.get('code') or '').strip().upper()
    name = (data.get('name') or 'Player').strip()[:20] or 'Player'

    if code not in rooms:
        emit('join_error', {'msg': 'Room not found. Check the code.'})
        return
    room = rooms[code]

    # Prevent duplicate joins from the same socket
    if sid in room['sid_to_seat']:
        broadcast_lobby(code)
        return

    if room['status'] == 'playing':
        # Allow joining mid-game only if a bot seat exists
        bot_seat = next(
            (s for s, p in sorted(room['seats'].items()) if p['is_bot']),
            None
        )
        if bot_seat is None:
            emit('join_error', {'msg': 'Game in progress and no open seats.'})
            return
        # Replace the bot with this human player
        p = room['seats'][bot_seat]
        p['name'] = name
        p['sid'] = sid
        p['is_bot'] = False
        room['sid_to_seat'][sid] = bot_seat
        sid_room[sid] = code
        sio_join(code)
        # Update the game state player name too
        if room.get('gs'):
            room['gs']['player_names'][bot_seat] = name
            room['gs']['log'].append(f"{name} joined and took over Bot seat {bot_seat}.")
        broadcast_game_state(code)
        return

    occupied = set(room['seats'].keys())
    next_seat = next((s for s in range(1, 7) if s not in occupied), None)
    if next_seat is None:
        emit('join_error', {'msg': 'Room is full (6 players).'})
        return

    room['seats'][next_seat] = {'name': name, 'sid': sid, 'is_bot': False}
    room['sid_to_seat'][sid] = next_seat
    sid_room[sid] = code
    sio_join(code)
    broadcast_lobby(code)


@socketio.on('start_game')
def on_start_game(_data):
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        emit('room_lost', {})
        return
    room = rooms[room_code]
    if room['host_sid'] != sid:
        emit('game_error', {'msg': 'Only the host can start.'})
        return

    # Fill empty seats with bots
    bot_num = 1
    for s in range(1, 7):
        if s not in room['seats']:
            room['seats'][s] = {'name': f'Bot {bot_num}', 'sid': None, 'is_bot': True}
            bot_num += 1

    room['status'] = 'playing'
    names_dict = {seat: p['name'] for seat, p in room['seats'].items()}
    room['gs'] = create_game_mp(names_dict)
    _process_bidding_mp(room)


@socketio.on('bid_action')
def on_bid_action(data):
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

    # Handle the mandatory opening choice (open at 170 and let others bid, or jump to 270 now)
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
            gs['log'].append(f"{name} opens with the mandatory bid of 170.")
            _process_bidding_mp(room)
        return

    bid_yes = data.get('bid_yes', False)
    jump_to_270 = data.get('jump_to_270', False)
    name = gs['player_names'][seat]

    if bid_yes:
        new_bid = 270 if jump_to_270 else min(gs['bid'] + 10, 270)
        gs['bid'] = new_bid
        gs['last_bidder'] = seat
        gs['log'].append(f"{name} bids 270!" if new_bid == 270 else f"{name} bids {new_bid}.")
        if new_bid == 270:
            gs['bidding_seat'] = None
            _finish_bidding_mp(room)
            return
    else:
        gs['passed'][seat] = True
        gs['pass_count'] += 1
        gs['log'].append(f"{name} passes.")
        if gs['pass_count'] >= 5:
            gs['bidding_seat'] = None
            _finish_bidding_mp(room)
            return

    gs['bidding_seat'] = None
    _process_bidding_mp(room)


@socketio.on('set_trump_action')
def on_set_trump(data):
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
        gs['first_suit'] = card[0]
    if card == gs.get('partner_1'): gs['partner_1_revealed'] = True
    if card == gs.get('partner_2'): gs['partner_2_revealed'] = True
    gs['trick_cards'].append(card)
    gs['trick_played_by'].append(seat)
    name = gs['player_names'][seat]
    disp = SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]]
    gs['log'].append(f"  {name} plays {disp}")
    _process_trick_auto_mp(room)


@socketio.on('new_round_action')
def on_new_round(_data):
    sid = request.sid
    room_code = sid_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    gs = room.get('gs')
    if not gs or gs['phase'] != 'scoring':
        return
    game_scores = gs['game_scores']
    names_dict = gs['player_names']
    next_seat = gs['start_seat'] % 6 + 1
    room['gs'] = create_game_mp(names_dict, game_scores, start_seat=next_seat)
    _process_bidding_mp(room)


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
