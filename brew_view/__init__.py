import contextlib
import logging
import os
import ssl
from concurrent.futures import ThreadPoolExecutor

from apispec import APISpec
from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers.tornado import TornadoScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APTPExecutor
from pytz import utc
from thriftpy.rpc import client_context
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.web import Application, StaticFileHandler, RedirectHandler
from urllib3.util.url import Url

import bg_utils
import brewtils.rest
from bg_utils.event_publisher import EventPublishers
from bg_utils.pika import TransientPikaClient
from bg_utils.plugin_logging_loader import PluginLoggingLoader
from brew_view.publishers import (MongoPublisher, RequestPublisher,
                                  TornadoPikaPublisher, WebsocketPublisher)
from brew_view.specification import get_default_logging_config
from brewtils.schemas import ParameterSchema, CommandSchema, InstanceSchema, SystemSchema, \
    RequestSchema, PatchSchema, LoggingConfigSchema, EventSchema, QueueSchema

config = None
application = None
server = None
tornado_app = None
public_url = None
logger = None
thrift_context = None
event_publishers = None
api_spec = None
plugin_logging_config = None
app_log_config = None
notification_meta = None
scheduler = None
request_map = {}


def setup_brew_view(spec, cli_args):
    global config, logger, app_log_config, event_publishers, notification_meta

    config = bg_utils.load_application_config(spec, cli_args)
    config.web.url_prefix = brewtils.rest.normalize_url_prefix(config.web.url_prefix)

    log_default = get_default_logging_config(config.log.level, config.log.file)
    app_log_config = bg_utils.setup_application_logging(config, log_default)
    logger = logging.getLogger(__name__)

    bg_utils.setup_database(config)
    load_plugin_logging_config(config)
    _setup_application()


def shutdown():
    """Close any open websocket connections"""
    from brew_view.controllers import EventSocket
    EventSocket.shutdown()


def load_plugin_logging_config(input_config):
    global plugin_logging_config

    plugin_logging_config = PluginLoggingLoader().load(
        filename=input_config.plugin_logging.config_file,
        level=input_config.plugin_logging.level,
        default_config=app_log_config
    )


def _setup_application():
    global application, server, tornado_app, public_url, thrift_context, event_publishers, scheduler

    public_url = Url(scheme='https' if config.web.ssl.enabled else 'http',
                     host=config.event.public_fqdn,
                     port=config.web.port,
                     path=config.web.url_prefix).url

    thrift_context = _setup_thrift_context()
    tornado_app = _setup_tornado_app()
    server_ssl, client_ssl = _setup_ssl_context()
    event_publishers = _setup_event_publishers(client_ssl)
    scheduler = _setup_scheduler()

    server = HTTPServer(tornado_app, ssl_options=server_ssl)
    server.listen(config.web.port)

    application = IOLoop.current()


def _setup_scheduler():
    # TODO: Create our own JobStore
    # https://apscheduler.readthedocs.io/en/latest/extending.html#custom-job-stores
    jobstores = {
        'mongo': MongoDBJobStore(config.db.name, **config.db.connection),
    }
    # TODO: Explore different executors (maybe process pool?)
    executors = {
        'default': APTPExecutor(20),
    }
    job_defaults = {
        'coalesce': True,
        'max_instances': 3,
    }

    return TornadoScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone=utc
    )


def _setup_tornado_app():

    # Import these here so we don't have a problem importing thrift_context
    from brew_view.controllers import AdminAPI, CommandAPI, CommandListAPI, ConfigHandler, \
        InstanceAPI, QueueAPI, QueueListAPI, RequestAPI, RequestListAPI, SystemAPI, SystemListAPI, \
        VersionHandler, SpecHandler, SwaggerConfigHandler, OldAdminAPI, OldQueueAPI, \
        OldQueueListAPI, LoggingConfigAPI, EventPublisherAPI, EventSocket, JobListAPI, JobAPI

    prefix = config.web.url_prefix
    static_base = os.path.join(os.path.dirname(__file__), 'static', 'dist')

    # These get documented in our OpenAPI (fka Swagger) documentation
    published_url_specs = [
        (r'{0}api/v1/commands/?'.format(prefix), CommandListAPI),
        (r'{0}api/v1/requests/?'.format(prefix), RequestListAPI),
        (r'{0}api/v1/systems/?'.format(prefix), SystemListAPI),
        (r'{0}api/v1/queues/?'.format(prefix), QueueListAPI),
        (r'{0}api/v1/admin/?'.format(prefix), AdminAPI),
        (r'{0}api/v1/commands/(\w+)/?'.format(prefix), CommandAPI),
        (r'{0}api/v1/instances/(\w+)/?'.format(prefix), InstanceAPI),
        (r'{0}api/v1/requests/(\w+)/?'.format(prefix), RequestAPI),
        (r'{0}api/v1/systems/(\w+)/?'.format(prefix), SystemAPI),
        (r'{0}api/v1/queues/([\w\.-]+)/?'.format(prefix), QueueAPI),
        (r'{0}api/v1/config/logging/?'.format(prefix), LoggingConfigAPI),

        # Beta
        (r'{0}api/vbeta/events/?'.format(prefix), EventPublisherAPI),

        # Deprecated
        (r'{0}api/v1/admin/system/?'.format(prefix), OldAdminAPI),
        (r'{0}api/v1/admin/queues/?'.format(prefix), OldQueueListAPI),
        (r'{0}api/v1/admin/queues/([\w\.-]+)/?'.format(prefix), OldQueueAPI)
    ]

    # And these do not
    unpublished_url_specs = [
        # TODO: Move these to published_url.
        (r'{0}api/v1/jobs/?'.format(prefix), JobListAPI),
        (r'{0}api/v1/jobs/(\w+)/?'.format(prefix), JobAPI),
        # These are a little special - unpublished but still versioned
        # The swagger spec
        (r'{0}api/v1/spec/?'.format(prefix), SpecHandler),
        # Events websocket
        (r'{0}api/v1/socket/events/?'.format(prefix), EventSocket),

        # Version / configs
        (r'{0}version/?'.format(prefix), VersionHandler),
        (r'{0}config/?'.format(prefix), ConfigHandler),
        (r'{0}config/swagger/?'.format(prefix), SwaggerConfigHandler),

        # Not sure if these are really necessary
        (r'{0}'.format(prefix[:-1]), RedirectHandler, {"url": prefix}),
        (r'{0}swagger/(.*)'.format(prefix), StaticFileHandler,
            {'path': os.path.join(static_base, 'swagger')}),

        # Static content
        (r'{0}(.*)'.format(prefix), StaticFileHandler,
            {'path': static_base, 'default_filename': 'index.html'})
    ]
    _load_swagger(published_url_specs, title=config.application.name)

    return Application(published_url_specs + unpublished_url_specs, debug=config.debug_mode)


