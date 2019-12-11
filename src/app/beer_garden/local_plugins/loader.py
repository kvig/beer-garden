# -*- coding: utf-8 -*-
import logging
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import List

from brewtils.models import Instance, System

import beer_garden.config
import beer_garden.db.api as db
import beer_garden.local_plugins.validator as validator
from beer_garden.errors import PluginValidationError
from beer_garden.local_plugins.plugin_runner import LocalPluginRunner
from beer_garden.local_plugins.registry import LocalPluginRegistry
from beer_garden.local_plugins.validator import CONFIG_NAME
from beer_garden.systems import create_system


class LocalPluginLoader(object):
    """Class that helps with loading local plugins"""

    logger = logging.getLogger(__name__)

    _instance = None

    def __init__(
        self,
        plugin_dir=None,
        log_dir=None,
        connection_info=None,
        username=None,
        password=None,
    ):
        self.registry = LocalPluginRegistry.instance()

        self._plugin_path = Path(plugin_dir) if plugin_dir else None
        self._log_dir = log_dir
        self._connection_info = connection_info
        self._username = username
        self._password = password

    @classmethod
    def instance(cls):
        if not cls._instance:
            cls._instance = cls(
                plugin_dir=beer_garden.config.get("plugin.local.directory"),
                log_dir=beer_garden.config.get("plugin.local.log_directory"),
                connection_info=beer_garden.config.get("entry.http"),
                username=beer_garden.config.get("plugin.local.auth.username"),
                password=beer_garden.config.get("plugin.local.auth.password"),
            )
        return cls._instance

    def load_plugins(self, path: str = None) -> None:
        """Load all plugins

        After each has been loaded, it checks the requirements to ensure
        the plugin can be loaded correctly.

        Args:
            path: The path to scan for plugins. If none will default to the
            plugin path specified at initialization.
        """
        for plugin_path in self.scan_plugin_path(path=path):
            try:
                self.load_plugin(plugin_path)
            except Exception as ex:
                self.logger.exception(
                    "Exception while loading plugin %s: %s", plugin_path, ex
                )

    def scan_plugin_path(self, path: Path = None) -> List[Path]:
        """Find valid plugin directories in a given path.

        Note: This scan does not walk the directory tree - all plugins must be
        in the top level of the given path.

        Args:
            path: The path to scan for plugins. If none will default to the
                plugin path specified at initialization.

        Returns:
            Potential paths containing plugins
        """
        path = path or self._plugin_path

        if path is None:
            return []

        return [x for x in path.iterdir() if x.is_dir()]

    def load_plugin(self, plugin_path: Path) -> List[LocalPluginRunner]:
        """Loads a plugin given a path to a plugin directory.

        It will use the validator to validate the plugin before registering the
        plugin in the database as well as adding an entry to the plugin map.

        Args:
            plugin_path: The path of the plugin

        Returns:
            A list of plugin runners

        """
        if not plugin_path or not plugin_path.is_dir():
            raise PluginValidationError(f"Plugin path {plugin_path} is not a directory")

        try:
            plugin_config = self._load_config(plugin_path)
        except PluginValidationError as ex:
            self.logger.error(f"Error loading config for plugin at {plugin_path}: {ex}")
            return []

        config_name = plugin_config["NAME"]
        config_version = plugin_config["VERSION"]
        config_entry = plugin_config["PLUGIN_ENTRY"]
        config_instances = plugin_config["INSTANCES"]
        config_args = plugin_config["PLUGIN_ARGS"]

        plugin_id = None
        plugin_commands = []
        plugin_instances = [Instance(name=name) for name in config_instances]

        # If this system already exists we need to do some stuff
        existing_system = db.query_unique(
            System, name=config_name, version=config_version
        )
        if existing_system:
            # Carry these over to the new system wholesale
            plugin_id = existing_system.id
            plugin_commands = existing_system.commands

            # Any previously existing instances should keep the same id
            for instance in plugin_instances:
                if existing_system.has_instance(instance.name):
                    instance.id = existing_system.get_instance(instance.name).id

            # And any instances that no longer exist should be removed
            for instance in existing_system.instances:
                if instance.name not in config_instances:
                    db.delete(instance)

        plugin_system = System(
            id=plugin_id,
            name=config_name,
            version=config_version,
            commands=plugin_commands,
            instances=plugin_instances,
            max_instances=len(plugin_instances),
            description=plugin_config.get("DESCRIPTION"),
            icon_name=plugin_config.get("ICON_NAME"),
            display_name=plugin_config.get("DISPLAY_NAME"),
            metadata=plugin_config.get("METADATA"),
        )

        plugin_system = create_system(plugin_system)

        plugin_list = []
        for instance in plugin_instances:
            # TODO - Local plugin runner shouldn't require HTTP entry point
            plugin = LocalPluginRunner(
                config_entry,
                plugin_system,
                instance.name,
                str(plugin_path),
                self._connection_info.host,
                self._connection_info.port,
                ssl_enabled=self._connection_info.ssl.enabled,
                plugin_args=config_args.get(instance.name),
                environment=plugin_config["ENVIRONMENT"],
                requirements=plugin_config["REQUIRES"],
                plugin_log_directory=self._log_dir,
                url_prefix=self._connection_info.url_prefix,
                ca_verify=False,
                ca_cert=self._connection_info.ssl.ca_cert,
                username=self._username,
                password=self._password,
                log_level=plugin_config["LOG_LEVEL"],
            )

            self.registry.register_plugin(plugin)
            plugin_list.append(plugin)

        return plugin_list

    def _load_config(self, plugin_path: Path) -> dict:
        """Loads a plugin config"""
        config_file = plugin_path / CONFIG_NAME

        if not config_file.exists():
            raise PluginValidationError("Config file does not exist")

        if not config_file.is_file():
            raise PluginValidationError("Config file is not actually a file")

        # Need to construct our own Loader here, the default doesn't work with .conf
        loader = SourceFileLoader("bg_plugin_config", str(config_file))
        spec = spec_from_file_location("bg_plugin_config", config_file, loader=loader)
        config_module = module_from_spec(spec)
        spec.loader.exec_module(config_module)

        validator.validate_config(config_module, plugin_path)

        instances = getattr(config_module, "INSTANCES", None)
        plugin_args = getattr(config_module, "PLUGIN_ARGS", None)
        log_name = getattr(config_module, "LOG_LEVEL", "INFO")
        log_level = getattr(logging, str(log_name).upper(), logging.INFO)

        if instances is None and plugin_args is None:
            instances = ["default"]
            plugin_args = {"default": None}

        elif plugin_args is None:
            plugin_args = {}
            for instance_name in instances:
                plugin_args[instance_name] = None

        elif instances is None:
            if isinstance(plugin_args, list):
                instances = ["default"]
                plugin_args = {"default": plugin_args}
            elif isinstance(plugin_args, dict):
                instances = list(plugin_args.keys())
            else:
                raise ValueError("Unknown plugin args type: %s" % plugin_args)

        elif isinstance(plugin_args, list):
            temp_args = {}
            for instance_name in instances:
                temp_args[instance_name] = plugin_args

            plugin_args = temp_args

        config = {
            "NAME": config_module.NAME,
            "VERSION": config_module.VERSION,
            "PLUGIN_ENTRY": config_module.PLUGIN_ENTRY,
            "INSTANCES": instances,
            "PLUGIN_ARGS": plugin_args,
            "LOG_LEVEL": log_level,
            "DESCRIPTION": getattr(config_module, "DESCRIPTION", ""),
            "ICON_NAME": getattr(config_module, "ICON_NAME", None),
            "DISPLAY_NAME": getattr(config_module, "DISPLAY_NAME", None),
            "REQUIRES": getattr(config_module, "REQUIRES", []),
            "ENVIRONMENT": getattr(config_module, "ENVIRONMENT", {}),
            "METADATA": getattr(config_module, "METADATA", {}),
        }

        return config
