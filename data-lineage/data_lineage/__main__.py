import logging

import click
from redis import Redis

from data_lineage import __version__
from data_lineage.server import create_server


@click.command()
@click.version_option(__version__)
@click.option(
    "-l", "--log-level", envvar="LOG_LEVEL", help="Logging Level", default="INFO"
)
@click.option(
    "--catalog-user", help="Database user name", envvar="CATALOG_USER", required=True
)
@click.option(
    "--catalog-password",
    help="Database Password",
    envvar="CATALOG_PASSWORD",
    required=True,
)
@click.option(
    "--catalog-host", help="Database Host", envvar="CATALOG_HOST", default="localhost"
)
@click.option(
    "--catalog-port", help="Database Password", envvar="CATALOG_PORT", default=5432
)
@click.option(
    "--catalog-db", help="Postgres Database", envvar="CATALOG_DB", default="tokern"
)
@click.option(
    "--redis-host",
    help="Redis host for queueing scans",
    envvar="REDIS_HOST",
    default="localhost",
)
@click.option(
    "--redis-port",
    help="Redis port for queueing scans",
    envvar="REDIS_PORT",
    default="6379",
)
@click.option(
    "--is-production/--not-production",
    help="Run server in development mode",
    default=True,
)
def main(
    log_level,
    catalog_user,
    catalog_password,
    catalog_host,
    catalog_port,
    catalog_db,
    redis_host,
    redis_port,
    is_production,
):
    logging.basicConfig(level=getattr(logging, log_level.upper()))
    catalog = {
        "user": catalog_user,
        "password": catalog_password,
        "host": catalog_host,
        "port": catalog_port,
        "database": catalog_db,
    }
    connection = Redis(redis_host, redis_port)
    app, catalog = create_server(
        catalog, connection=connection, is_production=is_production
    )
    if is_production:
        app.run()
    else:
        app.run(debug=True)


if __name__ == "__main__":
    main()
