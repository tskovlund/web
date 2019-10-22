import os
import pytz
from django.db import models
from django.db.models import Q, Sum
from django.contrib.auth.models import AbstractUser, UserManager
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.utils import timezone
from django.utils.html import mark_safe
from django.urls import reverse
import datetime
from tqdm import tqdm
from PIL import Image
from .seed import shuffle_with_seed


class CaseInsensitiveUserManager(UserManager):
    def get_by_natural_key(self, username):
        return self.get(**{f"{self.model.USERNAME_FIELD}__iexact": username})


def get_user_image_name(user, filename=None):
    return f"user_images/{user.id}.png"


def filter_season(qs, season, key=None, should_include_live=False):
    if key:
        key += "__"
    else:
        key = ""
    key += "end_datetime"

    q = Q(**{f"{key}__gte": season.start_datetime, f"{key}__lte": season.end_datetime})

    includes_live = season == all_time_season or Season.current_season() == season
    if includes_live and should_include_live:
        q |= Q(**{f"{key}__isnull": True})

    return qs.filter(q)


class PlayerStat(models.Model):
    class Meta:
        unique_together = [("user", "season_number")]

    user = models.ForeignKey("User", on_delete=models.CASCADE)
    season_number = models.PositiveIntegerField()

    total_games = models.PositiveIntegerField(default=0)
    total_time_played_seconds = models.FloatField(default=0)

    total_sips = models.PositiveIntegerField(default=0)

    best_game = models.ForeignKey(
        "Game", on_delete=models.CASCADE, null=True, related_name="+"
    )
    worst_game = models.ForeignKey(
        "Game", on_delete=models.CASCADE, null=True, related_name="+"
    )
    best_game_sips = models.PositiveIntegerField(null=True)
    worst_game_sips = models.PositiveIntegerField(null=True)

    total_chugs = models.PositiveIntegerField(default=0)

    fastest_chug = models.ForeignKey(
        "Chug", on_delete=models.CASCADE, null=True, related_name="+"
    )

    average_chug_time_seconds = models.FloatField(null=True)

    @classmethod
    def update_on_game_finished(cls, game):
        season = game.get_season()
        for s in [season, all_time_season]:
            for player in game.players.all():
                ps, _ = PlayerStat.objects.get_or_create(
                    user=player, season_number=s.number
                )
                ps.update_from_new_game(game)

    @classmethod
    def recalculate_all(cls):
        for season_number in tqdm(range(Season.current_season().number + 1)):
            cls.recalculate_season(Season(season_number))

    @classmethod
    def recalculate_season(cls, season):
        for user in tqdm(User.objects.all()):
            ps, _ = PlayerStat.objects.get_or_create(
                user=user, season_number=season.number
            )
            ps.recalculate()

    def recalculate(self):
        for f in self._meta.fields:
            if f.default != models.fields.NOT_PROVIDED:
                setattr(self, f.name, f.default)
            elif f.null:
                setattr(self, f.name, None)

        gameplayers = filter_season(
            self.user.gameplayer_set, self.season, key="game"
        ).filter(game__official=True, game__dnf=False)

        for gp in gameplayers:
            self.update_from_new_game(gp.game)

    def update_from_new_game(self, game):
        if not game.official or game.dnf:
            return

        gp = game.gameplayer_set.get(user=self.user)
        if gp.dnf:
            return

        self.total_games += 1

        player_index = gp.position
        duration = game.get_duration()
        if duration:
            self.total_time_played_seconds += duration.total_seconds()

        if self.average_chug_time_seconds:
            total_chug_time = self.total_chugs * self.average_chug_time_seconds
        else:
            total_chug_time = 0
        game_sips = 0
        for i, c in enumerate(game.ordered_cards()):
            if i % game.players.count() == player_index:
                game_sips += c.value
                if hasattr(c, "chug"):
                    chug_time = c.chug.duration
                    self.total_chugs += 1
                    total_chug_time += chug_time.total_seconds()
                    if not self.fastest_chug or chug_time < self.fastest_chug.duration:
                        self.fastest_chug = c.chug

        self.total_sips += game_sips

        if not self.best_game or game_sips > self.best_game_sips:
            self.best_game = game
            self.best_game_sips = game_sips

        if not self.worst_game or game_sips < self.worst_game_sips:
            self.worst_game = game
            self.worst_game_sips = game_sips

        if self.total_chugs > 0:
            self.average_chug_time_seconds = total_chug_time / self.total_chugs

        self.save()

    @property
    def season(self):
        if self.season_number == 0:
            return all_time_season
        return Season(self.season_number)

    @property
    def total_time_played(self):
        return datetime.timedelta(seconds=self.total_time_played_seconds)

    @property
    def total_beers(self):
        return self.total_sips / Game.STANDARD_SIPS_PER_BEER

    @property
    def approx_ects(self):
        HOURS_PER_ECTS = 28
        hours_played = self.total_time_played_seconds / (60 * 60)
        return hours_played / HOURS_PER_ECTS

    @property
    def approx_money_spent(self):
        AVERAGE_BEER_PRICE_DKK = 10
        cost = self.total_beers * AVERAGE_BEER_PRICE_DKK
        return f"{int(cost)} DKK"

    @property
    def average_game_sips(self):
        if self.total_games == 0:
            return None
        return round(self.total_sips / self.total_games, 1)

    @property
    def average_chug_time(self):
        if not self.average_chug_time_seconds:
            return None
        return datetime.timedelta(seconds=self.average_chug_time_seconds)


