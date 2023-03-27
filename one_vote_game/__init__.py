import time
import random
from otree import settings
from otree.api import *

from .image_utils import encode_image

doc = """
Real-effort tasks. The different tasks are available in task_matrix.py, task_transcription.py, etc.
You can delete the ones you don't need. 
"""


def get_task_module(player):
    """
    This function is only needed for demo mode, to demonstrate all the different versions.
    You can simplify it if you want.
    """
    from . import task_matrix, task_transcription, task_decoding

    session = player.session
    task = session.config.get("task")
    if task == "matrix":
        return task_matrix
    if task == "transcription":
        return task_transcription
    if task == "decoding":
        return task_decoding
    # default
    return task_matrix


class Constants(BaseConstants):
    name_in_url = "real_effort"
    players_per_group = 3
    vote_round = 2
    num_rounds = 2*vote_round - 1

    # endowment = 1000
    tax_rate = 0.4
    redistribution_multiplier = 0.5
    penalty_multiplier = 1 / tax_rate
    default_audit_prob = 0.2
    modified_audit_prob = 0.5
    real_effort_multiplier = 100

    instructions_template = __name__ + "/instructions.html"
    captcha_length = 3



class Subsession(BaseSubsession):
    pass


def creating_session(subsession: Subsession):
    session = subsession.session
    defaults = dict(
        retry_delay=1.0, puzzle_delay=1.0, attempts_per_puzzle=1, max_iterations=None
    )
    session.params = {}
    for param in defaults:
        session.params[param] = session.config.get(param, defaults[param])


class Group(BaseGroup):
    total_tax_paid = models.CurrencyField()
    individual_share = models.CurrencyField()

    treatment = models.StringField()
    total_if_vote = models.IntegerField()
    if_override = models.BooleanField()
    audit_weight = models.FloatField()


class Player(BasePlayer):
    iteration = models.IntegerField(initial=0)
    num_trials = models.IntegerField(initial=0)
    num_correct = models.IntegerField(initial=0)
    num_failed = models.IntegerField(initial=0)

    real_effort_income = models.CurrencyField()
    reported_income = models.CurrencyField(
        min=0, label="What is your pre-tax income?"
    )
    tax_paid = models.CurrencyField()
    if_audited = models.IntegerField()
    if_vote = models.BooleanField(widget=widgets.RadioSelectHorizontal(),
                                  label='Do you want to vote for a higher audit prob?')

# puzzle-specific stuff


class Puzzle(ExtraModel):
    """A model to keep record of all generated puzzles"""

    player = models.Link(Player)
    iteration = models.IntegerField(initial=0)
    attempts = models.IntegerField(initial=0)
    timestamp = models.FloatField(initial=0)
    # can be either simple text, or a json-encoded definition of the puzzle, etc.
    text = models.LongStringField()
    # solution may be the same as text, if it's simply a transcription task
    solution = models.LongStringField()
    response = models.LongStringField()
    response_timestamp = models.FloatField()
    is_correct = models.BooleanField()


def generate_puzzle(player: Player) -> Puzzle:
    """Create new puzzle for a player"""
    task_module = get_task_module(player)
    fields = task_module.generate_puzzle_fields()
    player.iteration += 1
    return Puzzle.create(
        player=player, iteration=player.iteration, timestamp=time.time(), **fields
    )


def get_current_puzzle(player):
    puzzles = Puzzle.filter(player=player, iteration=player.iteration)
    if puzzles:
        [puzzle] = puzzles
        return puzzle


def encode_puzzle(puzzle: Puzzle):
    """Create data describing puzzle to send to client"""
    task_module = get_task_module(puzzle.player)  # noqa
    # generate image for the puzzle
    image = task_module.render_image(puzzle)
    data = encode_image(image)
    return dict(image=data)


def get_progress(player: Player):
    """Return current player progress"""
    return dict(
        num_trials=player.num_trials,
        num_correct=player.num_correct,
        num_incorrect=player.num_failed,
        iteration=player.iteration,
    )


