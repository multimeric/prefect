import json
import re
import uuid
from asyncio import current_task, get_event_loop
from enum import Enum
from typing import Union

import pendulum
import pydantic
import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    create_async_engine,
)
from sqlalchemy.schema import MetaData
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import as_declarative, declared_attr, sessionmaker
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from prefect import settings

camel_to_snake = re.compile(r"(?<!^)(?=[A-Z])")

ENGINES = {}
SESSION_FACTORIES = {}


def get_engine(connection_url: str = None, echo: bool = None) -> sa.engine.Engine:
    """Retrieves an async SQLAlchemy engine.

    A new engine is created for each event loop and cached, so that
    engines are not shared across loops.

    Args:
        connection_url ([type], optional): The database connection string.
            Defaults to the value in Prefect's settings.
        echo ([type], optional): [description]. Whether to echo SQL sent
            to the database. Defaults to the value in Prefect's settings.

    Returns:
        sa.engine.Engine: a SQLAlchemy engine
    """
    if connection_url is None:
        connection_url = settings.orion.database.connection_url.get_secret_value()
    if echo is None:
        echo = settings.orion.database.echo

    loop = get_event_loop()
    cache_key = (loop, connection_url, echo)
    if cache_key not in ENGINES:
        ENGINES[cache_key] = create_async_engine(connection_url, echo=echo)
    return ENGINES[cache_key]


