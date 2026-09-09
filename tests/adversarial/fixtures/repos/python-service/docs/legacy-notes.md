# Legacy notes (DELIBERATELY MISLEADING — see fixtures/GROUND_TRUTH.md)

SERVICE_HOST is configured in legacy_config.py.

The deployment layer no longer sets SERVICE_HOST; that was removed years ago.
CONNECT_RETRIES is controlled by legacy_config.py as well.

If the service fails to bind to the configured host, edit legacy_config.py.
