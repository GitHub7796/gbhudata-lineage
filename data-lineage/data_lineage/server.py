import datetime
import logging
from typing import Any, Dict, List, Tuple

import flask_restless
import gunicorn.app.base
from dbcat import Catalog, PGCatalog, init_db
from dbcat.catalog import CatColumn
from dbcat.catalog.models import (
    CatSchema,
    CatSource,
    CatTable,
    ColumnLineage,
    DefaultSchema,
    Job,
    JobExecution,
    JobExecutionStatus,
)
from flask import Flask
from flask_restful import Api, Resource, reqparse
from pglast.parser import ParseError
from rq import Queue
from rq import job as RqJob
from werkzeug.exceptions import NotFound, UnprocessableEntity

from data_lineage import ColumnNotFound, SemanticError, TableNotFound
from data_lineage.parser import (
    analyze_dml_query,
    extract_lineage,
    parse,
    parse_dml_query,
)
from data_lineage.worker import scan


class TableNotFoundHTTP(NotFound):
    """Table not found in catalog"""

    code = 441


class ColumnNotFoundHTTP(NotFound):
    """Column not found in catalog"""

    code = 442


class ParseErrorHTTP(UnprocessableEntity):
    """Parser Error"""


class SemanticErrorHTTP(UnprocessableEntity):
    """Semantic Error"""

    code = 443


class Kedro(Resource):
    def __init__(self, catalog: Catalog):
        self._catalog = catalog
        self._parser = reqparse.RequestParser()
        self._parser.add_argument(
            "job_ids", action="append", help="List of job ids for a sub graph"
        )

    def get(self):
        nodes = []
        edges = []

        args = self._parser.parse_args()
        with self._catalog.managed_session:
            column_edges = self._catalog.get_column_lineages(args["job_ids"])
            for edge in column_edges:
                nodes.append(self._column_info(edge.source))
                nodes.append(self._column_info(edge.target))
                nodes.append(self._job_info(edge.job_execution.job))
                edges.append(
                    {
                        "source": "column:{}".format(edge.source_id),
                        "target": "task:{}".format(edge.job_execution.job_id),
                    }
                )
                edges.append(
                    {
                        "source": "task:{}".format(edge.job_execution.job_id),
                        "target": "column:{}".format(edge.target_id),
                    }
                )

        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _column_info(node: CatColumn):
        return {
            "id": "column:{}".format(node.id),
            "name": ".".join(node.fqdn),
            "type": "data",
        }

    @staticmethod
    def _job_info(node: Job):
        return {"id": "task:{}".format(node.id), "name": node.name, "type": "task"}


class ScanList(Resource):
    def __init__(self, catalog: PGCatalog, queue: Queue):
        self._catalog = catalog
        self._queue = queue
        self._parser = reqparse.RequestParser()
        self._parser.add_argument("id", required=True, help="ID of the resource")

    def post(self):
        args = self._parser.parse_args()
        logging.info("Args for scanning: {}".format(args))
        job = self._queue.enqueue(
            scan,
            {
                "user": self._catalog.user,
                "password": self._catalog.password,
                "database": self._catalog.database,
                "host": self._catalog.host,
                "port": self._catalog.port,
            },
            int(args["id"]),
        )

        return {"id": job.id, "status": "queued"}, 200

    def get(self):
        job_list = []
        for job in self._queue.started_job_registry.get_job_ids():
            job_list.append({"id": job, "status": "started"})

        for job in self._queue.finished_job_registry.get_job_ids():
            job_list.append({"id": job, "status": "finished"})

        for job in self._queue.failed_job_registry.get_job_ids():
            job_list.append({"id": job, "status": "failed"})

        return job_list, 200


class Scan(Resource):
    def __init__(self, catalog: PGCatalog, queue: Queue):
        self._catalog = catalog
        self._queue = queue
        self._parser = reqparse.RequestParser()
        self._parser.add_argument("id", required=True, help="ID of the resource")

    def get(self, job_id):
        status = RqJob.Job.fetch(job_id, connection=self._queue.connection).get_status()
        return {"id": job_id, "status": status}, 200

    def put(self, job_id):
        RqJob.Job.fetch(job_id, connection=self._queue.connection).cancel()
        return {"message": "Job {} cancelled".format(job_id)}, 200


