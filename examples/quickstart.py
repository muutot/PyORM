"""Business code uses attributes; the Manager owns loading and persistence."""

import os
import uuid

from pyorm import Document, EmbeddedDocument, EmbeddedField, init, shutdown


class Stats(EmbeddedDocument):
    _autosave = ("level", "exp")
    level = 1
    exp = 0


class Player(Document[str]):
    __collection__ = "players"
    _autosave = ("name", "coins")
    name = ""
    coins = 0
    stats = EmbeddedField(Stats)

    def reward(self, amount: int) -> None:
        self.coins += amount
        self.stats.exp += 10


class PetStats(EmbeddedDocument):
    _autosave = ("level", "exp", "hp")
    level = 1
    exp = 0
    hp = 100


class Pet(Document[str]):
    __collection__ = "pet"  # Independent MongoDB collection.
    _autosave = ("owner_id", "name", "species", "skills", "attributes", "position", "tags")
    owner_id: str | None = None
    name = ""
    species = "cat"
    skills: list[str] = []  # list: one tail value uses an index; batches use $push / $each.
    attributes: dict[str, int] = {}  # dict: nested changes use dotted MongoDB paths.
    position: tuple[int, int] = (0, 0)  # tuple: replace the immutable value by assignment.
    tags: set[str] = set()  # set: deduplicate in cache, then append only new members.
    stats = EmbeddedField(PetStats)

    def train(self, exp: int) -> None:
        self.stats.exp += exp

    def level_up(self) -> None:
        self.stats.level += 1
        self.stats.exp = 0
        self.stats.hp += 20

    def learn_skill(self, skill: str) -> None:
        self.skills.append(skill)

    def set_attack(self, attack: int) -> None:
        self.attributes["attack"] = attack

    def move_to(self, x: int, y: int) -> None:
        self.position = (x, y)

    def mark_rare(self) -> None:
        self.tags.add("rare")


def run_business() -> tuple[Player, Pet]:
    player = Player.load(uuid.uuid4().hex)
    player.name = "Alice"
    player.stats.level = 2
    for _ in range(1000):
        player.reward(1)
    assert Player.load(player.id) is player
    assert player.coins == 1000 and player.stats.exp == 10000

    pet = Pet.load(uuid.uuid4().hex)
    pet.owner_id = player.id
    pet.name = "Mimi"
    pet.species = "cat"
    pet.level_up()
    pet.train(50)
    pet.learn_skill("scratch")
    pet.set_attack(25)
    pet.move_to(10, 20)
    pet.mark_rare()
    assert Pet.load(pet.id) is pet
    assert pet.stats.level == 2 and pet.stats.exp == 50 and pet.stats.hp == 120
    assert pet.skills == ["scratch"] and pet.attributes == {"attack": 25}
    assert pet.position == (10, 20) and pet.tags == {"rare"}
    print(f"Cached player: {player.id}, coins={player.coins}, level={player.stats.level}")
    print(
        f"Cached pet: {pet.id}, owner={pet.owner_id}, name={pet.name}, "
        f"level={pet.stats.level}, exp={pet.stats.exp}, hp={pet.stats.hp}"
    )
    print(
        f"Pet containers: skills={pet.skills}, attributes={pet.attributes}, "
        f"position={pet.position}, tags={pet.tags}"
    )
    return player, pet


def main() -> None:
    # Initialize once at application startup, before running business code.
    uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    init(uri, database="pyorm_examples", flush_interval=0.2, batch_size=100)
    try:
        run_business()
    finally:
        shutdown()  # Application lifecycle hook; drains automatic persistence.
    print("Manager exited: all cached changes acknowledged by MongoDB.")


if __name__ == "__main__":
    main()
