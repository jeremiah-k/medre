"""Verify that max_attempts defaults are aligned across all retry sources.

All four canonical sources must agree on a default of 3:

1. ``RetryPolicy``          (core/planning — delivery plan retry model)
2. ``RetryConfig``           (config/model — global runtime config)
3. ``RouteRetryConfig``      (config/routes — per-route retry config)
4. ``RetryWorker.__init__``  (runtime/retry — worker parameter default)
"""

from __future__ import annotations

import inspect

from medre.config.model import RetryConfig
from medre.config.routes import RouteRetryConfig
from medre.core.planning.delivery_plan import RetryPolicy
from medre.runtime.retry import RetryWorker

_EXPECTED_MAX_ATTEMPTS = 3


class TestRetryPolicyDefault:
    """RetryPolicy (core planning) defaults."""

    def test_default_max_attempts(self) -> None:
        policy = RetryPolicy()
        assert policy.max_attempts == _EXPECTED_MAX_ATTEMPTS

    def test_dataclass_field_default(self) -> None:
        """Verify the field-level default, not just an instance check."""
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(RetryPolicy)}
        assert fields["max_attempts"].default == _EXPECTED_MAX_ATTEMPTS


class TestRetryConfigDefault:
    """RetryConfig (config/model) defaults."""

    def test_default_max_attempts(self) -> None:
        cfg = RetryConfig()
        assert cfg.max_attempts == _EXPECTED_MAX_ATTEMPTS

    def test_dataclass_field_default(self) -> None:
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(RetryConfig)}
        assert fields["max_attempts"].default == _EXPECTED_MAX_ATTEMPTS


class TestRouteRetryConfigDefault:
    """RouteRetryConfig (config/routes) defaults."""

    def test_default_max_attempts(self) -> None:
        cfg = RouteRetryConfig()
        assert cfg.max_attempts == _EXPECTED_MAX_ATTEMPTS

    def test_dataclass_field_default(self) -> None:
        import dataclasses

        fields = {f.name: f for f in dataclasses.fields(RouteRetryConfig)}
        assert fields["max_attempts"].default == _EXPECTED_MAX_ATTEMPTS


class TestRetryWorkerDefault:
    """RetryWorker (runtime) constructor parameter default."""

    def test_constructor_default_max_attempts(self) -> None:
        sig = inspect.signature(RetryWorker.__init__)
        max_attempts_param = sig.parameters["max_attempts"]
        assert max_attempts_param.default == _EXPECTED_MAX_ATTEMPTS


class TestCrossSourceAlignment:
    """All sources must share the same default value."""

    def test_all_defaults_equal(self) -> None:
        import dataclasses

        _planning_fields = {f.name: f for f in dataclasses.fields(RetryPolicy)}
        _config_fields = {f.name: f for f in dataclasses.fields(RetryConfig)}
        _route_fields = {f.name: f for f in dataclasses.fields(RouteRetryConfig)}
        planning_default = _planning_fields["max_attempts"].default
        config_default = _config_fields["max_attempts"].default
        route_default = _route_fields["max_attempts"].default
        sig = inspect.signature(RetryWorker.__init__)
        worker_default = sig.parameters["max_attempts"].default

        assert planning_default == config_default == route_default == worker_default
        assert planning_default == _EXPECTED_MAX_ATTEMPTS