class User(AbstractUser):
    IMAGE_SIZE = (156, 262)

    objects = CaseInsensitiveUserManager()

    class Meta:
        ordering = ("username",)

    email = models.EmailField(blank=True)
    image = models.ImageField(upload_to=get_user_image_name, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        super().save()

        # Ensure that the image file is always saved
        # at the location governed by get_user_image_name
        # and deleted when the image is removed.
        expected_image_name = get_user_image_name(self)
        expected_image_path = self.image.storage.path(expected_image_name)
        if self.image:
            if self.image.path != expected_image_path:
                os.rename(self.image.path, expected_image_path)
                self.image.name = expected_image_name
                super().save()

            image = Image.open(expected_image_path)
            thumb = image.resize(self.IMAGE_SIZE)
            thumb.save(expected_image_path)
        else:
            try:
                os.remove(expected_image_path)
            except FileNotFoundError:
                pass

    def total_game_count(self):
        return self.gameplayer_set.count()

    def current_season_game_count(self):
        season = Season.current_season()
        return filter_season(self.gameplayer_set, season, key="game").count()

    def stats_for_season(self, season):
        return PlayerStat.objects.get_or_create(user=self, season_number=season.number)[
            0
        ]

    def image_url(self):
        if self.image:
            return self.image.url
        else:
            return static("user.png")

    def get_absolute_url(self):
        return reverse("player_detail", args=[self.id])

    def get_ranked_games(self):
        return self.gameplayer_set.filter(
            dnf=False, game__dnf=False, game__official=True
        )

    def get_games_with_total_sips(self, season=None):
        if not season:
            season = all_time_season

        qs = filter_season(self.get_ranked_games, season, key="game")
        return qs.values("game").annotate(
            total_sips=Sum("all_cards_2__value")
        )


class Season:
    FIRST_SEASON_START = datetime.date(2013, 1, 1)

    def __init__(self, number):
        self.number = number

    def __str__(self):
        return f"Season {self.number}"

    def __eq__(self, other):
        return self.number == other.number

    def __neq__(self, other):
        return self.number != other.number

    @property
    def start_datetime(self):
        extra_half_years = self.number - 1
        date = self.FIRST_SEASON_START
        date = date.replace(year=date.year + extra_half_years // 2)
        if extra_half_years % 2 == 1:
            date = date.replace(month=7)

        return pytz.utc.localize(datetime.datetime(date.year, date.month, date.day))

    @property
    def end_datetime(self):
        return Season(self.number + 1).start_datetime - datetime.timedelta(
            microseconds=1
        )

    @classmethod
    def season_from_date(cls, date):
        year_diff = date.year - cls.FIRST_SEASON_START.year
        season_number = year_diff * 2 + 1
        if date.month >= 7:
            season_number += 1

        return Season(season_number)

    @classmethod
    def current_season(cls):
        return cls.season_from_date(datetime.date.today())

    @classmethod
    def is_valid_season_number(cls, number):
        try:
            number = int(number)
        except (ValueError, TypeError):
            return False

        return 1 <= number <= Season.current_season().number


class _AllTimeSeason:
    number = 0
    start_datetime = Season(1).start_datetime
    end_datetime = Season.current_season().end_datetime

    def __str__(self):
        return "All time"


all_time_season = _AllTimeSeason()


class Game(models.Model):
    TOTAL_ROUNDS = 13
    STANDARD_SIPS_PER_BEER = 14

    class Meta:
        ordering = ("-end_datetime",)

    """
    There are 4 kinds of games:

    - Unfinished games:
        - end_datetime          == None
        - start_datetime        != None
        - cards__drawn_datetime != None

    - Finished games after 2015-07-02 (e.g. ):
        - end_datetime          != None
        - start_datetime        != None
        - cards__drawn_datetime != None
        - example: 1800

    - Finished games between 2014-04-09 and 2015-7-2:
        - end_datetime          != None
        - start_datetime        != None
        - cards__drawn_datetime == None
        - example: 279

    - Finished games between 2013-02-01 and 2014-4-4:
        - end_datetime          != None
        - start_datetime        == None
        - cards__drawn_datetime == None
        - example: 119

    This means that if start_datetime is missing,
    then so are this card draw times.
    """

    players = models.ManyToManyField(User, through="GamePlayer", related_name="games")
    start_datetime = models.DateTimeField(blank=True, null=True, default=timezone.now)
    end_datetime = models.DateTimeField(blank=True, null=True)
    sips_per_beer = models.PositiveSmallIntegerField(default=STANDARD_SIPS_PER_BEER)
    description = models.CharField(max_length=1000, blank=True)
    official = models.BooleanField(default=True)
    dnf = models.BooleanField(default=False)

    def __str__(self):
        datetime = self.end_datetime or self.start_datetime
        return f"{datetime}: {self.players_str()}"

    @property
    def is_completed(self):
        return self.end_datetime is not None

    @property
    def has_ended(self):
        return self.is_completed or self.dnf

    @property
    def is_live(self):
        return not self.is_completed and not self.dnf

    def get_last_activity_time(self):
        if self.end_datetime:
            return self.end_datetime

        cards = self.ordered_cards()
        if len(cards) > 0:
            return cards.last().drawn_datetime

        return self.start_datetime

    def get_season(self):
        if not self.has_ended:
            return None

        return Season.season_from_date(self.get_last_activity_time())

    def season_number_str(self):
        season = self.get_season()
        if not season:
            return "-"
        return str(season.number)

    def get_duration(self):
        if self.dnf:
            return self.get_last_activity_time() - self.start_datetime

        if not (self.start_datetime and self.end_datetime):
            return None

        return self.end_datetime - self.start_datetime

    def duration_str(self):
        if not self.start_datetime:
            return "?"

        duration = self.get_duration()
        if not duration:
            duration = timezone.now() - self.start_datetime

        return datetime.timedelta(seconds=round(duration.total_seconds()))

    def end_str(self):
        if self.dnf:
            return "DNF"

        if not self.end_datetime:
            return "Live"

        return timezone.localtime(self.end_datetime).strftime("%B %d, %Y %H:%M")

    def ordered_gameplayers(self):
        return self.gameplayer_set.order_by("position")

    def ordered_players(self):
        return [p.user for p in self.ordered_gameplayers()]

    def players_str(self):
        return ", ".join(p.username for p in self.ordered_players())

    def ordered_cards(self):
        return self.cards.order_by("index")

    def cards_by_round(self):
        n = self.players.count()
        cards = self.ordered_cards()
        for i in range(self.TOTAL_ROUNDS):
            round_cards = cards[i * n : (i + 1) * n]
            yield list(round_cards) + [None] * (n - len(round_cards))

    def ordered_chugs(self):
        return (c.chug for c in self.cards.filter(chug__isnull=False))

    def get_total_card_count(self):
        return self.players.count() * len(Card.VALUES)

    def get_turn_durations(self):
        prev_datetime = self.start_datetime
        for c in self.ordered_cards():
            if c.drawn_datetime is None:
                return

            if prev_datetime is not None:
                yield c.drawn_datetime - prev_datetime

            prev_datetime = c.drawn_datetime

    def get_player_stats(self):
        # Note that toal_drawn and total_done,
        # can differ for one player, if the game hasn't ended.
        def div_or_none(a, b):
            if a is None or not b:
                return None
            return a / b

        n = self.players.count()
        total_sips = [0] * n
        total_drawn = [0] * n
        last_sip = None
        for i, c in enumerate(self.ordered_cards()):
            total_sips[i % n] += c.value
            total_drawn[i % n] += 1
            last_sip = (i % n, c.value)

        first_card = self.ordered_cards().first()
        if first_card and first_card.drawn_datetime:
            total_times = [datetime.timedelta()] * n
            total_done = [0] * n
            for i, dt in enumerate(self.get_turn_durations()):
                total_times[i % n] += dt
                total_done[i % n] += 1
        else:
            total_times = [None] * n
            total_done = [None] * n

        for i in range(n):
            full_beers = total_sips[i] // self.sips_per_beer
            extra_sips = total_sips[i] % self.sips_per_beer

            if not self.start_datetime:
                time_per_sip = None
            elif last_sip and last_sip[0] == i and not self.is_completed:
                time_per_sip = div_or_none(
                    total_times[i], (total_sips[i] - last_sip[1])
                )
            else:
                time_per_sip = div_or_none(total_times[i], total_sips[i])

            yield {
                "total_sips": total_sips[i],
                "sips_per_turn": div_or_none(total_sips[i], total_drawn[i]),
                "full_beers": full_beers,
                "extra_sips": extra_sips,
                "total_time": total_times[i],
                "time_per_turn": div_or_none(total_times[i], total_done[i]),
                "time_per_sip": time_per_sip,
            }

    def get_absolute_url(self):
        return reverse("game_detail", args=[self.id])


class GamePlayer(models.Model):
    class Meta:
        unique_together = [("game", "user", "position")]
        ordering = ("position",)

    game = models.ForeignKey(Game, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    dnf = models.BooleanField(default=False)


class Card(models.Model):
    class Meta:
        unique_together = [("game", "value", "suit"), ("game", "index")]
        ordering = ("index",)

    VALUES = [
        *zip(range(2, 11), map(str, range(2, 11))),
        (11, "Jack"),
        (12, "Queen"),
        (13, "King"),
        (14, "Ace"),
    ]

    SUITS = [
        ("S", "Spades"),
        ("C", "Clubs"),
        ("H", "Hearts"),
        ("D", "Diamonds"),
        ("A", "Carls"),
        ("I", "Heineken"),
    ]

    game = models.ForeignKey("Game", on_delete=models.CASCADE, related_name="cards")
    index = models.PositiveSmallIntegerField()
    value = models.SmallIntegerField(choices=VALUES)
    suit = models.CharField(max_length=1, choices=SUITS)
    drawn_datetime = models.DateTimeField(blank=True, null=True)
    drawn_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="all_cards"
    )
    drawn_by_2 = models.ForeignKey(
        GamePlayer, on_delete=models.CASCADE, related_name="all_cards_2"
    )

    def save(self, *args, **kwargs):
        if not hasattr(self, "drawn_by"):
            self.drawn_by = self.game.ordered_players()[
                self.index % self.game.players.count()
            ]
            self.drawn_by_2 = self.ordered_gameplayers()[
                self.index % self.game.players.count()
            ]
        super().save(*args, **kwargs)

    @classmethod
    def get_ordered_cards_for_players(cls, player_count):
        for suit, _ in Card.SUITS[:player_count]:
            for value, _ in Card.VALUES:
                yield value, suit

    @classmethod
    def get_shuffled_deck(cls, player_count, seed):
        cards = list(cls.get_ordered_cards_for_players(player_count))
        shuffle_with_seed(cards, seed)
        return cards

    def __str__(self):
        return f"{self.value} {self.suit}"

    def value_str(self):
        return dict(self.VALUES)[self.value]

    def suit_str(self):
        return dict(self.SUITS)[self.suit]

    def card_str(self):
        return f"{self.value_str()} of {self.suit_str()}"

    SUIT_SYMBOLS = {
        "S": ("♠", "black"),
        "C": ("♣", "black"),
        "H": ("♥", "red"),
        "D": ("♦", "red"),
        "A": ("☘", "green"),
        "I": ("🟊", "green"),
    }

    def suit_symbol(self):
        return self.SUIT_SYMBOLS[self.suit]

    def colored_suit_symbol(self):
        symbol, color = self.SUIT_SYMBOLS[self.suit]
        return mark_safe(f'<span style="color: {color};">{symbol}</span>')


class Chug(models.Model):
    VALUE = 14

    card = models.OneToOneField("Card", on_delete=models.CASCADE, related_name="chug")
    duration_in_milliseconds = models.PositiveIntegerField()

    @property
    def duration(self):
        return datetime.timedelta(milliseconds=self.duration_in_milliseconds)

    def duration_str(self):
        return str(self.duration)

    def __str__(self):
        return f"{self.card.drawn_by}: {self.card} ({self.duration_str()})"
