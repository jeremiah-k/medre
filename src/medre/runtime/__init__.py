"""Runtime orchestration — app lifecycle, builder, errors, route engine.

This package does not expose a stable public API.
Import directly from submodules:
  from medre.runtime.app import MedreApp
  from medre.runtime.builder import RuntimeBuilder
  from medre.runtime.errors import RuntimeStartupError

Route config dataclasses live in the config layer:
  from medre.config.routes import RouteConfig, RouteConfigSet, RouteDirectionality

Runtime route expansion and topology:
  from medre.runtime.route_engine import register_routes
"""
