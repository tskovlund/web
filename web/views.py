import datetime
import random
import re
from collections import Counter
from math import sqrt
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.core.files import File
from django.core.paginator import Paginator
from django.db.models import (
    Case,
    Count,
    DateTimeField,
    F,
    IntegerField,
    Sum,
    Value,
    When,
)
from django.shortcuts import render
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, UpdateView
from scipy.stats import hypergeom, norm

from games.achievements import ACHIEVEMENTS
from games.models import (
    Card,
    Chug,
    Game,
    GamePlayer,
    GamePlayerStat,
    User,
    filter_season,
)
from games.ranking import RANKINGS, get_ranking_from_key
from games.serializers import GameSerializerWithPlayerStats, UserSerializer

from .forms import UserSettingsForm
from .utils import (
    PlayerCountChooser,
    RankingChooser,
    SeasonChooser,
    round_timedelta,
    updated_query_url,
)

RANKING_PAGE_LIMIT = 15


def get_ranking_url(ranking, user, season):
    rank = ranking.get_rank(user, season)
    if rank is None:
        return None

    page = (rank - 1) // RANKING_PAGE_LIMIT + 1
    return f"/ranking/?" + urlencode(
        {"season": season.number, "type": ranking.key, "page": page}
    )


def get_recent_players(n, min_sample_size=10):
    recent_players = {}
    for game in Game.objects.order_by(F("end_datetime").desc(nulls_last=True)):
        for p in game.players.all():
            if p in recent_players or not p.image:
                continue

            recent_players[
                p
            ] = f"For playing game on {game.date} with {game.players_str()}"

        if len(recent_players) >= min_sample_size:
            break

    recent_players = random.sample(recent_players.items(), min(n, len(recent_players)))
    random.shuffle(recent_players)
    return recent_players


def get_bad_chuggers(n, min_sample_size=10):
    bad_chuggers = {}
    for chug in Chug.objects.filter(duration_in_milliseconds__gte=20 * 1000).order_by(
        F("card__drawn_datetime").desc(nulls_last=True)
    ):
        u = chug.card.get_user()
        if u in bad_chuggers or not u.image:
            continue

        bad_chuggers[
            u
        ] = f"For chugging an ace in {chug.duration_in_milliseconds / 1000} seconds on {chug.card.game.date}"

        if len(bad_chuggers) >= min_sample_size:
            break

    bad_chuggers = random.sample(bad_chuggers.items(), min(n, len(bad_chuggers)))
    random.shuffle(bad_chuggers)
    return bad_chuggers


def index(request):
    BEERS_PER_PLAYER = sum(range(2, 15)) / Game.STANDARD_SIPS_PER_BEER
    total_players = GamePlayer.objects.count()

    context = {
        "total_beers": total_players * BEERS_PER_PLAYER,
        "total_games": Game.objects.all().count(),
        "recent_players": get_recent_players(4),
        "wall_of_shame_players": get_bad_chuggers(4),
        "live_games": Game.objects.filter(
            end_datetime__isnull=True, dnf=False
        ).order_by("-start_datetime")[:5],
    }
    return render(request, "index.html", context)


def about(request):
    return render(request, "about.html")


class MyLoginView(LoginView):
    template_name = "login.html"


class MyPasswordResetView(PasswordResetView):
    template_name = "password_reset.html"


class MyPasswordResetDoneView(PasswordResetDoneView):
    template_name = "password_reset_done.html"


class MyPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "password_reset_confirm.html"


class MyPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "password_reset_complete.html"


class MyLogoutView(LogoutView):
    pass


class PaginatedListView(ListView):
    page_limit = 20

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paginator = Paginator(context["object_list"], self.page_limit)
        page = self.request.GET.get("page")
        object_list = paginator.get_page(page)
        context["object_list"] = object_list

        def page_url(page):
            if page == 1:
                return self.request.path

            return updated_query_url(self.request, {"page": page})

        # We want to preserve other query parameters, when changing pages
        context["paginator_first_url"] = page_url(1)
        context["paginator_last_url"] = page_url(paginator.num_pages)

        if object_list.has_previous():
            context["paginator_previous_url"] = page_url(
                object_list.previous_page_number()
            )

        if object_list.has_next():
            context["paginator_next_url"] = page_url(object_list.next_page_number())

        return context


