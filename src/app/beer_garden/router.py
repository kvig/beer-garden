# -*- coding: utf-8 -*-
import logging
import threading
from typing import Dict, Optional, Union

import requests
from brewtils.models import Events, Garden, Operation, Request, System
from brewtils.schema_parser import SchemaParser

import beer_garden
import beer_garden.commands
import beer_garden.config as config
import beer_garden.db.api as db
import beer_garden.garden
import beer_garden.log
import beer_garden.namespace
import beer_garden.plugin
import beer_garden.queues
import beer_garden.requests
import beer_garden.scheduler
import beer_garden.systems
from beer_garden.errors import RoutingRequestException, UnknownGardenException
from beer_garden.events.processors import QueueListener
from beer_garden.garden import get_garden, get_gardens

logger = logging.getLogger(__name__)

# These are the operations that we will forward to child gardens
routable_operations = [
    "INSTANCE_START",
    "INSTANCE_STOP",
    "REQUEST_CREATE",
    "SYSTEM_DELETE",
    "GARDENS_SYNC",
]

# Processor that will be used for forwarding
forward_processor: Optional[QueueListener] = None

# Used for actually sending operations to other gardens
garden_lock = threading.Lock()
gardens: Dict[str, Garden] = {}  # garden_name -> garden

# Used for determining WHERE to route an operation
routing_lock = threading.Lock()
system_name_routes: Dict[str, str] = {}
system_id_routes: Dict[str, str] = {}
instance_id_routes: Dict[str, str] = {}


def route_garden_sync(target_garden_name: str = None):
    # If a Garden Name is provided, determine where to route the request
    if target_garden_name:
        if target_garden_name == config.get("garden.name"):
            beer_garden.garden.publish_garden()
        else:
            forward(
                Operation(
                    operation_type="GARDEN_SYNC", target_garden_name=target_garden_name
                )
            )

    else:
        # Iterate over all gardens and forward the sync request
        with garden_lock:
            for garden in gardens.values():
                if garden.name == config.get("garden.name"):
                    beer_garden.garden.publish_garden()
                else:
                    forward(
                        Operation(
                            operation_type="GARDEN_SYNC", target_garden_name=garden.name
                        )
                    )


route_functions = {
    "REQUEST_CREATE": beer_garden.requests.process_request,
    "REQUEST_START": beer_garden.requests.start_request,
    "REQUEST_COMPLETE": beer_garden.requests.complete_request,
    "REQUEST_READ": beer_garden.requests.get_request,
    "REQUEST_READ_ALL": beer_garden.requests.get_requests,
    "COMMAND_READ": beer_garden.commands.get_command,
    "COMMAND_READ_ALL": beer_garden.commands.get_commands,
    "INSTANCE_READ": beer_garden.systems.get_instance,
    "INSTANCE_DELETE": beer_garden.systems.remove_instance,
    "INSTANCE_UPDATE": beer_garden.plugin.update,
    "INSTANCE_HEARTBEAT": beer_garden.plugin.heartbeat,
    "INSTANCE_INITIALIZE": beer_garden.plugin.initialize,
    "INSTANCE_START": beer_garden.plugin.start,
    "INSTANCE_STOP": beer_garden.plugin.stop,
    "INSTANCE_LOGS": beer_garden.plugin.read_logs,
    "JOB_CREATE": beer_garden.scheduler.create_job,
    "JOB_READ": beer_garden.scheduler.get_job,
    "JOB_READ_ALL": beer_garden.scheduler.get_jobs,
    "JOB_PAUSE": beer_garden.scheduler.pause_job,
    "JOB_RESUME": beer_garden.scheduler.resume_job,
    "JOB_DELETE": beer_garden.scheduler.remove_job,
    "SYSTEM_CREATE": beer_garden.systems.upsert,
    "SYSTEM_READ": beer_garden.systems.get_system,
    "SYSTEM_READ_ALL": beer_garden.systems.get_systems,
    "SYSTEM_UPDATE": beer_garden.systems.update_system,
    "SYSTEM_RELOAD": beer_garden.systems.reload_system,
    "SYSTEM_RESCAN": beer_garden.systems.rescan_system_directory,
    "SYSTEM_DELETE": beer_garden.systems.purge_system,
    "GARDEN_CREATE": beer_garden.garden.create_garden,
    "GARDEN_READ": beer_garden.garden.get_garden,
    "GARDEN_READ_ALL": beer_garden.garden.get_gardens,
    "GARDEN_UPDATE_STATUS": beer_garden.garden.update_garden_status,
    "GARDEN_UPDATE_CONFIG": beer_garden.garden.update_garden_config,
    "GARDEN_DELETE": beer_garden.garden.remove_garden,
    "GARDEN_SYNC": route_garden_sync,
    "PLUGIN_LOG_READ": beer_garden.log.get_plugin_log_config,
    "PLUGIN_LOG_READ_LEGACY": beer_garden.log.get_plugin_log_config_legacy,
    "PLUGIN_LOG_RELOAD": beer_garden.log.load_plugin_log_config,
    "QUEUE_READ": beer_garden.queues.get_all_queue_info,
    "QUEUE_DELETE": beer_garden.queues.clear_queue,
    "QUEUE_DELETE_ALL": beer_garden.queues.clear_all_queues,
    "QUEUE_READ_INSTANCE": beer_garden.queues.get_instance_queues,
    "NAMESPACE_READ_ALL": beer_garden.namespace.get_namespaces,
}