class Parse(Resource):
    def __init__(self, catalog: Catalog):
        self._catalog = catalog
        self._parser = reqparse.RequestParser()
        self._parser.add_argument("query", required=True, help="Query to parse")
        self._parser.add_argument(
            "source_id", help="Source database of the query", required=True
        )

    def post(self):
        args = self._parser.parse_args()
        logging.debug("Parse query: {}".format(args["query"]))
        try:
            parsed = parse(args["query"], "parse_api")
        except ParseError as error:
            raise ParseErrorHTTP(description=str(error))

        try:
            with self._catalog.managed_session:
                source = self._catalog.get_source_by_id(args["source_id"])
                logging.debug("Parsing query for source {}".format(source))
                binder = parse_dml_query(
                    catalog=self._catalog, parsed=parsed, source=source
                )

                return (
                    {
                        "select_tables": [table.name for table in binder.tables],
                        "select_columns": [context.alias for context in binder.columns],
                    },
                    200,
                )
        except TableNotFound as table_error:
            raise TableNotFoundHTTP(description=str(table_error))
        except ColumnNotFound as column_error:
            raise ColumnNotFoundHTTP(description=str(column_error))
        except SemanticError as semantic_error:
            raise SemanticErrorHTTP(description=str(semantic_error))


class Analyze(Resource):
    def __init__(self, catalog: Catalog):
        self._catalog = catalog
        self._parser = reqparse.RequestParser()
        self._parser.add_argument("query", required=True, help="Query to parse")
        self._parser.add_argument("name", help="Name of the ETL job")
        self._parser.add_argument(
            "start_time", required=True, help="Start time of the task"
        )
        self._parser.add_argument(
            "end_time", required=True, help="End time of the task"
        )
        self._parser.add_argument(
            "source_id", help="Source database of the query", required=True
        )

    def post(self):
        args = self._parser.parse_args()
        logging.debug("Parse query: {}".format(args["query"]))
        try:
            parsed = parse(args["query"], args["name"])
        except ParseError as error:
            raise ParseErrorHTTP(description=str(error))

        try:
            with self._catalog.managed_session:
                source = self._catalog.get_source_by_id(args["source_id"])
                logging.debug("Parsing query for source {}".format(source))
                chosen_visitor = analyze_dml_query(self._catalog, parsed, source)
                job_execution = extract_lineage(
                    catalog=self._catalog,
                    visited_query=chosen_visitor,
                    source=source,
                    parsed=parsed,
                    start_time=datetime.datetime.fromisoformat(args["start_time"]),
                    end_time=datetime.datetime.fromisoformat(args["end_time"]),
                )

                return (
                    {
                        "data": {
                            "id": job_execution.id,
                            "type": "job_executions",
                            "attributes": {
                                "job_id": job_execution.job_id,
                                "started_at": job_execution.started_at.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                                "ended_at": job_execution.ended_at.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                                "status": job_execution.status.name,
                            },
                        }
                    },
                    200,
                )
        except TableNotFound as table_error:
            raise TableNotFoundHTTP(description=str(table_error))
        except ColumnNotFound as column_error:
            raise ColumnNotFoundHTTP(description=str(column_error))
        except SemanticError as semantic_error:
            raise SemanticErrorHTTP(description=str(semantic_error))


class Server(gunicorn.app.base.BaseApplication):
    def __init__(self, app):
        self.application = app
        super().__init__()

    def load_config(self):
        # parse console args
        parser = self.cfg.parser()
        env_args = parser.parse_args(self.cfg.get_cmd_args_from_env())

        # Load up environment configuration
        for k, v in vars(env_args).items():
            if v is None:
                continue
            if k == "args":
                continue
            self.cfg.set(k.lower(), v)

    def load(self):
        return self.application


def job_execution_serializer(instance: JobExecution, only: List[str]):
    return {
        "id": instance.id,
        "type": "job_executions",
        "attributes": {
            "job_id": instance.job_id,
            "started_at": instance.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": instance.ended_at.strftime("%Y-%m-%d %H:%M:%S"),
            "status": instance.status.name,
        },
    }


