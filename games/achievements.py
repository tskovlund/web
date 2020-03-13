ACHIEVEMENTS = []


class AchievementMetaClass(type):
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        if name != "Achievement":
            ACHIEVEMENTS.append(self)


class Achievement(metaclass=AchievementMetaClass):
    __slots__ = ["name", "description", "icon"]

    @staticmethod
    def has_achieved(user):
        raise NotImplementedError


class DNFAchievement(Achievement):
    name = "DNF"
    description = "Participated in a game that completed, where you didn't"
    icon = "coffin"

    def has_achieved(user):
        return (
            user.gameplayer_set.filter(dnf=True)
            .filter(game__dnf=False, game__end_datetime__isnull=False)
            .exists()
        )


class Top10Achievement(Achievement):
    name = "Top 10"
    description = "Placed top 10 total sips in a season"
    icon = "trophy-cup"

    def has_achieved(user):
        return (
            user.gameplayer_set.filter(dnf=True)
            .filter(game__dnf=False, game__end_datetime__isnull=False)
            .exists()
        )


"""
class LateGameAchievement(Achievement):
    name = "Late Game"
    description = "Participated in a game that started before 04:00 but ended after"
    icon = "night-sleep"

    def has_achieved(user):
        return (
            user.gameplayer_set.filter(dnf=True)
            .filter(game__dnf=False, game__end_datetime__isnull=False)
            .exists()
        )
"""