def route(operation: Operation):
    """Entry point into the routing subsystem

    Args:
        operation: The operation to route

    Returns:

    """
    operation = _pre_route(operation)

    logger.debug(f"Routing {operation!r}")

    if not operation.operation_type:
        raise RoutingRequestException("Missing operation type")

    if operation.operation_type not in route_functions.keys():
        raise RoutingRequestException(
            f"Unknown operation type '{operation.operation_type}'"
        )

    # Determine which garden the operation is targeting
    if not operation.target_garden_name:
        operation.target_garden_name = _determine_target_garden(operation)

    if not operation.target_garden_name:
        raise UnknownGardenException(
            f"Could not determine the target garden for routing {operation!r}"
        )

    # If it's targeted at THIS garden, execute
    if operation.target_garden_name == config.get("garden.name"):
        return execute_local(operation)
    else:
        return initiate_forward(operation)


def execute_local(operation: Operation):
    """Execute an operation on the local garden

    Args:
        operation:

    Returns:

    """
    operation = _pre_execute(operation)

    return route_functions[operation.operation_type](
        *operation.args, **operation.kwargs
    )


def initiate_forward(operation: Operation):
    """Forward an operation to a child garden

    Will:
    - Pre-process the operation
    - Put the operation on the queue for forwarding
    - Return the "correct" response based on operation type

    Args:
        operation:
    """
    operation = _pre_forward(operation)

    # TODO - Check to ensure garden conn_info is not 'local' before forwarding?

    forward_processor.put(operation)

    if operation.operation_type == "REQUEST_CREATE":
        return operation.model


def forward(operation: Operation):
    """Forward the operation to a child garden

    Intended to be called in the context of an executor or processor.

    Args:
        operation: The operation to forward

    Returns:
        The result of the specific forward transport function used

    Raises:
        RoutingRequestException: Could not determine a route to child
        UnknownGardenException: The specified target garden is unknown
    """
    target_garden = gardens.get(operation.target_garden_name)

    if not target_garden:
        target_garden = get_garden(operation.target_garden_name)

    if not target_garden:
        raise UnknownGardenException(
            f"Unknown child garden {operation.target_garden_name}"
        )

    try:
        connection_type = target_garden.connection_type

        if connection_type is None:
            raise RoutingRequestException(
                f"Attempted to forward operation to garden "
                f"'{operation.target_garden_name}' but the connection type was None. "
                f"This probably means that the connection to the child garden has not "
                f"been configured, please talk to your system administrator."
            )
        elif connection_type.casefold() == "http":
            return _forward_http(operation, target_garden.connection_params)
        else:
            raise RoutingRequestException(f"Unknown connection type {connection_type}")
    except Exception as ex:
        logger.exception(f"Error forwarding operation:{ex}")


def setup_routing():
    """Initialize the routing subsystem

    This will load the cached child garden definitions and use them to populate the
    two dictionaries that matter, garden_lookup and garden_connections.

    It will then query the database for all local systems and add those to the
    dictionaries as well.
    """
    for system in db.query(System, filter_params={"local": True}):
        add_routing_system(system)

    # Don't add the local garden
    for garden in get_gardens(include_local=False):
        if garden.name != config.get("garden.name"):
            for system in garden.systems:
                add_routing_system(system=system, garden_name=garden.name)

            if (
                garden.connection_type is not None
                and garden.connection_type.casefold() != "local"
            ):
                with garden_lock:
                    gardens[garden.name] = garden
            else:
                logger.warning(f"Garden with invalid connection info: {garden!r}")


def add_routing_system(system=None, garden_name=None):
    """Update the gardens used for routing"""
    # Default to local garden name
    garden_name = garden_name or config.get("garden.name")

    with routing_lock:
        system_name_routes[str(system)] = garden_name
        system_id_routes[system.id] = garden_name

        for instance in system.instances:
            instance_id_routes[instance.id] = garden_name


def remove_routing_system(system=None):
    """Update the gardens used for routing"""
    with routing_lock:
        del system_name_routes[str(system)]
        del system_id_routes[system.id]

        for instance in system.instances:
            del instance_id_routes[instance.id]


