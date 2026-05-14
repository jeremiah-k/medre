"""Docker-based integration tests for MEDRE.

Tests in this package require Docker and are tagged with ``pytest.mark.docker``.
They are excluded from the default test run by the ``addopts`` in
``pyproject.toml`` (``-m 'not live and not docker'``).

To run these tests locally::

    pytest tests/integration/ -m docker -v

Or to run all non-unit tests::

    pytest -m ""  # includes both live and docker
"""