def _setup_ssl_context():

    if config.web.ssl.enabled:
        server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ssl.load_cert_chain(certfile=config.web.ssl.public_key,
                                   keyfile=config.web.ssl.private_key)
        server_ssl.verify_mode = getattr(ssl, 'CERT_'+config.web.ssl.client_cert_verify.upper())

        client_ssl = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        client_ssl.load_cert_chain(certfile=config.web.ssl.public_key,
                                   keyfile=config.web.ssl.private_key)

        if config.web.ssl.ca_cert or config.web.ssl.ca_path:
            server_ssl.load_verify_locations(cafile=config.web.ssl.ca_cert,
                                             capath=config.web.ssl.ca_path)
            client_ssl.load_verify_locations(cafile=config.web.ssl.ca_cert,
                                             capath=config.web.ssl.ca_path)
    else:
        server_ssl = None
        client_ssl = None

    return server_ssl, client_ssl


def _setup_thrift_context():

    class BgClient(object):
        """Helper class that wraps a thriftpy TClient"""

        executor = ThreadPoolExecutor(max_workers=10)

        def __init__(self, t_client):
            self.t_client = t_client

        def __getattr__(self, thrift_method):
            def submit(*args, **kwargs):
                return self.executor.submit(self.t_client.__getattr__(thrift_method),
                                            *args, **kwargs)
            return submit

    @contextlib.contextmanager
    def bg_thrift_context(async=True, **kwargs):
        with client_context(bg_utils.bg_thrift.BartenderBackend,
                            host=config.backend.host,
                            port=config.backend.port,
                            socket_timeout=config.backend.socket_timeout,
                            **kwargs) as client:
            yield BgClient(client) if async else client

    return bg_thrift_context


def _setup_event_publishers(ssl_context):
    from brew_view.controllers.event_api import EventSocket

    # Create the collection of event publishers and add concrete publishers to it
    pubs = EventPublishers({
        'request': RequestPublisher(ssl_context=ssl_context),
        'websocket': WebsocketPublisher(EventSocket)
    })

    if config.event.mongo.enable:
        pubs['mongo'] = MongoPublisher()

    if config.event.amq.enable and config.event.amq.virtual_host and config.event.amq.exchange:
        pika_params = {
            'host': config.amq.host,
            'port': config.amq.connections.message.port,
            'user': config.amq.connections.admin.user,
            'password': config.amq.connections.admin.password,
            'exchange': config.event.amq.exchange,
            'virtual_host': config.event.amq.virtual_host,
            'connection_attempts': config.amq.connection_attempts
        }

        # Make sure the exchange exists
        TransientPikaClient(**pika_params).declare_exchange()

        pubs['pika'] = TornadoPikaPublisher(
            shutdown_timeout=config.shutdown_timeout,
            **pika_params)

    # Add metadata functions - additional metadata that will be included with each event
    pubs.metadata_funcs['public_url'] = lambda: public_url

    return pubs


def _load_swagger(url_specs, title=None):

    global api_spec
    api_spec = APISpec(title=title, version='1.0',
                       plugins=('apispec.ext.marshmallow', 'apispec.ext.tornado'))

    # Schemas from Marshmallow
    api_spec.definition('Parameter', schema=ParameterSchema)
    api_spec.definition('Command', schema=CommandSchema)
    api_spec.definition('Instance', schema=InstanceSchema)
    api_spec.definition('Request', schema=RequestSchema)
    api_spec.definition('System', schema=SystemSchema)
    api_spec.definition('LoggingConfig', schema=LoggingConfigSchema)
    api_spec.definition('Event', schema=EventSchema)
    api_spec.definition('Queue', schema=QueueSchema)
    api_spec.definition('_patch', schema=PatchSchema)
    api_spec.definition('Patch', properties={"operations": {
        "type": "array", "items": {"$ref": "#/definitions/_patch"}}
    })

    error = {'message': {'type': 'string'}}
    api_spec.definition('400Error', properties=error, description='Parameter validation error')
    api_spec.definition('404Error', properties=error, description='Resource does not exist')
    api_spec.definition('409Error', properties=error, description='Resource already exists')
    api_spec.definition('50xError', properties=error, description='Server exception')

    # Finally, add documentation for all our published paths
    for url_spec in url_specs:
        api_spec.add_path(urlspec=url_spec)
