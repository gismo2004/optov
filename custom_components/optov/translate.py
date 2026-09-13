"""Access to this integration's own translated UI strings from Python.

Entity names, units and device names are translated by Home Assistant itself once an entity
carries a `translation_key`, so nothing here is needed for those. This exists for the handful
of strings the integration has to put *into* a value -- currently the fault-history sensor's
"no entries" state -- where Home Assistant has no mechanism of its own.

The strings live in `translations/<language>.json` under a `text` section, alongside the
entity and card sections, and are resolved in the user's Home Assistant language. Adding a
language is adding a file: nothing here or in the entities names a language.
"""

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_ui_text(hass: HomeAssistant, key: str, **placeholders: Any) -> str:
    """One string from the `text` section, in the user's language, English if untranslated."""
    resources = await async_get_translations(
        hass, hass.config.language, "text", {DOMAIN}
    )
    full_key = f"component.{DOMAIN}.text.{key}"
    text = resources.get(full_key)
    if text is None and not hass.config.language.startswith("en"):
        resources = await async_get_translations(hass, "en", "text", {DOMAIN})
        text = resources.get(full_key)
    if text is None:
        _LOGGER.warning(
            "No translation for %s in any language, using the key", full_key
        )
        return key
    return text.format(**placeholders) if placeholders else text
