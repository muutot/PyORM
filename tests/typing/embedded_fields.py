"""Checked with Pyright: inferred and explicit embedded descriptor types."""

from typing import assert_type

from pyorm import Document, EmbeddedDocument, EmbeddedField, Field, ListField


class Stats(EmbeddedDocument):
    _autosave = ("level", "exp")
    level = 1
    exp = 0


class Player(Document[str]):
    _autosave = ("name",)
    name = ""
    stats = EmbeddedField(Stats)
    explicit_stats = EmbeddedField[Stats](Stats)
    custom_stats = EmbeddedField(Stats, default_factory=lambda: Stats())
    history = ListField(EmbeddedField(Stats))
    coins = Field(int, default=0)


class OptionalPlayer(Document[str]):
    _autosave = ("coins", "stats")
    coins: int | None = None
    stats: Stats | None = None


def check_optional(player: OptionalPlayer) -> None:
    assert_type(player.coins, int | None)
    assert_type(player.stats, Stats | None)
    player.coins = 5
    player.stats = Stats()


def check_player(player: Player) -> None:
    assert_type(player.stats, Stats)
    assert_type(player.explicit_stats, Stats)
    assert_type(player.custom_stats, Stats)
    assert_type(Player.stats, EmbeddedField[Stats])
    assert_type(player.history, list[Stats])
    assert_type(player.coins, int)
    player.stats.exp += 10
    player.explicit_stats.level = 2
    player.history.append(Stats())
    player.stats = Stats()