def handle_event(event):
    """Handle events"""
    # Event handling is not fast enough to deal with system changes arising from the
    # local garden, so only handle child gardens
    if event.garden != config.get("garden.name"):
        if event.name in (Events.SYSTEM_CREATED.name, Events.SYSTEM_UPDATED.name):
            add_routing_system(system=event.payload, garden_name=event.garden)
        elif event.name == Events.SYSTEM_REMOVED.name:
            remove_routing_system(system=event.payload)

    # This is a little unintuitive. We want to let the garden module deal with handling
    # any downstream garden changes since handling those changes is nontrivial.
    # It's *those* events we want to act on here, not the "raw" downstream ones.
    # This is also why we only handle GARDEN_UPDATED and not STARTED or STOPPED
    if event.garden == config.get("garden.name"):
        if event.name == Events.GARDEN_UPDATED.name:
            gardens[event.payload.name] = event.payload

        elif event.name == Events.GARDEN_REMOVED.name:
            try:
                del gardens[event.payload.name]
            except KeyError:
                pass


def _pre_route(operation: Operation) -> Operation:
    """Called before any routing logic is applied"""
    # If no source garden is defined set it to the local garden
    if operation.source_garden_name is None:
        operation.source_garden_name = config.get("garden.name")

    return operation


def _pre_forward(operation: Operation) -> Operation:
    """Called before forwarding an operation"""

    # Validate that the operation can be forwarded
    if operation.operation_type not in routable_operations:
        raise RoutingRequestException(
            f"Operation type '{operation.operation_type}' can not be forwarded"
        )

    if operation.operation_type == "REQUEST_CREATE":
        operation.model = (
            beer_garden.requests.RequestValidator.instance().validate_request(
                operation.model
            )
        )

        # Save the request so it'll have an ID and we'll have something to update
        operation.model = db.create(operation.model)

        # Clear parent before forwarding so the child doesn't freak out about an
        # unknown request
        operation.model.parent = None
        operation.model.has_parent = False

        # Pull out and store the wait event, if it exists
        wait_event = operation.kwargs.pop("wait_event", None)
        if wait_event:
            beer_garden.requests.request_map[operation.model.id] = wait_event

    return operation


def _pre_execute(operation: Operation) -> Operation:
    """Called before executing an operation"""
    # If there's a model present, shove it in the front
    if operation.model:
        operation.args.insert(0, operation.model)

    return operation


def _determine_target_garden(operation: Operation) -> str:
    """Determine the system the operation is targeting"""

    # Certain operations are ASSUMED to be targeted at the local garden
    if (
        "READ" in operation.operation_type
        or "GARDEN" in operation.operation_type
        or "JOB" in operation.operation_type
        or operation.operation_type
        in ("PLUGIN_LOG_RELOAD", "SYSTEM_CREATE", "SYSTEM_RESCAN")
    ):
        return config.get("garden.name")

    # Otherwise, each operation needs to be "parsed"
    if operation.operation_type in ("SYSTEM_DELETE", "SYSTEM_RELOAD", "SYSTEM_UPDATE"):
        return _system_id_lookup(operation.args[0])

    elif "INSTANCE" in operation.operation_type:
        if "system_id" in operation.kwargs and "instance_name" in operation.kwargs:
            return _system_id_lookup(operation.kwargs["system_id"])
        else:
            return _instance_id_lookup(operation.args[0])

    elif operation.operation_type == "REQUEST_CREATE":
        target_system = System(
            namespace=operation.model.namespace,
            name=operation.model.system,
            version=operation.model.system_version,
        )
        return _system_name_lookup(target_system)

    elif operation.operation_type.startswith("REQUEST"):
        request = db.query_unique(Request, id=operation.args[0])
        operation.kwargs["request"] = request

        return config.get("garden.name")

    elif operation.operation_type == "QUEUE_DELETE":
        # Need to deconstruct the queue name
        parts = operation.args[0].split(".")
        version = parts[2].replace("-", ".")

        return _system_name_lookup(
            System(namespace=parts[0], name=parts[1], version=version)
        )

    raise Exception(f"Bad operation type {operation.operation_type}")


def _forward_http(operation: Operation, conn_info: dict):
    """Actually forward an operation using HTTP

    Args:
        operation: The operation to forward
        conn_info: Connection info
    """
    endpoint = "{}://{}:{}{}api/v1/forward".format(
        "https" if conn_info.get("ssl") else "http",
        conn_info.get("host"),
        conn_info.get("port"),
        conn_info.get("url_prefix", "/"),
    )

    if conn_info.get("ssl"):
        http_config = config.get("entry.http")
        return requests.post(
            endpoint,
            data=SchemaParser.serialize_operation(operation),
            cert=http_config.ssl.ca_cert,
            verify=http_config.ssl.ca_path,
        )

    else:
        return requests.post(
            endpoint,
            data=SchemaParser.serialize_operation(operation),
            headers={"Content-type": "application/json", "Accept": "text/plain"},
        )


def _system_name_lookup(system: Union[str, System]) -> str:
    system_name = str(system)

    with routing_lock:
        return system_name_routes[system_name]


def _system_id_lookup(system_id: str) -> str:
    with routing_lock:
        return system_id_routes[system_id]


def _instance_id_lookup(instance_id: str) -> str:
    with routing_lock:
        return instance_id_routes[instance_id]