def get_session_factory(
    bind: Union[sa.engine.Engine, sa.engine.Connection] = None,
) -> sa.ext.asyncio.scoping.async_scoped_session:
    """Retrieves a SQLAlchemy session factory for the provided bind.
    The session factory is cached for each event loop.

    Args:
        engine (Union[sa.engine.Engine, sa.engine.Connection], optional): An
            async SQLAlchemy engine or connection. If none is
            provided, `get_engine()` is called to recover one.

    Returns:
        sa.ext.asyncio.scoping.async_scoped_session: an async scoped session factory
    """
    if bind is None:
        bind = get_engine()

    loop = get_event_loop()
    cache_key = (loop, bind)
    if cache_key not in SESSION_FACTORIES:
        # create session factory
        session_factory = sessionmaker(
            bind,
            future=True,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        # create session factory with async scoping
        SESSION_FACTORIES[cache_key] = async_scoped_session(
            session_factory, scopefunc=current_task
        )

    return SESSION_FACTORIES[cache_key]


@listens_for(sa.engine.Engine, "engine_connect", once=True)
def setup_sqlite(conn, named=True):
    """The first time a connection is made to a sqlite engine:
    - we check if it's an in-memory sqlite database and create all Orion tables
      as a convenience
    - we enable sqlite foreign keys
    to the user."""
    if conn.engine.url.get_backend_name() == "sqlite":
        # enable foreign keys
        cursor = conn.connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

        # create tables
        if conn.engine.url.database in (":memory:", None):
            Base.metadata.create_all(conn.engine)


@compiles(sa.JSON, "postgresql")
def compile_json_as_jsonb_postgres(type_, compiler, **kw):
    """Compiles the generic SQLAlchemy JSON type as JSONB on postgres"""
    return "JSONB"


class UUIDDefault(FunctionElement):
    """
    Platform-independent UUID default generator.
    Note the actual functionality for this class is speficied in the
    `compiles`-decorated functions below
    """

    name = "uuid_default"


@compiles(UUIDDefault, "postgresql")
def visit_custom_uuid_default_for_postgres(element, compiler, **kwargs):
    """
    Generates a random UUID in Postgres; requires the pgcrypto extension.
    """

    return "(GEN_RANDOM_UUID())"


@compiles(UUIDDefault)
def visit_custom_uuid_default(element, compiler, **kwargs):
    """
    Generates a random UUID in other databases (SQLite) by concatenating
    bytes in a way that approximates a UUID hex representation. This is
    sufficient for our purposes of having a random client-generated ID
    that is compatible with a UUID spec.
    """

    return """
    (
        lower(hex(randomblob(4))) 
        || '-' 
        || lower(hex(randomblob(2))) 
        || '-4' 
        || substr(lower(hex(randomblob(2))),2) 
        || '-' 
        || substr('89ab',abs(random()) % 4 + 1, 1) 
        || substr(lower(hex(randomblob(2))),2) 
        || '-' 
        || lower(hex(randomblob(6)))
    )
    """


class Timestamp(TypeDecorator):
    """TypeDecorator that ensures that timestamps have a timezone.

    For SQLite, all timestamps are converted to UTC (since they are stored
    as naive timestamps) and recovered as UTC.

    Note: this should still be instantiated as Timestamp(timezone=True)
    """

    impl = sa.TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        else:
            if value.tzinfo is None:
                raise ValueError("Timestamps must have a timezone.")
            elif dialect.name == "sqlite":
                return pendulum.instance(value).in_timezone("UTC")
            else:
                return value

    def process_result_value(self, value, dialect):
        # retrieve timestamps in their native timezone (or UTC)
        if value is not None:
            return pendulum.instance(value).in_timezone("utc")


class Pydantic(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_type):
        super().__init__()
        self._pydantic_type = pydantic_type

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # parse the value to ensure it complies with the schema
        # (this will raise validation errors if not)
        value = pydantic.parse_obj_as(self._pydantic_type, value)
        # sqlalchemy requires the bind parameter's value to be a python-native
        # collection of JSON-compatible objects. we achieve that by dumping the
        # value to a json string using the pydantic JSON encoder and re-parsing
        # it into a python-native form.
        return json.loads(json.dumps(value, default=pydantic.json.pydantic_encoder))

    def process_result_value(self, value, dialect):
        if value is not None:
            # load the json object into a fully hydrated typed object
            return pydantic.parse_obj_as(self._pydantic_type, value)


class UUID(TypeDecorator):
    """
    Platform-independent UUID type.

    Uses PostgreSQL's UUID type, otherwise uses
    CHAR(36), storing as stringified hex values with
    hyphens.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgresUUID())
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        elif dialect.name == "postgresql":
            return str(value)
        elif isinstance(value, uuid.UUID):
            return str(value)
        else:
            return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            if not isinstance(value, uuid.UUID):
                value = uuid.UUID(value)
            return value


class Now(FunctionElement):
    """
    Platform-independent "now" generator
    """

    name = "now"


@compiles(Now, "sqlite")
def sqlite_microseconds_current_timestamp(element, compiler, **kwargs):
    """
    Generates the current timestamp for SQLite

    We need to add three zeros to the string representation
    because SQLAlchemy uses a regex expression which is expecting
    6 decimal places, but SQLite only stores milliseconds. This
    causes SQLAlchemy to interpret 01:23:45.678 as if it were
    01:23:45.000678. By forcing SQLite to store an extra three
    0's, we work around his issue.

    Note this only affects timestamps that we ask SQLite to issue
    in SQL (like the default value for a timestamp column); not
    datetimes provided by SQLAlchemy itself.
    """
    return "strftime('%Y-%m-%d %H:%M:%f000+00:00', 'now')"


@compiles(Now)
def now(element, compiler, **kwargs):
    """
    Generates the current timestamp in standard SQL
    """
    return "CURRENT_TIMESTAMP"


# define naming conventions for our Base class to use
# sqlalchemy will use the following templated strings
# to generate the names of indices, constraints, and keys
#
# more information on this templating and available
# customization can be found here
# https://docs.sqlalchemy.org/en/14/core/metadata.html#sqlalchemy.schema.MetaData
#
# this also allows us to avoid having to specify names explicitly
# when using sa.ForeignKey.use_alter = True
# https://docs.sqlalchemy.org/en/14/core/constraints.html
base_metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_N_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


@as_declarative(metadata=base_metadata)
class Base(object):
    """
    Base SQLAlchemy model that automatically infers the table name
    and provides ID, created, and updated columns
    """

    # required in order to access columns with server defaults
    # or SQL expression defaults, subsequent to a flush, without
    # triggering an expired load
    #
    # this allows us to load attributes with a server default after
    # an INSERT, for example
    #
    # https://docs.sqlalchemy.org/en/14/orm/extensions/asyncio.html#preventing-implicit-io-when-using-asyncsession
    __mapper_args__ = {"eager_defaults": True}

    @declared_attr
    def __tablename__(cls):
        """
        By default, turn the model's camel-case class name
        into a snake-case table name. Override by providing
        an explicit `__tablename__` class property.
        """
        return camel_to_snake.sub("_", cls.__name__).lower()

    id = Column(
        UUID(),
        primary_key=True,
        server_default=UUIDDefault(),
        default=uuid.uuid4,
    )
    created = Column(
        Timestamp(timezone=True),
        nullable=False,
        server_default=Now(),
        default=lambda: pendulum.now("UTC"),
    )
    updated = Column(
        Timestamp(timezone=True),
        nullable=False,
        index=True,
        server_default=Now(),
        default=lambda: pendulum.now("UTC"),
        onupdate=Now(),
    )


async def create_db(engine=None):
    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine=None):
    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)