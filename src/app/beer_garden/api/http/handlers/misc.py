# -*- coding: utf-8 -*-
import logging

import beer_garden.api.http
import beer_garden.config as config
from beer_garden.api.http.base_handler import BaseHandler

logger = logging.getLogger(__name__)


class ConfigHandler(BaseHandler):
    async def get(self):
        """Subset of configuration options that the frontend needs"""
        app_config = config.get("application")
        auth_config = config.get("auth")

        configs = {
            "allow_unsafe_templates": app_config.allow_unsafe_templates,
            "application_name": app_config.name,
            "icon_default": app_config.icon_default,
            "debug_mode": app_config.debug_mode,
            "auth_enabled": auth_config.enabled,
            "guest_login_enabled": auth_config.guest_login_enabled,
            "url_prefix": config.get("entry.http.url_prefix"),
            "metrics_url": config.get("metrics.prometheus.url"),
            "garden_name": config.get("garden.name"),
        }

        self.write(configs)


class VersionHandler(BaseHandler):
    async def get(self):
        self.write(
            {
                "beer_garden_version": beer_garden.__version__,
                "brew_view_version": beer_garden.__version__,
                "bartender_version": beer_garden.__version__,
                "current_api_version": "v1",
                "supported_api_versions": ["v1"],
            }
        )


class SwaggerConfigHandler(BaseHandler):
    def get(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(
            {
                "url": f"{config.get('entry.http.url_prefix')}api/v1/spec",
                "validatorUrl": None,
            }
        )


class SpecHandler(BaseHandler):
    def get(self):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(beer_garden.api.http.api_spec.to_dict())
