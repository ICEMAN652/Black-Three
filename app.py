from flask import Flask, render_template, request, jsonify, session
import os
import random
import uuid
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'black3_secret_2024_kala_teen')

# In-memory game storage keyed by session game_id
games = {}

# ─── Card Constants ───────────────────────────────────────────────────────────
SUITS = ['s', 'h', 'd', 'c']          # spades, hearts, diamonds, clubs
RANKS = ['a', 'k', 'q', 'j', 't', '9', '8', '7', '6', '5', '4', '3']
RANK_ORDER = {r: i + 1 for i, r in enumerate(RANKS)}  # a=1 (best) … 3=12 (worst)

SUIT_NAMES = {'s': 'Spades', 'h': 'Hearts', 'd': 'Diamonds', 'c': 'Clubs'}
SUIT_SYMBOLS = {'s': '♠', 'h': '♥', 'd': '♦', 'c': '♣'}
RANK_DISPLAY = {
    'a': 'A', 'k': 'K', 'q': 'Q', 'j': 'J', 't': '10',
    '9': '9', '8': '8', '7': '7', '6': '6', '5': '5', '4': '4', '3': '3'
}
AI_NAMES = {2: 'Priya', 3: 'Arjun', 4: 'Kavya', 5: 'Rajan', 6: 'Sunita'}


# ─── Card Helpers ─────────────────────────────────────────────────────────────
def card_sort_key(card):
    return (SUITS.index(card[0]), RANK_ORDER[card[1]])


def card_points(card):
    if card == 's3':
        return 30
    if card[1] == 'a':
        return 20
    if card[1] in 'kqjt':
        return 10
    return 0


def beats(challenger, champion, trump, first_suit):
    """True if challenger beats current champion in this trick."""
    if champion is None:
        return True
    c_trump = challenger[0] == trump
    p_trump = champion[0] == trump
    if c_trump and not p_trump:
        return True
    if not c_trump and p_trump:
        return False
    if c_trump and p_trump:
        return RANK_ORDER[challenger[1]] < RANK_ORDER[champion[1]]
    # Neither trump
    if challenger[0] == first_suit and champion[0] != first_suit:
        return True
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
    """Cards that may be played given first suit led (or all if leading)."""
    if first_suit is None:
        return list(hand)
    same = [c for c in hand if c[0] == first_suit]
    return same if same else list(hand)


def get_trick_play_order(leader):
    """6-player list starting from leader, wrapping 1-6."""
    return [(leader - 1 + i) % 6 + 1 for i in range(6)]


# ─── AI Logic ─────────────────────────────────────────────────────────────────
def ai_max_bid(hand):
    """Calculate how high this AI hand is worth bidding."""
    points = 60  # partner bonus
    suit_count = {s: 0 for s in SUITS}
    has_black3 = False
    for card in hand:
        r, s = card[1], card[0]
        if r == 'a':
            points += 45
        elif r == 'k':
            points += 35
        elif r == 'q':
            points += 25
        suit_count[s] += 1
        if card == 's3':
            has_black3 = True
    best = max(suit_count, key=suit_count.get)
    if suit_count[best] >= 4:
        points += suit_count[best] * 10
        if best == 's' and has_black3:
            points += 20
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
    # Trump ace/king first if we don't hold them
    for r in ['a', 'k']:
        if trump + r not in has:
            priority.append(trump + r)
    # Non-trump aces
    for s in SUITS:
        if s != trump and s + 'a' not in has:
            priority.append(s + 'a')
    # Non-trump kings
    for s in SUITS:
        if s != trump and s + 'k' not in has:
            priority.append(s + 'k')
    # Queens, jacks, tens
    for r in ['q', 'j', 't']:
        for s in SUITS:
            if s + r not in has:
                priority.append(s + r)
    # Any remaining
    for s in SUITS:
        for r in RANKS:
            c = s + r
            if c not in has:
                priority.append(c)
    chosen = []
    for c in priority:
        if c not in chosen:
            chosen.append(c)
        if len(chosen) == 2:
            break
    return chosen[0], chosen[1]


