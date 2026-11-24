"""Public methods preserve the concrete model type across cache and query APIs."""

from concurrent.futures import Future
from typing import assert_type

from pymongo.results import DeleteResult, UpdateResult

from examples.quickstart import Pet, PetStats, Player
from pyorm import Manager, MongoORM, get_manager, init
from pyorm.query import QuerySet, Repository
from pyorm.types import BSONDocument


class SpecialPet(Pet):
    pass


def check_methods(manager: Manager, orm: MongoORM) -> None:
    assert_type(init(check_connection=False), Manager)
    assert_type(get_manager(), Manager)
    assert_type(Player.load("player-1"), Player)
    pet = Pet.load("pet-1")
    assert_type(pet, Pet)
    assert_type(pet.id, str)
    pet.owner_id = Player.load("player-1").id
    assert_type(pet.stats, PetStats)
    assert_type(pet.skills, list[str])
    assert_type(pet.attributes, dict[str, int])
    assert_type(pet.position, tuple[int, int])
    assert_type(pet.tags, set[str])
    assert_type(SpecialPet.load("special-1"), SpecialPet)
    assert_type(Pet.from_mongo({"_id": "pet-1"}), Pet)
    assert_type(PetStats.from_mongo({"level": 1}), PetStats)
    assert_type(manager.get(Pet, "pet-1"), Pet)
    assert_type(manager.release(pet), bool)
    assert_type(manager.release(pet, discard=True), bool)
    assert_type(manager.delete(pet), bool)
    assert_type(orm[Pet], Repository[Pet])
    repo = orm.repository(Pet)
    assert_type(repo, Repository[Pet])
    assert_type(repo.new(id="pet-1"), Pet)
    assert_type(repo.create(id="pet-1"), Pet)
    assert_type(repo.get("pet-1"), Pet | None)
    assert_type(repo.get_many(["pet-1"]), dict[str, Pet])
    assert_type(repo.all(), list[Pet])
    query = repo.filter(name="Mimi").sort("name").skip(0).limit(10)
    assert_type(query, QuerySet[Pet])
    assert_type(query.filter(species="cat"), QuerySet[Pet])
    assert_type(query.first(), Pet | None)
    assert_type(query.all(), list[Pet])
    assert_type(next(iter(query)), Pet)
    assert_type(query.raw({"name": 1}), list[BSONDocument])
    assert_type(query.count(), int)
    assert_type(query.exists(), bool)
    assert_type(repo.filter().update(name="Mimi"), UpdateResult)
    assert_type(repo.filter().delete(), DeleteResult)
    assert_type(repo.update_one({"_id": "pet-1"}, name="Mimi"), Pet | None)
    assert_type(repo.aggregate([{"$count": "total"}]), list[BSONDocument])
    assert_type(repo.ensure_indexes(), list[str])
    assert_type(pet.save(orm=orm), Pet)
    assert_type(pet.save_async(orm=orm), Future[Pet])
    assert_type(pet.reload(), Pet)
    assert_type(pet.delete(), bool)
    assert_type(orm.submit(repo.new, id="pet-2"), Future[Pet])
    with pet.locked() as locked_pet:
        assert_type(locked_pet, Pet)
    with manager as entered_manager:
        assert_type(entered_manager, Manager)
    with orm as entered_orm:
        assert_type(entered_orm, MongoORM)
