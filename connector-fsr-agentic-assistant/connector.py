"""FSR Agentic Assistant connector entry point.

Dispatches each declared operation to the matching handler in
`operations.py`. Errors are surfaced as ConnectorError so the platform
records them in the operation execution log.
"""
from connectors.core.connector import Connector, get_logger, ConnectorError
from .operations import (operations, _check_health, _trigger_background_warmup,
                         bind_caller, reset_caller)

logger = get_logger('connector-fsr-agentic-assistant')


class FSRPlaybookBuilder(Connector):
    def execute(self, config, operation_name, params, **kwargs):
        try:
            op = operations.get(operation_name)
            if op is None:
                raise ConnectorError(f"unknown operation: {operation_name}")
            # The caller the platform gateway named, for the reviewer check
            # on approvals (see _decision_gate). Never the widget's claim.
            token = bind_caller(kwargs.get("request"))
            try:
                return op(config, params or {})
            finally:
                reset_caller(token)
        except ConnectorError:
            raise
        except Exception as e:
            logger.exception("operation %s failed: %s", operation_name, e)
            raise ConnectorError(str(e))

    def check_health(self, config):
        try:
            return _check_health(config)
        except Exception as e:
            logger.exception("check_health failed: %s", e)
            raise ConnectorError(str(e))

    # --- Lifecycle hooks -----------------------------------------------------
    # A fresh `$replace` install ships a THIN catalog: the reference DB (inside
    # the fsr_playbooks wheel) has `modules` populated but `module_fields` EMPTY
    # by design, and the connector/op tables are filled only by a warm. Warm it
    # at app-load / config-save instead of paying the cost -- and the field
    # lookups that need it -- on the analyst's first chat.
    # `_trigger_background_warmup` is gated (`_warmup_gate_reason`), logs one
    # decision line, is single-flight, backgrounded, and never raises, so these
    # stay safe no-ops once warm. A `find(kind='field')` that still misses on
    # `module_fields_unwarmed` kicks the same warm (fsr_soc_triage/registry.py).

    def on_app_start(self, config, active):
        if active:
            _trigger_background_warmup(config)

    def on_add_config(self, config, active):
        if active:
            _trigger_background_warmup(config)

    def on_update_config(self, old_config, new_config, active):
        if active:
            _trigger_background_warmup(new_config)

    def on_activate(self, config):
        _trigger_background_warmup(config)