def ai_play_card(player_num, hand, played_order, played_cards, trump,
                 bidder_num, p1_num, p2_num, first_suit, trick_num):
    """Return the card AI player_num should play."""
    is_bidder_team = player_num in (bidder_num, p1_num, p2_num)
    is_first = (first_suit is None)
    trump_in_hand = [c for c in hand if c[0] == trump]

    if is_first:
        return _ai_lead(hand, trump_in_hand, is_bidder_team,
                        player_num, bidder_num, trick_num)
    else:
        return _ai_follow(hand, played_order, played_cards, trump, first_suit,
                          trump_in_hand, is_bidder_team,
                          player_num, bidder_num, p1_num, p2_num)


def _ai_lead(hand, trump_in_hand, is_bidder_team, player_num, bidder_num, trick_num):
    if player_num == bidder_num:
        # Bidder: flush trump early (tricks 1-3), then lead high-value cards
        if trick_num <= 3 and trump_in_hand:
            return min(trump_in_hand, key=lambda c: RANK_ORDER[c[1]])
        high_val = sorted(hand, key=lambda c: (-card_points(c), RANK_ORDER[c[1]]))
        return high_val[0]
    elif is_bidder_team:
        # Partner: support bidder with trump or high card
        if trump_in_hand:
            return min(trump_in_hand, key=lambda c: RANK_ORDER[c[1]])
        high_val = sorted(hand, key=lambda c: (-card_points(c), RANK_ORDER[c[1]]))
        return high_val[0]
    else:
        # Opponent: dump worthless low cards
        by_val = sorted(hand, key=lambda c: (card_points(c), -RANK_ORDER[c[1]]))
        return by_val[0]


def _ai_follow(hand, played_order, played_cards, trump, first_suit,
               trump_in_hand, is_bidder_team, player_num, bidder_num, p1_num, p2_num):
    my_valid = valid_cards(hand, first_suit)
    same_suit_valid = [c for c in my_valid if c[0] == first_suit]

    # Determine current winning card/player
    if played_cards:
        win_idx = trick_winner_idx(played_cards, trump, first_suit)
        cur_winner_num = played_order[win_idx]
        current_best = played_cards[win_idx]
        winner_is_bidder_team = cur_winner_num in (bidder_num, p1_num, p2_num)
    else:
        cur_winner_num = None
        current_best = None
        winner_is_bidder_team = False

    if same_suit_valid:
        # Must follow suit
        if is_bidder_team:
            if winner_is_bidder_team and cur_winner_num != player_num:
                # Teammate winning: save good cards, throw worst of suit
                return max(same_suit_valid, key=lambda c: RANK_ORDER[c[1]])
            can_beat = [c for c in same_suit_valid
                        if beats(c, current_best, trump, first_suit)]
            if can_beat:
                # Play the weakest winning card
                return min(can_beat, key=lambda c: RANK_ORDER[c[1]])
            return max(same_suit_valid, key=lambda c: RANK_ORDER[c[1]])
        else:
            # Opponent
            if not winner_is_bidder_team:
                # Our side winning: preserve our good cards
                return max(same_suit_valid, key=lambda c: RANK_ORDER[c[1]])
            can_beat = [c for c in same_suit_valid
                        if beats(c, current_best, trump, first_suit)]
            if can_beat:
                return min(can_beat, key=lambda c: RANK_ORDER[c[1]])
            return max(same_suit_valid, key=lambda c: RANK_ORDER[c[1]])
    else:
        # Can't follow suit – trump in or discard
        if is_bidder_team:
            if trump_in_hand and not winner_is_bidder_team:
                # Play lowest trump to win
                return max(trump_in_hand, key=lambda c: RANK_ORDER[c[1]])
            # Discard lowest-value card
            by_val = sorted(my_valid, key=lambda c: (card_points(c), -RANK_ORDER[c[1]]))
            return by_val[0]
        else:
            # Dump lowest-value card (don't gift points)
            by_val = sorted(my_valid, key=lambda c: (card_points(c), -RANK_ORDER[c[1]]))
            return by_val[0]