class GameListView(PaginatedListView):
    model = Game
    template_name = "game_list.html"

    def get_queryset(self):
        season = SeasonChooser(self.request).current
        qs = filter_season(Game.objects, season, should_include_live=True)

        query = self.request.GET.get("query", "")
        parts = re.split(r"[\s,]", query)
        for part in parts:
            part = part.strip()
            if part != "":
                if part[0] == "#":
                    # Filter by hashtag in description
                    qs = qs.filter(description__icontains=part)
                else:
                    # Filter by username
                    qs = qs.filter(players__username__icontains=part)

        # First show live games (but not dnf games),
        # then show all other games sorted by
        # end_datetime if it is not null,
        # otherwise use start_datetime instead
        return qs.distinct().order_by(
            Case(
                When(end_datetime__isnull=True, dnf=False, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            Case(
                When(end_datetime__isnull=True, then="start_datetime"),
                default="end_datetime",
                output_field=DateTimeField(),
            ).desc(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("query", "")
        return context


class GameDetailView(DetailView):
    model = Game
    template_name = "game_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["game_data"] = GameSerializerWithPlayerStats(self.object).data
        context["ordered_gameplayers"] = [
            {"dnf": gp.dnf, "user": UserSerializer(gp.user).data}
            for gp in self.object.ordered_gameplayers()
        ]
        context["card_constants"] = {
            "value_names": dict(Card.VALUES),
            "suit_names": dict(Card.SUITS),
            "suit_symbols": Card.SUIT_SYMBOLS,
        }
        return context


class PlayerListView(PaginatedListView):
    model = User
    template_name = "player_list.html"

    def get_queryset(self):
        query = self.request.GET.get("query", "")
        return (
            User.objects.filter(username__icontains=query)
            .annotate(total_games=Count("gameplayer"))
            .order_by("-total_games")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("query", "")
        return context


class PlayerDetailView(DetailView):
    model = User
    template_name = "player_detail.html"
    # Without this, django will set context["user"] to the viewed user,
    # overriding the current user
    context_object_name = "_unused"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        season = SeasonChooser(self.request).current
        context["stats"] = self.object.stats_for_season(season)

        context["achievements"] = []
        for achievement in ACHIEVEMENTS:
            context["achievements"].append(
                {
                    "achieved": achievement.has_achieved(self.object),
                    "name": achievement.name,
                    "description": achievement.description,
                    "icon_url": f"/static/achievements/{achievement.icon}.svg",
                }
            )

        context["rankings"] = []
        for ranking in RANKINGS:
            context["rankings"].append(
                {
                    "name": ranking.name,
                    "rank": ranking.get_rank(self.object, season),
                    "url": get_ranking_url(ranking, self.object, season),
                }
            )

        games_played = Counter()
        for g in self.object.games.all():
            if g.end_datetime:
                games_played[g.end_datetime.date()] += 1

        HEATMAP_WEEKS = 53

        today = timezone.now().date()
        weekday = today.weekday()

        DAY_NAMES = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        DAY_NAMES = DAY_NAMES[weekday + 1 :] + DAY_NAMES[: weekday + 1]

        series = []
        dates = []
        categories = []
        for d in DAY_NAMES:
            series.append({"name": d, "data": []})
            dates.append([])

        date = today - datetime.timedelta(days=HEATMAP_WEEKS * 7 - 1)
        for i in range(HEATMAP_WEEKS * 7):
            if i % 7 == 0:
                if date.day <= 7:
                    categories.append(date.strftime("%b"))
                else:
                    categories.append("")

            series[i % 7]["data"].append(games_played[date])
            dates[i % 7].append(date)
            date += datetime.timedelta(days=1)

        context["heatmap_data"] = {
            "series": series,
            "categories": categories,
            "dates": dates,
        }

        return context


class UserSettingsView(LoginRequiredMixin, UpdateView):
    model = User
    template_name = "user_settings.html"
    form_class = UserSettingsForm

    def get_object(self):
        return self.request.user

    def get_success_url(self):
        return f"/players/{self.get_object().id}/"

    def form_valid(self, form):
        messages.success(self.request, "Profile updated")

        if form.cleaned_data["new_password"]:
            self.object.set_password(form.cleaned_data["new_password"])
            self.object.save()
            update_session_auth_hash(self.request, self.request.user)

        image_io = form.cleaned_data["image_io"]
        if image_io:
            self.object.image.save(None, File(image_io), save=True)
            image_io.close()

        if form.cleaned_data["image_deleted"]:
            self.object.image.delete(save=True)

        return super().form_valid(form)


class RankingView(PaginatedListView):
    template_name = "ranking.html"
    page_limit = RANKING_PAGE_LIMIT

    def get_ranking(self):
        ranking_type = self.request.GET.get("type")
        return get_ranking_from_key(ranking_type) or RANKINGS[0]

    def get_queryset(self):
        season = SeasonChooser(self.request).current
        ranking = self.get_ranking()
        return ranking.get_qs(season)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        ranking_chooser = RankingChooser(self.request)
        context["ranking_chooser"] = ranking_chooser
        ranking = ranking_chooser.current

        object_list = context["object_list"]
        start_index = object_list.start_index()
        for i, o in enumerate(object_list):
            o.rank = start_index + i
            o.value = ranking.get_value(o)
            o.game = ranking.get_game(o)

        if self.request.user.is_authenticated:
            season = SeasonChooser(self.request).current
            context["user_rank"] = ranking.get_rank(self.request.user, season)
            context["user_rank_url"] = get_ranking_url(
                ranking, self.request.user, season
            )

        return context


SIPS_MEAN = sum(range(2, 14 + 1))
CARD_MEAN = SIPS_MEAN / 13
SIPS_VAR = sum((CARD_MEAN - i) ** 2 for i in range(2, 14 + 1))


class StatsView(TemplateView):
    template_name = "stats.html"

    @classmethod
    def sips_count_distribution(cls, player_count):
        """
        Approximate probability of getting exactly x sips.

        Approximated using a normal distribution where the mean is known,
        but the variance is estimated using a finite population correction.

        Futhermore a continuity correction is used.

        See https://math.stackexchange.com/a/1300566/19750
        """
        mean = SIPS_MEAN + 0.5
        total_cards = 13 * player_count
        fpc = (total_cards - 13) / (total_cards - 1)
        var = SIPS_VAR * fpc
        return norm(mean, sqrt(var)).pdf, f"N({mean}, {var:.2f})"

    @classmethod
    def chug_count_distribution(cls, player_count):
        # Exact probability
        N = 13 * player_count
        K = player_count
        n = 13
        rv = hypergeom(N, K, n)
        return rv.pmf, f"HyperGeometric({N}, {K}, {n})"

    @classmethod
    def combined_distribution(cls, season, dist_f):
        ds = []
        ws = []
        for player_count in range(2, 6 + 1):
            ds.append(dist_f(player_count))
            ws.append(
                GamePlayerStat.get_stats_with_player_count(season, player_count).count()
            )

        total_ws = sum(ws)

        return (
            lambda x: sum(d[0](x) * w / total_ws for d, w in zip(ds, ws)),
            "\n" + " + \n".join(f"{w / total_ws:.2f} * {d[1]}" for d, w in zip(ds, ws)),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        season = SeasonChooser(self.request).current

        chooser = PlayerCountChooser(self.request)
        context["player_count_chooser"] = chooser
        player_count = chooser.current

        games = filter_season(Game.objects, season)
        if player_count != None:
            games = games.annotate(player_count=Count("gameplayer")).filter(
                player_count=player_count
            )

        total_sips = (
            Card.objects.filter(game__id__in=games).aggregate(total_sips=Sum("value"))[
                "total_sips"
            ]
            or 0
        )

        total_duration = Game.add_durations(games).aggregate(
            total_duration=Sum("duration")
        )["total_duration"]

        context["game_stats"] = {
            "total_games": games.count(),
            "total_dnf": games.filter(dnf=True).count(),
            "total_sips": total_sips,
            "total_beers": total_sips / 14,
            "total_duration": str(round_timedelta(total_duration)),
        }

        stat_types = {
            "sips_data": (
                GamePlayerStat.get_sips_distribution(season, player_count),
                self.sips_count_distribution,
            ),
            "chugs_data": (
                GamePlayerStat.get_chugs_distribution(season, player_count),
                self.chug_count_distribution,
            ),
        }

        for name, (stats, prob_f) in stat_types.items():
            xs = []
            ys = []
            probs = []
            dist_str = None
            if stats.exists():
                d = {s["value"]: s["value__count"] for s in stats}
                if player_count == None:
                    dist, dist_str = self.combined_distribution(season, prob_f)
                else:
                    dist, dist_str = prob_f(player_count)

                for x in range(stats.first()["value"], stats.last()["value"] + 1):
                    xs.append(x)
                    ys.append(d.get(x, 0))

                    probs.append(dist(x))

            context[name] = {
                "xs": xs,
                "ys": ys,
                "total_ys": sum(ys),
                "probs": probs,
                "probs_exact": name == "chugs_data",
                "dist_str": dist_str,
            }

        return context
