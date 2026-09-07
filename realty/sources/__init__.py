"""Реєстр джерел даних."""
from __future__ import annotations

from .base import BaseSource
from .blago import BlagoSource
from .domria import DomRiaSource
from .flombu import FlombuSource
from .lun import LunSource
from .olx import OlxSource

REGISTRY: dict[str, type[BaseSource]] = {
    "domria": DomRiaSource,
    "lun": LunSource,
    "flombu": FlombuSource,
    "olx": OlxSource,
    "blago": BlagoSource,
}

__all__ = ["REGISTRY", "BaseSource"]