# ─── Game State ───────────────────────────────────────────────────────────────
def create_game(player_name):
    deck = [s + r for s in SUITS for r in RANKS]
    random.shuffle(deck)
    hands = {i + 1: sorted(deck[i * 8:(i + 1) * 8], key=card_sort_key)
             for i in range(6)}
    names = {1: player_name, **{i: AI_NAMES[i] for i in range(2, 7)}}

    gs = {
        'phase': 'bidding',
        'player_name': player_name,
        'player_names': names,
        'hands': hands,
        'bid': 170,
        'last_bidder': 1,
        'passed': {i: False for i in range(1, 7)},
        'pass_count': 0,
        'bid_pos': 0,             # index into BID_ORDER
        'bid_order': [2, 3, 4, 5, 6, 1],
        'bidder': None,
        'trump': None,
        'partner_1': None,
        'partner_2': None,
        'partner_1_player': None,
        'partner_2_player': None,
        'trick_num': 1,
        'trick_leader': None,
        'trick_play_order': [],
        'trick_cards': [],        # cards played this trick in order
        'trick_played_by': [],    # player nums corresponding to trick_cards
        'first_suit': None,
        'tricks_won': {i: [] for i in range(1, 7)},
        'last_trick_info': None,
        'game_scores': {i: 0 for i in range(1, 7)},
        'round_result': None,
        'log': [],
        'message': '',
    }

    # Player 1 opens with 170 – auto-process AI bidding
    gs['log'].append(f"{player_name} opens with the mandatory bid of 170.")
    _process_ai_bidding(gs)
    return gs


def _process_ai_bidding(gs):
    """Advance bidding through AI players until player 1 must act or bidding ends."""
    order = gs['bid_order']
    while gs['pass_count'] < 5:
        pos = gs['bid_pos'] % len(order)
        player = order[pos]
        gs['bid_pos'] += 1

        if gs['passed'][player]:
            continue

        if player == 1:
            # Player 1's turn – stop
            gs['message'] = (
                f"Current bid: {gs['bid']}. "
                f"Bid {gs['bid'] + 10} or pass?"
            )
            return

        # AI's turn
        max_b = ai_max_bid(gs['hands'][player])
        next_bid = gs['bid'] + 10
        name = gs['player_names'][player]

        if next_bid <= max_b and next_bid <= 270:
            gs['bid'] = next_bid
            gs['last_bidder'] = player
            gs['log'].append(f"{name} bids {next_bid}.")
            if gs['bid'] == 270:
                _finish_bidding(gs)
                return
        else:
            gs['passed'][player] = True
            gs['pass_count'] += 1
            gs['log'].append(f"{name} passes.")
            if gs['pass_count'] >= 5:
                _finish_bidding(gs)
                return

    _finish_bidding(gs)


def _finish_bidding(gs):
    gs['bidder'] = gs['last_bidder']
    bidder_name = gs['player_names'][gs['bidder']]
    gs['log'].append(f"─── {bidder_name} wins the bid at {gs['bid']}! ───")

    if gs['bidder'] == 1:
        gs['phase'] = 'set_trump'
        gs['message'] = "You won the bid! Choose your trump suit and two partner cards."
    else:
        # AI won: auto-choose trump/partners
        gs['phase'] = 'set_trump'
        trump = ai_choose_trump(gs['hands'][gs['bidder']])
        p1, p2 = ai_choose_partners(gs['hands'][gs['bidder']], trump)
        _apply_trump_partners(gs, trump, p1, p2, auto=True)


def _apply_trump_partners(gs, trump, partner_1, partner_2, auto=False):
    gs['trump'] = trump
    gs['partner_1'] = partner_1
    gs['partner_2'] = partner_2

    # Find who holds each partner card
    for pnum in range(1, 7):
        hand = gs['hands'][pnum]
        if partner_1 in hand:
            gs['partner_1_player'] = pnum
        if partner_2 in hand:
            gs['partner_2_player'] = pnum

    bidder_name = gs['player_names'][gs['bidder']]
    suit_name = SUIT_NAMES[trump]
    p1_disp = SUIT_SYMBOLS[partner_1[0]] + RANK_DISPLAY[partner_1[1]]
    p2_disp = SUIT_SYMBOLS[partner_2[0]] + RANK_DISPLAY[partner_2[1]]

    if auto:
        gs['log'].append(
            f"{bidder_name} (AI) chose trump: {suit_name}, "
            f"partners: {p1_disp} & {p2_disp}."
        )
    else:
        gs['log'].append(
            f"{bidder_name} chose trump: {suit_name}, "
            f"partners: {p1_disp} & {p2_disp}."
        )

    gs['phase'] = 'playing'
    gs['trick_leader'] = gs['bidder']
    gs['trick_play_order'] = get_trick_play_order(gs['trick_leader'])
    gs['message'] = "Game on! Play your first card."
    gs['log'].append(f"─── Playing begins. {bidder_name} leads. ───")

    # Auto-process AI turns at start of first trick
    _process_trick_auto(gs)