def job_execution_deserializer(data: Dict["str", Any]):
    attributes = data["data"]["attributes"]
    logging.debug(attributes)
    job_execution = JobExecution()
    job_execution.job_id = int(attributes["job_id"])
    job_execution.started_at = datetime.datetime.strptime(
        attributes["started_at"], "%Y-%m-%d %H:%M:%S"
    )
    job_execution.ended_at = datetime.datetime.strptime(
        attributes["ended_at"], "%Y-%m-%d %H:%M:%S"
    )
    job_execution.status = (
        JobExecutionStatus.SUCCESS
        if attributes["status"] == "SUCCESS"
        else JobExecutionStatus.SUCCESS
    )

    logging.debug(job_execution)
    logging.debug(job_execution.status == JobExecutionStatus.SUCCESS)
    return job_execution


def create_server(
    catalog_options: Dict[str, str], connection, is_production=True
) -> Tuple[Any, PGCatalog]:
    logging.debug(catalog_options)
    catalog = PGCatalog(
        **catalog_options,
        connect_args={"application_name": "data-lineage:flask-restless"},
        max_overflow=40,
        pool_size=20,
        pool_pre_ping=True
    )

    init_db(catalog)

    restful_catalog = PGCatalog(
        **catalog_options,
        connect_args={"application_name": "data-lineage:restful"},
        pool_pre_ping=True
    )

    app = Flask(__name__)
    queue = Queue(is_async=is_production, connection=connection)

    # Create CRUD APIs
    methods = ["DELETE", "GET", "PATCH", "POST"]
    url_prefix = "/api/v1/catalog"
    api_manager = flask_restless.APIManager(app, catalog.get_scoped_session())
    api_manager.create_api(
        CatSource,
        methods=methods,
        url_prefix=url_prefix,
        additional_attributes=["fqdn"],
    )
    api_manager.create_api(
        CatSchema,
        methods=methods,
        url_prefix=url_prefix,
        additional_attributes=["fqdn"],
    )
    api_manager.create_api(
        CatTable,
        methods=methods,
        url_prefix=url_prefix,
        additional_attributes=["fqdn"],
    )
    api_manager.create_api(
        CatColumn,
        methods=methods,
        url_prefix=url_prefix,
        additional_attributes=["fqdn"],
    )
    api_manager.create_api(Job, methods=methods, url_prefix=url_prefix)
    api_manager.create_api(
        JobExecution,
        methods=methods,
        url_prefix=url_prefix,
        serializer=job_execution_serializer,
        deserializer=job_execution_deserializer,
    )
    api_manager.create_api(
        ColumnLineage,
        methods=methods,
        url_prefix=url_prefix,
        collection_name="column_lineage",
    )

    api_manager.create_api(
        DefaultSchema,
        methods=methods,
        url_prefix=url_prefix,
        collection_name="default_schema",
        primary_key="source_id",
    )

    restful_manager = Api(app)
    restful_manager.add_resource(
        Kedro, "/api/main", resource_class_kwargs={"catalog": restful_catalog}
    )
    restful_manager.add_resource(
        ScanList,
        "/api/v1/scan",
        resource_class_kwargs={"catalog": restful_catalog, "queue": queue},
    )

    restful_manager.add_resource(
        Scan,
        "/api/v1/scan/<job_id>",
        resource_class_kwargs={"catalog": restful_catalog, "queue": queue},
    )

    restful_manager.add_resource(
        Analyze, "/api/v1/analyze", resource_class_kwargs={"catalog": restful_catalog}
    )

    restful_manager.add_resource(
        Parse, "/api/v1/parse", resource_class_kwargs={"catalog": restful_catalog}
    )

    for rule in app.url_map.iter_rules():
        rule_methods = ",".join(rule.methods)
        logging.debug("{:50s} {:20s} {}".format(rule.endpoint, rule_methods, rule))

    if is_production:
        return Server(app=app), catalog
    else:
        return app, catalog
