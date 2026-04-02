"""
Plugin loader for the bot — designed for easy extension.

Design approach:
- Any .py file placed in this directory automatically becomes a plugin module.
- Each plugin should export a single setup function named `setup_*handlers(app)`
  where `app` is the telegram.ext.Application instance.
- This module scans plugins/ at bot startup, imports them dynamically, and
  calls their setup function (if present). You can add/remove plugins without
  modifying bot.py or this file beyond placing the module here.

Tip: define small, single-purpose plugins (video, document, help, admin, stats, etc.)
so you can enable/disable or iterate quickly.
"""

import importlib
import pkgutil
import logging
from typing import Any

logger = logging.getLogger(__name__)


def setup_plugins_handlers(app: Any) -> None:
    """
    Discover all modules in this package and call their setup_*handlers(app)
    function if available.

    Parameters:
    app (telegram.ext.Application): Bot application instance to register handlers with.
    """
    # Iterate over all plugins in this folder
    for _, module_name, _ in pkgutil.iter_modules(__path__):
        try:
            module = importlib.import_module(f"{__name__}.{module_name}")
            setup_fn_name = f"setup_{module_name}_handlers"
            setup_fn = getattr(module, setup_fn_name, None)

            if setup_fn is not None:
                setup_fn(app)
                logger.info(
                    "Loaded plugin: %s — called %s(app)",
                    module_name,
                    setup_fn_name,
                )
            else:
                logger.debug(
                    "Plugin found: %s — no setup function named %s()",
                    module_name,
                    setup_fn_name,
                )
        except Exception:
            logger.exception("Failed to load plugin: %s", module_name)