def _process_trick_auto(gs):
    """Drive AI turns until player 1 must act, game ends, or scoring begins."""
    while gs['phase'] == 'playing':
        pos = len(gs['trick_cards'])
        if pos >= 6:
            _finish_trick(gs)
            if gs['phase'] != 'playing':
                return
            # If player 1 leads next trick, stop
            if gs['trick_leader'] == 1:
                gs['message'] = "You lead this trick. Play a card."
                return
            continue

        next_player = gs['trick_play_order'][pos]
        if next_player == 1:
            # Determine valid cards for message
            first_suit = gs['first_suit']
            if first_suit:
                suit_name = SUIT_NAMES[first_suit]
                if any(c[0] == first_suit for c in gs['hands'][1]):
                    gs['message'] = f"You must follow {suit_name}. Play a card."
                else:
                    gs['message'] = "You have no cards of the led suit. Play any card."
            else:
                gs['message'] = "You lead this trick. Play a card."
            return

        # AI turn
        first_suit = gs['first_suit']
        card = ai_play_card(
            player_num=next_player,
            hand=gs['hands'][next_player],
            played_order=list(gs['trick_played_by']),
            played_cards=list(gs['trick_cards']),
            trump=gs['trump'],
            bidder_num=gs['bidder'],
            p1_num=gs['partner_1_player'],
            p2_num=gs['partner_2_player'],
            first_suit=first_suit,
            trick_num=gs['trick_num'],
        )
        gs['hands'][next_player].remove(card)
        if not gs['trick_cards']:
            gs['first_suit'] = card[0]
        gs['trick_cards'].append(card)
        gs['trick_played_by'].append(next_player)

        name = gs['player_names'][next_player]
        disp = SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]]
        gs['log'].append(f"  {name} plays {disp}")


def _finish_trick(gs):
    """Resolve current trick, update state for next."""
    cards = gs['trick_cards']
    first_suit = cards[0][0] if cards else gs['first_suit']

    win_idx = trick_winner_idx(cards, gs['trump'], first_suit)
    winner_num = gs['trick_played_by'][win_idx]
    winner_name = gs['player_names'][winner_num]

    # Save cards to winner
    for card in cards:
        gs['tricks_won'][winner_num].append(card)

    # Points in this trick
    pts = sum(card_points(c) for c in cards)
    win_disp = SUIT_SYMBOLS[cards[win_idx][0]] + RANK_DISPLAY[cards[win_idx][1]]
    gs['log'].append(
        f"Trick {gs['trick_num']}: {winner_name} wins with {win_disp} ({pts} pts)"
    )

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

    # Tally points per player
    player_pts = {}
    for pnum in range(1, 7):
        player_pts[pnum] = sum(card_points(c) for c in gs['tricks_won'][pnum])

    bidder = gs['bidder']
    p1p = gs['partner_1_player']
    p2p = gs['partner_2_player']
    bid = gs['bid']

    # Bidder team total
    team_pts = player_pts[bidder]
    if p1p:
        team_pts += player_pts[p1p]
    if p2p and p2p != p1p:
        team_pts += player_pts[p2p]

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
        gs['game_scores'][bidder] += bid * 2
        if p1p:
            gs['game_scores'][p1p] += bid
        if p2p and p2p != p1p:
            gs['game_scores'][p2p] += bid
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team wins! "
            f"({team_pts} pts ≥ bid {bid}) ───"
        )
    else:
        for pnum in range(1, 7):
            if pnum not in (bidder, p1p, p2p):
                gs['game_scores'][pnum] += bid
        gs['game_scores'][bidder] -= bid
        gs['log'].append(
            f"─── {gs['player_names'][bidder]}'s team falls short! "
            f"({team_pts} pts < bid {bid}) ───"
        )

    gs['message'] = "Round over! See results below."


