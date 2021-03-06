from typing import Any
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from baseplate import _ExcInfo
from baseplate import Span
from baseplate import SpanObserver
from baseplate.clients import ContextFactory
from baseplate.lib import config
from baseplate.lib import metrics
from baseplate.lib.secrets import SecretsStore


def engine_from_config(
    app_config: config.RawConfig,
    secrets: Optional[SecretsStore] = None,
    prefix: str = "database.",
    **kwargs: Any,
) -> Engine:
    """Make an :py:class:`~sqlalchemy.engine.Engine` from a configuration dictionary.

    The keys useful to :py:func:`engine_from_config` should be prefixed, e.g.
    ``database.url``, etc. The ``prefix`` argument specifies the prefix used to
    filter keys.

    Supported keys:

    * ``url``: the connection URL to the database, passed to
        :py:func:`~sqlalchemy.engine.url.make_url` to create the
        :py:class:`~sqlalchemy.engine.url.URL` used to connect to the database.
    * ``credentials_secret`` (optional): the key used to retrieve the database
        credentials from ``secrets`` as a :py:class:`~baseplate.lib.secrets.CredentialSecret`.
        If this is supplied, any credentials given in ``url`` we be replaced by
        these.
    * ``pool_recycle`` (optional): this setting causes the pool to recycle connections after
        the given number of seconds has passed. It defaults to -1, or no timeout.

    """
    assert prefix.endswith(".")
    parser = config.SpecParser(
        {
            "url": config.String,
            "credentials_secret": config.Optional(config.String),
            "pool_recycle": config.Optional(config.Integer),
        }
    )
    options = parser.parse(prefix[:-1], app_config)
    url = make_url(options.url)

    if options.pool_recycle is not None:
        kwargs.setdefault("pool_recycle", options.pool_recycle)

    if options.credentials_secret:
        if not secrets:
            raise TypeError("'secrets' is required if 'credentials_secret' is set")
        credentials = secrets.get_credentials(options.credentials_secret)
        url.username = credentials.username
        url.password = credentials.password

    return create_engine(url, **kwargs)


class SQLAlchemySession(config.Parser):
    """Configure a SQLAlchemy Session.

    This is meant to be used with
    :py:meth:`baseplate.Baseplate.configure_context`.

    See :py:func:`engine_from_config` for available configuration settings.

    :param secrets: Required if configured to use credentials to talk to the database.

    """

    def __init__(self, secrets: Optional[SecretsStore] = None, **kwargs: Any):
        self.secrets = secrets
        self.kwargs = kwargs

    def parse(
        self, key_path: str, raw_config: config.RawConfig
    ) -> "SQLAlchemySessionContextFactory":
        engine = engine_from_config(
            raw_config, secrets=self.secrets, prefix=f"{key_path}.", **self.kwargs
        )
        return SQLAlchemySessionContextFactory(engine)


Parameters = Optional[Union[Dict[str, Any], Sequence[Any]]]


class SQLAlchemyEngineContextFactory(ContextFactory):
    """SQLAlchemy core engine context factory.

    This factory will attach a SQLAlchemy :py:class:`sqlalchemy.engine.Engine`
    to an attribute on the :py:class:`~baseplate.RequestContext`. All cursor
    (query) execution will automatically record diagnostic information.

    Additionally, the trace and span ID will be added as a comment to the text
    of the SQL statement. This is to aid correlation of queries with requests.

    .. seealso::

        The engine is the low-level SQLAlchemy API. If you want to use the ORM,
        consider using
        :py:class:`~baseplate.clients.sqlalchemy.SQLAlchemySessionContextFactory`
        instead.

    :param engine: A configured SQLAlchemy engine.

    """

    def __init__(self, engine: Engine):
        self.engine = engine.execution_options()
        event.listen(self.engine, "before_cursor_execute", self.on_before_execute, retval=True)
        event.listen(self.engine, "after_cursor_execute", self.on_after_execute)
        event.listen(self.engine, "handle_error", self.on_error)

    def report_runtime_metrics(self, batch: metrics.Client) -> None:
        pool = self.engine.pool
        if not isinstance(pool, QueuePool):
            return

        batch.gauge("pool.size").replace(pool.size())
        batch.gauge("pool.open_and_available").replace(pool.checkedin())
        batch.gauge("pool.in_use").replace(pool.checkedout())
        batch.gauge("pool.overflow").replace(max(pool.overflow(), 0))

    def make_object_for_context(self, name: str, span: Span) -> Engine:
        engine = self.engine.execution_options(context_name=name, server_span=span)
        return engine

    # pylint: disable=unused-argument, too-many-arguments
    def on_before_execute(
        self,
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Parameters,
        context: Optional[ExecutionContext],
        executemany: bool,
    ) -> Tuple[str, Parameters]:
        """Handle the engine's before_cursor_execute event."""
        context_name = conn._execution_options["context_name"]
        server_span = conn._execution_options["server_span"]

        trace_name = "{}.{}".format(context_name, "execute")
        span = server_span.make_child(trace_name)
        span.set_tag("statement", statement)
        span.start()

        conn.info["span"] = span

        # add a comment to the sql statement with the trace and span ids
        # this is useful for slow query logs and active query views
        annotated_statement = f"{statement} -- trace:{span.trace_id:d},span:{span.id:d}"
        return annotated_statement, parameters

    # pylint: disable=unused-argument, too-many-arguments
    def on_after_execute(
        self,
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Parameters,
        context: Optional[ExecutionContext],
        executemany: bool,
    ) -> None:
        """Handle the event which happens after successful cursor execution."""
        conn.info["span"].finish()
        conn.info["span"] = None

    def on_error(self, context: ExceptionContext) -> None:
        """Handle the event which happens on exceptions during execution."""
        exc_info = (type(context.original_exception), context.original_exception, None)
        context.connection.info["span"].finish(exc_info=exc_info)
        context.connection.info["span"] = None


class SQLAlchemySessionContextFactory(SQLAlchemyEngineContextFactory):
    """SQLAlchemy ORM session context factory.

    This factory will attach a new SQLAlchemy
    :py:class:`sqlalchemy.orm.session.Session` to an attribute on the
    :py:class:`~baseplate.RequestContext`. All cursor (query) execution will
    automatically record diagnostic information.

    The session will be automatically closed, but not committed or rolled back,
    at the end of each request.

    .. seealso::

        The session is part of the high-level SQLAlchemy ORM API. If you want
        to do raw queries, consider using
        :py:class:`~baseplate.clients.sqlalchemy.SQLAlchemyEngineContextFactory`
        instead.

    :param engine: A configured SQLAlchemy engine.

    """

    def make_object_for_context(self, name: str, span: Span) -> Session:
        engine = super().make_object_for_context(name, span)
        session = Session(bind=engine)
        span.register(SQLAlchemySessionSpanObserver(session))
        return session


class SQLAlchemySessionSpanObserver(SpanObserver):
    """Automatically close the session at the end of each request."""

    def __init__(self, session: Session):
        self.session = session

    def on_finish(self, exc_info: Optional[_ExcInfo]) -> None:
        self.session.close()
