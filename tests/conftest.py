import mongomock
import pytest

from pyorm import Document, Field, MongoORM


class Player(Document):
    __collection__ = "players"
    name = Field(str, required=True)
    coins = Field(int, default=0)
    inventory = Field(dict, default_factory=dict)
    tags = Field(set, default_factory=set)


@pytest.fixture
def orm():
    with MongoORM(client=mongomock.MongoClient(), database="tests", max_pending=512) as engine:
        yield engine


@pytest.fixture
def players(orm):
    return orm[Player]