# ─── Client-facing State ──────────────────────────────────────────────────────
def client_state(gs):
    """Serialize only what the browser needs."""
    if gs is None:
        return {'phase': 'setup'}

    hand1 = gs['hands'].get(1, [])
    first_suit = gs.get('first_suit')
    vc = valid_cards(hand1, first_suit) if gs['phase'] == 'playing' else hand1

    # Build trick display: list of {player_name, player_num, card, card_disp}
    trick_display = []
    for pnum, card in zip(gs['trick_played_by'], gs['trick_cards']):
        trick_display.append({
            'player_num': pnum,
            'player_name': gs['player_names'][pnum],
            'card': card,
            'card_disp': SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]],
            'suit': card[0],
        })

    # Format hand for display
    hand_display = []
    for card in hand1:
        hand_display.append({
            'card': card,
            'disp': SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]],
            'suit': card[0],
            'valid': card in vc,
        })

    # Who's currently playing
    pos = len(gs['trick_cards'])
    current_player = None
    if gs['phase'] == 'playing' and pos < 6:
        order = gs.get('trick_play_order', [])
        if order:
            current_player = order[pos]

    # Partner card display
    p1_disp = (SUIT_SYMBOLS[gs['partner_1'][0]] + RANK_DISPLAY[gs['partner_1'][1]]
               if gs.get('partner_1') else None)
    p2_disp = (SUIT_SYMBOLS[gs['partner_2'][0]] + RANK_DISPLAY[gs['partner_2'][1]]
               if gs.get('partner_2') else None)

    # For scoring phase
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
            'player_pts': {
                gs['player_names'][p]: v for p, v in rr['player_pts'].items()
            },
        }

    game_scores = {gs['player_names'][p]: v
                   for p, v in gs['game_scores'].items()}

    # Last trick info
    last_trick = None
    if gs.get('last_trick_info'):
        lt = gs['last_trick_info']
        last_trick = {
            'winner_name': lt['winner_name'],
            'cards': [
                {
                    'player_name': gs['player_names'][lt['order'][i]],
                    'disp': SUIT_SYMBOLS[c[0]] + RANK_DISPLAY[c[1]],
                    'suit': c[0],
                }
                for i, c in enumerate(lt['cards'])
            ],
        }

    return {
        'phase': gs['phase'],
        'player_name': gs['player_name'],
        'player_names': gs['player_names'],
        'hand': hand_display,
        'bid': gs['bid'],
        'bidder': gs['bidder'],
        'bidder_name': gs['player_names'].get(gs['bidder']) if gs['bidder'] else None,
        'trump': gs.get('trump'),
        'trump_name': SUIT_NAMES.get(gs['trump']) if gs.get('trump') else None,
        'trump_symbol': SUIT_SYMBOLS.get(gs['trump']) if gs.get('trump') else None,
        'partner_1': gs.get('partner_1'),
        'partner_2': gs.get('partner_2'),
        'partner_1_disp': p1_disp,
        'partner_2_disp': p2_disp,
        'partner_1_player': gs.get('partner_1_player'),
        'partner_2_player': gs.get('partner_2_player'),
        'trick_num': gs['trick_num'],
        'trick_display': trick_display,
        'current_player': current_player,
        'trick_leader': gs.get('trick_leader'),
        'first_suit': first_suit,
        'first_suit_name': SUIT_NAMES.get(first_suit) if first_suit else None,
        'passed': gs['passed'],
        'pass_count': gs['pass_count'],
        'message': gs.get('message', ''),
        'log': gs['log'][-20:],   # last 20 log lines
        'round_result': round_result,
        'game_scores': game_scores,
        'last_trick': last_trick,
        'hand_counts': {i: len(gs['hands'].get(i, [])) for i in range(1, 7)},
        'suits': {s: {'name': SUIT_NAMES[s], 'symbol': SUIT_SYMBOLS[s]} for s in SUITS},
        'all_cards': [
            {'card': s + r, 'disp': SUIT_SYMBOLS[s] + RANK_DISPLAY[r], 'suit': s}
            for s in SUITS for r in RANKS
        ],
    }


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/new_game', methods=['POST'])
def new_game():
    data = request.get_json() or {}
    player_name = (data.get('player_name') or 'You').strip()[:20] or 'You'
    game_id = str(uuid.uuid4())
    session['game_id'] = game_id
    gs = create_game(player_name)
    games[game_id] = gs
    return jsonify(client_state(gs))


