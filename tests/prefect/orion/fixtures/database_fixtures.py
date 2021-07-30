import pendulum
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from prefect.orion.utilities.database import Base
from prefect import settings
from prefect.orion import models, schemas


@pytest.fixture(scope="session")
async def database_engine():
    """Creates an in memory sqlite database for use in testing. For performance,
    the database is created only once, at the beginning of testing. Subsequent
    sessions roll themselves back at the end of each test to restore the
    database.
    """
    # create an in memory db engine
    engine = create_async_engine(
        settings.orion.database.connection_url.get_secret_value(),
        echo=settings.orion.database.echo,
    )

    # populate database tables
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def database_session(database_engine):
    """Test database session. At the end of each test, the session is rolled
    back to restore the original database condition and avoid carrying over
    state.
    """
    async with AsyncSession(
        database_engine, future=True, expire_on_commit=False
    ) as session:
        # open transaction
        async with session.begin():
            try:
                yield session
            finally:
                await session.rollback()


@pytest.fixture
async def flow(database_session):
    model = await models.flows.create_flow(
        session=database_session, flow=schemas.actions.FlowCreate(name="my-flow")
    )
    return model


@pytest.fixture
async def flow_run(database_session, flow):
    model = await models.flow_runs.create_flow_run(
        session=database_session,
        flow_run=schemas.actions.FlowRunCreate(flow_id=flow.id, flow_version="0.1"),
    )
    return model


@pytest.fixture
async def task_run(database_session, flow_run):
    fake_task_run = schemas.actions.TaskRunCreate(
        flow_run_id=flow_run.id, task_key="my-key"
    )
    model = await models.task_runs.create_task_run(
        session=database_session, task_run=fake_task_run
    )
    return model


@pytest.fixture
async def flow_run_states(database_session, flow_run):
    scheduled_state = schemas.actions.StateCreate(
        type=schemas.core.StateType.SCHEDULED,
        timestamp=pendulum.now().subtract(seconds=5),
    )
    scheduled_flow_run_state = await models.flow_run_states.create_flow_run_state(
        session=database_session,
        flow_run_id=flow_run.id,
        state=scheduled_state,
    )
    running_state = schemas.actions.StateCreate(type="RUNNING")
    running_flow_run_state = await models.flow_run_states.create_flow_run_state(
        session=database_session,
        flow_run_id=flow_run.id,
        state=running_state,
    )
    return [scheduled_flow_run_state, running_flow_run_state]


@pytest.fixture
async def task_run_states(database_session, task_run):
    scheduled_state = schemas.actions.StateCreate(
        type=schemas.core.StateType.SCHEDULED,
        timestamp=pendulum.now().subtract(seconds=5),
    )
    scheduled_task_run_state = await models.task_run_states.create_task_run_state(
        session=database_session,
        task_run_id=task_run.id,
        state=scheduled_state,
    )
    running_state = schemas.actions.StateCreate(type="RUNNING")
    running_task_run_state = await models.task_run_states.create_task_run_state(
        session=database_session,
        task_run_id=task_run.id,
        state=running_state,
    )
    return [scheduled_task_run_state, running_task_run_state]