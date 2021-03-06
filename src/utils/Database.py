import pymongo

from dynaconf import settings


class MetaClass(type):
    _instance = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instance:
            cls._instance[cls] = super(
                MetaClass, cls).__call__(*args, **kwargs)
            return cls._instance[cls]


class Database(metaclass=MetaClass):
    """ MongoDB connector """

    def instance(driver='Mongo'):
        client = pymongo.MongoClient(
            host=settings.DATABASE.Mongo.URL,
        )

        return client.get_database()