def play_game(player: Player, message: dict):
    """Main game workflow
    Implemented as reactive scheme: receive message from vrowser, react, respond.

    Generic game workflow, from server point of view:
    - receive: {'type': 'load'} -- empty message means page loaded
    - check if it's game start or page refresh midgame
    - respond: {'type': 'status', 'progress': ...}
    - respond: {'type': 'status', 'progress': ..., 'puzzle': data} -- in case of midgame page reload

    - receive: {'type': 'next'} -- request for a next/first puzzle
    - generate new puzzle
    - respond: {'type': 'puzzle', 'puzzle': data}

    - receive: {'type': 'answer', 'answer': ...} -- user answered the puzzle
    - check if the answer is correct
    - respond: {'type': 'feedback', 'is_correct': true|false, 'retries_left': ...} -- feedback to the answer

    If allowed by config `attempts_pre_puzzle`, client can send more 'answer' messages
    When done solving, client should explicitely request next puzzle by sending 'next' message

    Field 'progress' is added to all server responses to indicate it on page.

    To indicate max_iteration exhausted in response to 'next' server returns 'status' message with iterations_left=0
    """
    session = player.session
    my_id = player.id_in_group
    params = session.params
    task_module = get_task_module(player)

    now = time.time()
    # the current puzzle or none
    current = get_current_puzzle(player)

    message_type = message['type']

    # page loaded
    if message_type == 'load':
        p = get_progress(player)
        if current:
            return {
                my_id: dict(type='status', progress=p, puzzle=encode_puzzle(current))
            }
        else:
            return {my_id: dict(type='status', progress=p)}

    if message_type == "cheat" and settings.DEBUG:
        return {my_id: dict(type='solution', solution=current.solution)}

    # client requested new puzzle
    if message_type == "next":
        if current is not None:
            if current.response is None:
                raise RuntimeError("trying to skip over unsolved puzzle")
            if now < current.timestamp + params["puzzle_delay"]:
                raise RuntimeError("retrying too fast")
            if current.iteration == params['max_iterations']:
                return {
                    my_id: dict(
                        type='status', progress=get_progress(player), iterations_left=0
                    )
                }
        # generate new puzzle
        z = generate_puzzle(player)
        p = get_progress(player)
        return {my_id: dict(type='puzzle', puzzle=encode_puzzle(z), progress=p)}

    # client gives an answer to current puzzle
    if message_type == "answer":
        if current is None:
            raise RuntimeError("trying to answer no puzzle")

        if current.response is not None:  # it's a retry
            if current.attempts >= params["attempts_per_puzzle"]:
                raise RuntimeError("no more attempts allowed")
            if now < current.response_timestamp + params["retry_delay"]:
                raise RuntimeError("retrying too fast")

            # undo last updation of player progress
            player.num_trials -= 1
            if current.is_correct:
                player.num_correct -= 1
            else:
                player.num_failed -= 1

        # check answer
        answer = message["answer"]

        if answer == "" or answer is None:
            raise ValueError("bogus answer")

        current.response = answer
        current.is_correct = task_module.is_correct(answer, current)
        current.response_timestamp = now
        current.attempts += 1

        # update player progress
        if current.is_correct:
            player.num_correct += 1
        else:
            player.num_failed += 1
        player.num_trials += 1

        retries_left = params["attempts_per_puzzle"] - current.attempts
        p = get_progress(player)
        return {
            my_id: dict(
                type='feedback',
                is_correct=current.is_correct,
                retries_left=retries_left,
                progress=p,
            )
        }

    raise RuntimeError("unrecognized message from client")


def set_payoffs(group: Group):


    players = group.get_players()
    for p in players:
        p.tax_paid = p.reported_income * Constants.tax_rate #tax paid from every player
        indice = random.choices([1,0], weights=[group.audit_weight, 1-group.audit_weight], k=1) #if audited return 1
        p.if_audited = indice[0]
    group_tax_paid = [p.tax_paid for p in players]
    group.total_tax_paid = sum(group_tax_paid)
    group.individual_share = (group.total_tax_paid * Constants.redistribution_multiplier / Constants.players_per_group)
    for p in players:
        if p.if_audited == 1:
            p.payoff = p.real_effort_income - p.tax_paid + group.individual_share - Constants.penalty_multiplier*Constants.tax_rate*(p.real_effort_income-p.reported_income)
        else:
            p.payoff = p.real_effort_income - p.tax_paid + group.individual_share


def assign_treatment(group: Group):
    players = group.get_players()
    group_if_vote = [p.if_vote for p in players]
    group.total_if_vote = sum(group_if_vote)
    if group.total_if_vote > Constants.players_per_group/2:
        group.treatment = 'EndoYes'
    else:
        group.treatment = 'EndoNo'
    computer_control = random.choices([1,0], weights=[1,1], k=1)
    if computer_control[0] == 1:
        group.if_override = 1
        dice = random.choices([1,0], weights=[1,1], k=1)
        if dice[0] == 1:
            group.treatment = 'ExoYes'
        else:
            group.treatment = 'ExoNo'
    else:
        group.if_override = 0


class Game(Page):
    def is_displayed(player):
        return player.round_number != Constants.vote_round

    live_method = play_game

    timeout_seconds = 30
    @staticmethod
    def js_vars(player: Player):
        return dict(params=player.session.params)

    @staticmethod
    def vars_for_template(player: Player):
        task_module = get_task_module(player)
        return dict(DEBUG=settings.DEBUG,
                    input_type=task_module.INPUT_TYPE,
                    placeholder=task_module.INPUT_HINT)

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        if not timeout_happened and not player.session.params['max_iterations']:
            raise RuntimeError("malicious page submission")
        player.real_effort_income = player.num_correct * Constants.real_effort_multiplier

        if player.group.round_number < Constants.vote_round:
            player.group.audit_weight = Constants.default_audit_prob
        else:
            player.group.treatment = player.group.in_round(Constants.vote_round).treatment
            if (player.group.treatment == 'EndoYes') or (player.group.treatment == 'ExoYes'):
                player.group.audit_weight = Constants.modified_audit_prob
            else:
                player.group.audit_weight = Constants.default_audit_prob
    # def vars_for_template(player: Player):
    #     return {
    #         'round_number': player.round_number if player.round_number<Constants.vote_round else player.round_number-Constants.vote_round,
    #
    #     }


class Contribute(Page):
    form_model = 'player'
    form_fields = ['reported_income']

    def is_displayed(player):
        return player.round_number != Constants.vote_round


class ContributeWaitPage(WaitPage):
    after_all_players_arrive = set_payoffs

    def is_displayed(player):
        return player.round_number != Constants.vote_round

class Vote(Page):
    form_model = 'player'
    form_fields = ['if_vote']

    def is_displayed(player):
        return player.round_number == Constants.vote_round


class VoteWaitPage(WaitPage):
    after_all_players_arrive = assign_treatment

    def is_displayed(player):
        return player.round_number == Constants.vote_round


class Results(Page):
    pass


page_sequence = [Game,Contribute, ContributeWaitPage,Vote, VoteWaitPage, Results]