@app.route('/api/action', methods=['POST'])
def action():
    game_id = session.get('game_id')
    if not game_id or game_id not in games:
        return jsonify({'error': 'No game found. Please start a new game.'}), 400
    gs = games[game_id]
    data = request.get_json() or {}
    act = data.get('action')

    if act == 'bid':
        _handle_bid(gs, data.get('bid_yes', False))

    elif act == 'set_trump':
        trump = data.get('trump', '')
        p1 = data.get('partner_1', '')
        p2 = data.get('partner_2', '')
        err = _validate_trump_partners(gs, trump, p1, p2)
        if err:
            return jsonify({'error': err}), 400
        _apply_trump_partners(gs, trump, p1, p2, auto=False)

    elif act == 'play_card':
        card = data.get('card', '')
        err = _validate_play(gs, card)
        if err:
            return jsonify({'error': err}), 400
        _handle_play_card(gs, card)

    elif act == 'new_round':
        _start_new_round(gs)

    return jsonify(client_state(gs))


def _handle_bid(gs, bid_yes):
    if gs['phase'] != 'bidding':
        return
    name = gs['player_name']
    if bid_yes:
        new_bid = gs['bid'] + 10
        if new_bid > 270:
            new_bid = 270
        gs['bid'] = new_bid
        gs['last_bidder'] = 1
        gs['log'].append(f"{name} bids {new_bid}.")
        if new_bid == 270:
            _finish_bidding(gs)
            return
    else:
        gs['passed'][1] = True
        gs['pass_count'] += 1
        gs['log'].append(f"{name} passes.")
        if gs['pass_count'] >= 5:
            _finish_bidding(gs)
            return
    _process_ai_bidding(gs)


def _validate_trump_partners(gs, trump, p1, p2):
    if trump not in SUITS:
        return "Invalid trump suit. Use s, h, d, or c."
    valid = [s + r for s in SUITS for r in RANKS]
    if p1 not in valid:
        return f"Invalid partner card: {p1}"
    if p2 not in valid:
        return f"Invalid partner card: {p2}"
    if p1 == p2:
        return "Partner cards must be different."
    hand = gs['hands'][1]
    if p1 in hand:
        return f"You already hold {p1} – choose a card you don't have."
    if p2 in hand:
        return f"You already hold {p2} – choose a card you don't have."
    return None


def _validate_play(gs, card):
    if gs['phase'] != 'playing':
        return "Not in playing phase."
    pos = len(gs['trick_cards'])
    order = gs.get('trick_play_order', [])
    if not order or order[pos] != 1:
        return "It is not your turn."
    hand = gs['hands'][1]
    if card not in hand:
        return "You don't hold that card."
    first_suit = gs.get('first_suit')
    vc = valid_cards(hand, first_suit)
    if card not in vc:
        suit_name = SUIT_NAMES.get(first_suit, '')
        return f"You must follow {suit_name} suit if you have it."
    return None


def _handle_play_card(gs, card):
    hand = gs['hands'][1]
    hand.remove(card)
    if not gs['trick_cards']:
        gs['first_suit'] = card[0]
    gs['trick_cards'].append(card)
    gs['trick_played_by'].append(1)

    disp = SUIT_SYMBOLS[card[0]] + RANK_DISPLAY[card[1]]
    gs['log'].append(f"  {gs['player_name']} plays {disp}")

    # Let AI finish the trick and auto-advance
    _process_trick_auto(gs)


def _start_new_round(gs):
    """Keep game scores, re-deal for next round."""
    game_scores = gs['game_scores']
    player_name = gs['player_name']
    names = gs['player_names']
    gs2 = create_game(player_name)
    gs2['game_scores'] = game_scores
    gs2['player_names'] = names
    # Overwrite the game state in-place
    gs.clear()
    gs.update(gs2)


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(debug=False, port=port)
