# Traceability matrix

This file maps requirement areas to implementation modules and associated test definitions. Associations do not establish that every requirement was tested. It is verified by `tests/test_traceability.py`, which fails if a listed file or test function disappears or if a test file is missing from the matrix. Synthetic evidence proves only synthetic behavior; see `docs/ACCEPTANCE.md` for what remains open.

Regenerate with `python scripts/gen_traceability.py` after changing tests.

Evidence labels: **Implemented** (code only), **Locally tested** (executed by the local gate), **Container-tested** (built image under a real runtime), **Hosted-CI-tested** (executed by the hosted workflow), **Not tested**, **Blocked** (cannot execute here; limitation recorded in STATUS.md), **Requires owner action**. Only labels backed by recorded execution appear below. Hosted evidence is limited to the exact revision in STATUS.md.

| Area | Contract | Implementation | Test files | Defined tests | Evidence |
|---|---|---|---|---|---|
| Legal / privacy gate | COMPLIANCE.md, PRD.md | `docs/COMPLIANCE.md`, `docs/PRD.md`, `docs/SECURITY.md` | `tests/test_operational_config.py` | 4 | Blocked |
| TDLib / accounts (offline boundary) | TDLIB_OFFLINE.md | `src/tg_control/adapters/tdlib_boundary.py`, `src/tg_control/adapters/tdlib_poc.py`, `src/tg_control/adapters/tdlib_read_model.py`, `src/tg_control/adapters/tdlib_outcomes.py` | `tests/test_tdlib_boundary.py`, `tests/test_adapter.py`, `tests/test_tdlib_read_model.py`, `tests/test_tdlib_outcomes.py`, `tests/test_native_tdlib.py` | 28 | Locally tested |
| Clients and policy | SECURITY.md | `src/tg_control/auth.py`, `src/tg_control/policy.py`, `src/tg_control/core.py` | `tests/test_policy_auth.py`, `tests/test_core.py` | 27 | Locally tested |
| Read / search / history / media | API.md | `src/tg_control/core.py`, `src/tg_control/adapters/synthetic.py`, `src/tg_control/media.py` | `tests/test_core.py`, `tests/test_media.py`, `tests/test_retention.py` | 37 | Locally tested |
| Write / admin / batch | API.md | `src/tg_control/core.py`, `src/tg_control/schemas.py` | `tests/test_adapter.py`, `tests/test_core.py` | 27 | Locally tested |
| Commands and media pipeline | COMMANDS_MEDIA_AI_PLUGINS.md | `src/tg_control/commands.py`, `src/tg_control/media.py` | `tests/test_commands.py`, `tests/test_commands_api.py`, `tests/test_media.py` | 25 | Locally tested |
| Events | EVENTS_JOBS_WORKFLOWS.md | `src/tg_control/events.py` | `tests/test_events.py`, `tests/test_events_jobs_api.py` | 23 | Locally tested |
| Durable jobs | EVENTS_JOBS_WORKFLOWS.md | `src/tg_control/jobs.py`, `src/tg_control/job_kinds.py` | `tests/test_jobs.py`, `tests/test_events_jobs_api.py` | 35 | Locally tested |
| Workflows | EVENTS_JOBS_WORKFLOWS.md | `src/tg_control/workflows.py` | `tests/test_workflows.py`, `tests/test_workflows_api.py` | 16 | Locally tested |
| AI task contract | COMMANDS_MEDIA_AI_PLUGINS.md | `src/tg_control/ai.py` | `tests/test_ai.py`, `tests/test_ai_api.py` | 12 | Locally tested |
| Plugins | COMMANDS_MEDIA_AI_PLUGINS.md | `src/tg_control/plugins.py` | `tests/test_plugins.py`, `tests/test_plugins_api.py` | 11 | Locally tested |
| Interfaces (HTTP/CLI/MCP/panel) | API.md, MCP.md, CLI.md | `src/tg_control/api.py`, `src/tg_control/cli.py`, `src/tg_control/mcp_server.py`, `src/tg_control/mcp_stdio.py`, `src/tg_control/static` | `tests/test_api.py`, `tests/test_cli.py`, `tests/test_mcp.py`, `tests/test_stdio_process.py`, `tests/test_browser.py` | 29 | Locally tested |
| Audit / retention / instance guard | SECURITY.md, OPERATIONS.md | `src/tg_control/audit.py`, `src/tg_control/cache.py`, `src/tg_control/instance_guard.py`, `src/tg_control/security.py`, `src/tg_control/request_audit.py` | `tests/test_retention.py`, `tests/test_retention_pruning.py`, `tests/test_api.py`, `tests/test_operational_config.py` | 30 | Locally tested |
| Backup / recovery | BACKUP_RESTORE.md, OPERATIONS.md | `src/tg_control/backup.py`, `src/tg_control/backup_probe.py` | `tests/test_backup.py`, `tests/test_backup_catalogs.py`, `tests/test_disaster_recovery.py` | 14 | Locally tested |
| Interface contracts (parity matrix) | CONTRACT_INDEX.md | `src/tg_control/contracts.py`, `docs/contracts/interfaces.json`, `scripts/gen_contracts.py` | `tests/test_contracts.py` | 11 | Locally tested |
| Cursor pagination | API.md | `src/tg_control/pagination.py` | `tests/test_pagination.py` | 10 | Locally tested |
| Observability / health / diagnostics | OPERATIONS.md | `src/tg_control/observability.py`, `src/tg_control/diagnostics.py` | `tests/test_observability.py` | 13 | Locally tested |
| Authentication throttling | SECURITY.md | `src/tg_control/throttle.py` | `tests/test_throttle.py` | 6 | Locally tested |
| Security property boundaries | SECURITY.md | `src/tg_control/errors.py`, `src/tg_control/pagination.py`, `src/tg_control/observability.py` | `tests/test_property_boundaries.py` | 6 | Locally tested |
| Migrations / data model | DATA_MODEL.md | `src/tg_control/db.py`, `alembic/versions` | `tests/test_migrations.py`, `tests/test_postgres_api.py` | 10 | Locally tested |
| Packaging / deployment config | DEPLOYMENT.md, RELEASE_PROCESS.md | `Dockerfile`, `.dockerignore`, `compose.yaml`, `.env.example`, `pyproject.toml`, `requirements.lock` | `tests/test_operational_config.py`, `tests/test_deployment.py` | 15 | Locally tested |
| Deployment lifecycle (host process) | DEPLOYMENT.md | `scripts/runtime_smoke.py`, `src/tg_control/config.py`, `src/tg_control/healthcheck.py`, `docs/DEPLOYMENT.md` | `tests/test_runtime_lifecycle.py` | 1 | Locally tested |
| Deployment preflight | DEPLOYMENT.md | `src/tg_control/preflight.py`, `src/tg_control/config.py`, `src/tg_control/db.py` | `tests/test_preflight.py` | 10 | Locally tested |
| Adapter conformance | PRD.md, SECURITY.md | `src/tg_control/adapters/base.py`, `src/tg_control/adapters/synthetic.py`, `src/tg_control/adapters/tdlib_boundary.py` | `tests/test_adapter_conformance.py`, `tests/test_adapter.py` | 15 | Locally tested |
| Scheduled retention | OPERATIONS.md, API.md | `src/tg_control/retention.py`, `src/tg_control/api.py` | `tests/test_retention_scheduling.py`, `tests/test_retention_pruning.py` | 10 | Locally tested |
| Dependency audit and coverage | TESTING.md, RELEASE_PROCESS.md | `scripts/ci.sh`, `requirements.lock`, `pyproject.toml` | `tests/test_deployment.py` | 11 | Locally tested |
| Release metadata and provenance | RELEASE_PROCESS.md | `src/tg_control/version.py`, `scripts/build_release.py`, `docs/RELEASE_PROCESS.md` | `tests/test_deployment.py` | 11 | Locally tested |
| Hosted CI pipeline | RELEASE_PROCESS.md | `.github/workflows/ci.yaml`, `scripts/ci.sh`, `scripts/container_smoke.sh` | `tests/test_deployment.py` | 11 | Hosted-CI-tested |
| Owner notification / monitoring | OPERATIONS.md | `src/tg_control/monitoring.py` | `tests/test_session_monitor.py`, `tests/test_monitor_progress.py`, `tests/test_api.py` | 28 | Locally tested |
| Delivery / evidence | ACCEPTANCE.md, TRACEABILITY.md | `docs/TRACEABILITY.md`, `docs/ACCEPTANCE.md`, `docs/ROADMAP.md`, `scripts/gen_traceability.py` | `tests/test_traceability.py` | 7 | Locally tested |

## Verification and remaining gaps

Historical hosted workflow [42401029613](https://github.com/20kevit/tg-control/actions/runs/42401029613) passed quality and container jobs at commit `59951afad2c05a7aa114d691ee7e73a026272026`. Its synthetic container smoke is not VPS, production or live Telegram evidence. A subsequent commit requires its own verification; see STATUS.md.

| Requirement | Contract | Status | Why |
|---|---|---|---|
| Image/OS vulnerability scan and base-image digest pinning | RELEASE_PROCESS.md | Requires owner action | Needs registry/network access and approval. |
| Publishing an image, GitHub release, or any deployment | RELEASE_PROCESS.md | Requires owner action | Explicitly out of scope without owner approval. |
| Native TDLib adapter / live Telegram behavior | COMPLIANCE.md | Blocked | Compliance and owner gates unresolved. |

## Test definition inventory

### `tests/test_adapter.py`

- `tests/test_adapter.py::test_accounts_are_isolated_and_writes_remain_local`
- `tests/test_adapter.py::test_lifecycle_and_faults_are_account_scoped`
- `tests/test_adapter.py::test_media_and_missing_message`
- `tests/test_adapter.py::test_offline_tdjson_correlates_clients_and_updates`
- `tests/test_adapter.py::test_tdlib_paths_are_isolated_and_reject_traversal`
- `tests/test_adapter.py::test_invalid_retry_duration_is_rejected`

### `tests/test_adapter_conformance.py`

- `tests/test_adapter_conformance.py::test_adapter_exposes_the_protocol_surface`
- `tests/test_adapter_conformance.py::test_reads_are_account_scoped_and_unknown_chats_are_typed`
- `tests/test_adapter_conformance.py::test_unknown_operations_are_normalized_and_read_only_adapters_refuse_writes`
- `tests/test_adapter_conformance.py::test_media_retention_arguments_are_validated`
- `tests/test_adapter_conformance.py::test_update_draining_is_bounded_and_normalized`
- `tests/test_adapter_conformance.py::test_unready_sessions_are_explicit_and_fail_closed`
- `tests/test_adapter_conformance.py::test_close_is_idempotent_and_refuses_further_service`
- `tests/test_adapter_conformance.py::test_saved_messages_capability_is_declared_not_faked`
- `tests/test_adapter_conformance.py::test_errors_never_carry_upstream_text`

### `tests/test_adapter_error_boundary.py`

- `tests/test_adapter_error_boundary.py::test_exception_constructors_reject_invalid_types`
- `tests/test_adapter_error_boundary.py::test_malformed_preflight_error_does_not_leak_or_execute`
- `tests/test_adapter_error_boundary.py::test_uncertain_failure_never_retries_after_synthetic_effect`
- `tests/test_adapter_error_boundary.py::test_mutated_retry_delay_is_not_used_or_exposed`
- `tests/test_adapter_error_boundary.py::test_mutated_execution_error_is_sanitized_and_unknown`
- `tests/test_adapter_error_boundary.py::test_mutated_delivery_error_never_leaks_read_result`
- `tests/test_adapter_error_boundary.py::test_owner_status_and_reconnect_sanitize_mutated_errors`
- `tests/test_adapter_error_boundary.py::test_reconnect_requires_ready_before_retrying_original_operation`
- `tests/test_adapter_error_boundary.py::test_contradictory_rejection_metadata_never_triggers_retry`
- `tests/test_adapter_error_boundary.py::test_owner_and_monitor_reject_malformed_session_exception`
- `tests/test_adapter_error_boundary.py::test_http_and_mcp_do_not_expose_mutated_adapter_failure`
- `tests/test_adapter_error_boundary.py::test_generic_or_malformed_write_failure_is_conservatively_unknown`

### `tests/test_ai.py`

- `tests/test_ai.py::test_explicit_invocation_and_exact_input_only`
- `tests/test_ai.py::test_telegram_references_and_secrets_are_rejected_before_provider`
- `tests/test_ai.py::test_unknown_task_provider_model_and_capability`
- `tests/test_ai.py::test_input_and_cost_budgets`
- `tests/test_ai.py::test_timeout_cancellation_and_retry_semantics`
- `tests/test_ai.py::test_output_validation_rejects_malformed_provider_output`
- `tests/test_ai.py::test_revoked_credentials_and_registration_rules`
- `tests/test_ai.py::test_catalog_has_no_approved_utility_by_default`
- `tests/test_ai.py::test_audit_includes_no_input_or_output`

### `tests/test_ai_api.py`

- `tests/test_ai_api.py::test_ai_catalog_is_empty_and_owner_only_providers`
- `tests/test_ai_api.py::test_registered_conformance_task_enforces_compliance_over_http`
- `tests/test_ai_api.py::test_ai_requires_client_credential`

### `tests/test_api.py`

- `tests/test_api.py::test_end_to_end_auth_scopes_credential_rotation_and_revocation`
- `tests/test_api.py::test_api_batch_preflight_and_immediate_policy_revocation`
- `tests/test_api.py::test_input_origin_host_size_and_security_headers`
- `tests/test_api.py::test_audit_and_at_rest_secrecy`
- `tests/test_api.py::test_settings_session_health_and_owner_authority`
- `tests/test_api.py::test_invalid_content_is_not_echoed`
- `tests/test_api.py::test_chunked_body_limit_and_duplicate_sensitive_headers`
- `tests/test_api.py::test_health_and_openapi_require_fresh_credentials_and_are_audited`
- `tests/test_api.py::test_health_fails_closed_if_audit_is_unavailable`
- `tests/test_api.py::test_all_methods_enforce_streaming_body_and_framing_boundaries`
- `tests/test_api.py::test_sqlite_single_gateway_excludes_other_processes_and_releases`
- `tests/test_api.py::test_sqlite_guard_loss_rejects_new_requests_and_stays_failed`
- `tests/test_api.py::test_sqlite_guard_rejects_preexisting_symlink_and_insecure_lock`
- `tests/test_api.py::test_single_gateway_guard_releases_when_startup_fails`
- `tests/test_api.py::test_sqlite_instance_guard_sanitizes_path_failures`

### `tests/test_backup.py`

- `tests/test_backup.py::test_encrypted_postgres_backup_restore_and_negative_paths`
- `tests/test_backup.py::test_restore_rejects_authenticated_path_traversal_before_database_access`
- `tests/test_backup.py::test_backup_secret_file_permissions_and_url_errors_are_sanitized`
- `tests/test_backup.py::test_backup_tool_environment_excludes_ambient_secrets_and_libpq_overrides`
- `tests/test_backup.py::test_authenticated_manifest_version_must_be_an_integer`
- `tests/test_backup.py::test_verify_reports_metadata_and_writes_nothing`
- `tests/test_backup.py::test_verify_flags_legacy_and_revision_mismatch`
- `tests/test_backup.py::test_verify_state_archive_requires_a_new_state_dir`
- `tests/test_backup.py::test_verify_rejects_unauthenticated_or_malformed_archives`
- `tests/test_backup.py::test_verify_rejects_invalid_layout_and_dump`
- `tests/test_backup.py::test_verify_cli_prints_report_and_fails_on_incompatibility`

### `tests/test_backup_catalogs.py`

- `tests/test_backup_catalogs.py::test_catalog_probe_rejects_non_table_objects`
- `tests/test_backup_catalogs.py::test_catalog_probe_fails_closed_when_target_cannot_be_inspected`

### `tests/test_browser.py`

- `tests/test_browser.py::test_owner_panel_end_to_end_in_chromium`

### `tests/test_cli.py`

- `tests/test_cli.py::test_cli_uses_api_and_enforces_authorization`
- `tests/test_cli.py::test_cli_rejects_insecure_urls_and_absolute_request_targets`
- `tests/test_cli.py::test_cli_returns_nonzero_for_partial_batch_and_preserves_every_item`
- `tests/test_cli.py::test_cli_requires_complete_correlated_successful_batch_results`

### `tests/test_commands.py`

- `tests/test_commands.py::test_save_text_message_preserves_content_and_audits_metadata_only`
- `tests/test_commands.py::test_save_media_message_preserves_filename_and_caption`
- `tests/test_commands.py::test_save_requires_read_on_source_and_write_on_saved_messages`
- `tests/test_commands.py::test_save_resolves_reply_in_selected_account_only`
- `tests/test_commands.py::test_save_duplicate_invocation_is_rejected`
- `tests/test_commands.py::test_save_unsupported_and_unavailable_content`
- `tests/test_commands.py::test_save_rate_limit_and_unknown_outcome_reporting`
- `tests/test_commands.py::test_save_rejects_revoked_credentials_and_cleans_staged_media`
- `tests/test_commands.py::test_save_media_staging_failure_leaves_no_files`
- `tests/test_commands.py::test_command_catalog_is_explicit`
- `tests/test_commands.py::test_unknown_command_is_rejected`

### `tests/test_commands_api.py`

- `tests/test_commands_api.py::test_command_catalog_and_save_text_over_http`
- `tests/test_commands_api.py::test_save_media_over_http_preserves_filename`
- `tests/test_commands_api.py::test_save_denial_and_duplicate_invocation`
- `tests/test_commands_api.py::test_save_missing_reply_and_invalid_input`
- `tests/test_commands_api.py::test_owner_delegated_save_applies_client_policy`
- `tests/test_commands_api.py::test_save_audit_contains_no_content`

### `tests/test_contracts.py`

- `tests/test_contracts.py::test_registry_json_is_current`
- `tests/test_contracts.py::test_every_route_is_declared_and_every_declaration_exists`
- `tests/test_contracts.py::test_no_duplicate_bindings`
- `tests/test_contracts.py::test_every_client_operation_is_bound_on_all_three_interfaces`
- `tests/test_contracts.py::test_owner_plane_is_http_only_by_design`
- `tests/test_contracts.py::test_mcp_tool_catalog_matches_registry`
- `tests/test_contracts.py::test_cli_catalog_matches_registry`
- `tests/test_contracts.py::test_paginated_operations_expose_cursor_parameters`
- `tests/test_contracts.py::test_error_codes_are_normalized_codes`
- `tests/test_contracts.py::test_core_references_resolve`
- `tests/test_contracts.py::test_authorization_requirements_are_declared`

### `tests/test_core.py`

- `tests/test_core.py::test_discovery_and_search_filter_exclusions_and_accounts`
- `tests/test_core.py::test_pre_authorize_all_and_forward_source_prevents_exfiltration`
- `tests/test_core.py::test_independent_admin_write_and_read`
- `tests/test_core.py::test_write_media_edit_delete_forward_and_item_failures`
- `tests/test_core.py::test_replay_is_rejected_and_audit_has_no_content`
- `tests/test_core.py::test_rate_limit_wait_timeout_reconnect_and_unknown_failures`
- `tests/test_core.py::test_revocation_during_wait_stops_retry`
- `tests/test_core.py::test_scope_revocation_during_search_retry_does_not_leak`
- `tests/test_core.py::test_audit_failure_prevents_effects`
- `tests/test_core.py::test_timeout_of_inflight_mutation_is_unknown`
- `tests/test_core.py::test_two_clients_two_accounts_and_authorized_cross_account_search`
- `tests/test_core.py::test_revocation_while_read_in_flight_never_returns_content`
- `tests/test_core.py::test_scope_revoked_during_chat_lookup_does_not_authorize`
- `tests/test_core.py::test_earlier_read_in_batch_is_not_delivered_after_later_revocation`
- `tests/test_core.py::test_search_delivery_filters_chat_revoked_after_earlier_result`
- `tests/test_core.py::test_positive_rate_limits_are_retried_beyond_eight_when_deadline_allows`
- `tests/test_core.py::test_zero_delay_rate_limit_spin_guard_never_causes_effect`
- `tests/test_core.py::test_retry_wait_and_reauthorization_are_deadline_bounded_without_unknown_effect`
- `tests/test_core.py::test_owner_session_status_and_reconnect_are_bounded_and_audited`
- `tests/test_core.py::test_owner_session_adapter_errors_are_sanitized_and_fail_closed`
- `tests/test_core.py::test_reauthentication_state_always_requires_owner_intervention`

### `tests/test_deployment.py`

- `tests/test_deployment.py::test_dockerfile_is_multi_stage_non_root_and_secret_free`
- `tests/test_deployment.py::test_dockerignore_excludes_secrets_state_and_tooling`
- `tests/test_deployment.py::test_compose_is_loopback_only_without_docker_socket_or_privileges`
- `tests/test_deployment.py::test_compose_declares_resource_limits_log_rotation_and_graceful_stop`
- `tests/test_deployment.py::test_compose_secrets_are_mounted_files_and_environment_has_no_values`
- `tests/test_deployment.py::test_migrations_are_an_explicit_one_shot_service_not_a_startup_action`
- `tests/test_deployment.py::test_example_environment_contains_placeholders_only`
- `tests/test_deployment.py::test_hosted_ci_workflow_is_pinned_least_privilege_and_runs_the_local_gate`
- `tests/test_deployment.py::test_release_build_script_refuses_to_publish_and_records_provenance`
- `tests/test_deployment.py::test_release_build_is_byte_reproducible`
- `tests/test_deployment.py::test_dockerfile_installs_the_pinned_build_backends_before_building`

### `tests/test_disaster_recovery.py`

- `tests/test_disaster_recovery.py::test_complete_synthetic_backup_verify_restore_and_compare`

### `tests/test_events.py`

- `tests/test_events.py::test_envelope_validation_rejects_malformed_and_oversized_input`
- `tests/test_events.py::test_ingestion_is_durable_and_deduplicated`
- `tests/test_events.py::test_subscription_requires_read_scope_and_resolvable_chats`
- `tests/test_events.py::test_pull_filters_and_rechecks_read_scope_before_delivery`
- `tests/test_events.py::test_cursor_persistence_ack_monotonicity_and_conflict`
- `tests/test_events.py::test_backpressure_isolates_a_slow_consumer`
- `tests/test_events.py::test_nack_retries_then_dead_letters_with_controlled_replay`
- `tests/test_events.py::test_adapter_update_drain_ingests_once_and_keeps_accounts_isolated`
- `tests/test_events.py::test_operational_events_are_never_delivered_to_clients`
- `tests/test_events.py::test_paused_subscription_and_pruned_cursor`
- `tests/test_events.py::test_subscription_deletion_removes_cursor_state`
- `tests/test_events.py::test_event_error_body_contains_no_upstream_detail`

### `tests/test_events_jobs_api.py`

- `tests/test_events_jobs_api.py::test_event_subscription_lifecycle_and_ack_over_http`
- `tests/test_events_jobs_api.py::test_event_subscription_isolated_between_clients`
- `tests/test_events_jobs_api.py::test_event_subscription_requires_read_scope`
- `tests/test_events_jobs_api.py::test_dead_letter_owner_workflow_over_http`
- `tests/test_events_jobs_api.py::test_adapter_updates_reach_subscribers_after_ingestion`
- `tests/test_events_jobs_api.py::test_job_lifecycle_over_http`
- `tests/test_events_jobs_api.py::test_job_validation_and_authorization_errors`
- `tests/test_events_jobs_api.py::test_job_cancel_and_pause_over_http`
- `tests/test_events_jobs_api.py::test_event_ingestion_rejects_cross_account_and_unknown_chat`
- `tests/test_events_jobs_api.py::test_settings_include_event_retention`
- `tests/test_events_jobs_api.py::test_public_group_broad_grant_still_excluded_from_events`

### `tests/test_jobs.py`

- `tests/test_jobs.py::test_state_machine_rejects_invalid_transitions`
- `tests/test_jobs.py::test_timezone_dst_skip_and_repeat`
- `tests/test_jobs.py::test_schedule_validation`
- `tests/test_jobs.py::test_enqueue_requires_known_kind_valid_input_and_credentials`
- `tests/test_jobs.py::test_due_job_runs_once_with_persisted_history`
- `tests/test_jobs.py::test_retry_only_for_definite_failures`
- `tests/test_jobs.py::test_unknown_outcome_is_terminal_and_never_retried`
- `tests/test_jobs.py::test_handler_timeout_marks_mutations_unknown`
- `tests/test_jobs.py::test_pause_resume_cancel_and_deadline`
- `tests/test_jobs.py::test_cancellation_during_run_becomes_cancelled_only_without_effect`
- `tests/test_jobs.py::test_expired_lease_recovery_is_conservative`
- `tests/test_jobs.py::test_non_mutating_lease_recovery_retries`
- `tests/test_jobs.py::test_misfire_policy_skip_and_run_once`
- `tests/test_jobs.py::test_recurring_daily_job_advances_after_success`
- `tests/test_jobs.py::test_jobs_are_isolated_per_client`
- `tests/test_jobs.py::test_registry_rejects_duplicates_and_invalid_kinds`
- `tests/test_jobs.py::test_scheduler_tick_and_graceful_close`
- `tests/test_jobs.py::test_delayed_send_reevaluates_authorization_at_execution`
- `tests/test_jobs.py::test_delayed_send_denied_when_policy_revoked_before_execution`
- `tests/test_jobs.py::test_delayed_send_blocked_when_credential_revoked`
- `tests/test_jobs.py::test_unauthorized_delayed_send_is_rejected_at_creation`
- `tests/test_jobs.py::test_job_inputs_cannot_carry_credentials_or_unknown_fields`
- `tests/test_jobs.py::test_gateway_execution_key_prevents_duplicate_delayed_effect`
- `tests/test_jobs.py::test_cancelled_execution_is_recorded_as_unknown_and_never_retried`

### `tests/test_mcp.py`

- `tests/test_mcp.py::test_mcp_streamable_http_auth_tools_scope_and_revocation`
- `tests/test_mcp.py::test_mcp_invalid_input_never_echoes_content`
- `tests/test_mcp.py::test_mcp_sdk_stdio_backend_over_shared_api`
- `tests/test_mcp.py::test_official_sdk_streamable_http_client`
- `tests/test_mcp.py::test_mcp_partial_batch_is_error_but_reports_all_outcomes`
- `tests/test_mcp.py::test_mcp_does_not_claim_success_for_malformed_batch_results`
- `tests/test_mcp.py::test_stdio_mcp_does_not_ignore_http_failure_status`
- `tests/test_mcp.py::test_mcp_is_bearer_only_and_advertises_no_oauth_metadata`

### `tests/test_media.py`

- `tests/test_media.py::test_stage_verifies_checksum_and_releases`
- `tests/test_media.py::test_untrusted_filename_and_content_type_are_validated`
- `tests/test_media.py::test_signature_size_and_checksum_mismatches_are_rejected`
- `tests/test_media.py::test_limits_and_quotas_are_enforced`
- `tests/test_media.py::test_streaming_cancellation_leaves_no_partial_files`
- `tests/test_media.py::test_guard_failure_aborts_staging_and_cleanup_expires_files`
- `tests/test_media.py::test_root_must_be_private_and_not_a_symlink`
- `tests/test_media.py::test_multi_chunk_streaming_keeps_memory_bounded_and_verifies_digest`

### `tests/test_migrations.py`

- `tests/test_migrations.py::test_sqlite_migration`
- `tests/test_migrations.py::test_postgres_migration_and_storage`
- `tests/test_migrations.py::test_incremental_index_migration_preserves_audit_and_old_schema_fails_startup`
- `tests/test_migrations.py::test_migration_rollback_and_reapply_on_disposable_sqlite`
- `tests/test_migrations.py::test_partial_schema_is_rejected_even_when_the_revision_matches`
- `tests/test_migrations.py::test_unknown_revision_is_reported_and_refused`
- `tests/test_migrations.py::test_migration_policy_is_explicit_and_never_automatic`

### `tests/test_monitor_progress.py`

- `tests/test_monitor_progress.py::test_progress_records_failure_recovery_and_monotonic_age`
- `tests/test_monitor_progress.py::test_owner_can_inspect_monitor_failure_recovery_and_stopped_task`
- `tests/test_monitor_progress.py::test_owner_can_see_an_in_progress_monitor_pass`

### `tests/test_native_tdlib.py`

- `tests/test_native_tdlib.py::test_native_tdjson_offline_two_client_correlation`

### `tests/test_observability.py`

- `tests/test_observability.py::test_structured_logs_are_json_bounded_and_redact_sensitive_fields`
- `tests/test_observability.py::test_log_event_never_raises_on_hostile_values`
- `tests/test_observability.py::test_safe_fields_bounds_everything`
- `tests/test_observability.py::test_metrics_reject_unknown_labels_and_normalize_outcomes`
- `tests/test_observability.py::test_metrics_cardinality_is_capped`
- `tests/test_observability.py::test_spans_capture_hierarchy_duration_and_no_payloads`
- `tests/test_observability.py::test_span_records_failure_code_and_reraises`
- `tests/test_observability.py::test_health_ready_and_dependency_endpoints`
- `tests/test_observability.py::test_metrics_traces_and_diagnostics_are_owner_only_and_bounded`
- `tests/test_observability.py::test_diagnostics_surface_failed_jobs_and_quarantined_plugins`
- `tests/test_observability.py::test_request_id_is_echoed_and_correlates_errors`
- `tests/test_observability.py::test_error_envelope_never_echoes_input`
- `tests/test_observability.py::test_log_level_is_configurable_and_bounded`

### `tests/test_operational_config.py`

- `tests/test_operational_config.py::test_synthetic_secret_initialization_never_overwrites_or_prints_values`
- `tests/test_operational_config.py::test_compose_security_posture_is_local_only`
- `tests/test_operational_config.py::test_secret_setup_refuses_a_single_existing_target_without_partial_writes`
- `tests/test_operational_config.py::test_healthcheck_invalid_config_fails_without_disclosing_file_contents`

### `tests/test_pagination.py`

- `tests/test_pagination.py::test_cursor_roundtrip_is_deterministic_and_scope_bound`
- `tests/test_pagination.py::test_cursor_rejects_tampering_and_malformed_input`
- `tests/test_pagination.py::test_cursor_expiry_and_identifier_validation`
- `tests/test_pagination.py::test_limit_bounds_and_scope_digest`
- `tests/test_pagination.py::test_job_pagination_first_intermediate_final_empty_and_isolation`
- `tests/test_pagination.py::test_pagination_handles_concurrent_inserts_and_deletions`
- `tests/test_pagination.py::test_audit_pagination_and_legacy_before_id`
- `tests/test_pagination.py::test_workflow_run_pagination`
- `tests/test_pagination.py::test_page_metadata_is_bounded_and_typed`
- `tests/test_pagination.py::test_cursor_identifiers_are_not_plaintext`

### `tests/test_plugins.py`

- `tests/test_plugins.py::test_manifest_rejects_unsafe_or_incoherent_declarations`
- `tests/test_plugins.py::test_install_requires_explicit_grants_and_binding`
- `tests/test_plugins.py::test_plugin_cannot_use_capabilities_or_principal_it_was_not_granted`
- `tests/test_plugins.py::test_enabled_plugin_acts_under_delegated_client_policy`
- `tests/test_plugins.py::test_rate_limit_timeout_and_audit_containment`
- `tests/test_plugins.py::test_repeated_failures_quarantine_without_affecting_core`
- `tests/test_plugins.py::test_upgrade_requires_new_version_and_rebinding`
- `tests/test_plugins.py::test_concurrent_plugin_calls_are_bounded`

### `tests/test_plugins_api.py`

- `tests/test_plugins_api.py::test_plugin_registry_is_owner_only_and_starts_empty`
- `tests/test_plugins_api.py::test_install_grant_enable_and_quarantine_over_http`
- `tests/test_plugins_api.py::test_plugin_manifest_validation_over_http`

### `tests/test_policy_auth.py`

- `tests/test_policy_auth.py::test_independent_scopes_and_exclusion_matrix`
- `tests/test_policy_auth.py::test_private_type_grants_are_rejected`
- `tests/test_policy_auth.py::test_token_rotation_revocation_and_no_plaintext_storage`
- `tests/test_policy_auth.py::test_audit_retention_and_metadata_only`
- `tests/test_policy_auth.py::test_settings_require_valid_secrets`
- `tests/test_policy_auth.py::test_secret_files_require_private_permissions_and_bounded_content`

### `tests/test_postgres_api.py`

- `tests/test_postgres_api.py::test_postgres_api_round_trip`
- `tests/test_postgres_api.py::test_postgres_single_gateway_guard_and_loss_are_fail_closed`
- `tests/test_postgres_api.py::test_guard_sanitizes_driver_and_cleanup_failures`

### `tests/test_preflight.py`

- `tests/test_preflight.py::test_preflight_passes_on_a_valid_private_deployment`
- `tests/test_preflight.py::test_missing_configuration_fails_with_variable_names_only`
- `tests/test_preflight.py::test_group_readable_secret_file_fails_closed`
- `tests/test_preflight.py::test_insecure_state_directory_is_a_storage_failure`
- `tests/test_preflight.py::test_unreachable_database_and_stale_schema_are_database_failures`
- `tests/test_preflight.py::test_disk_headroom_check_and_duplicate_backup_key_warning`
- `tests/test_preflight.py::test_configuration_failure_takes_precedence_over_storage`
- `tests/test_preflight.py::test_unexpected_internal_failure_reports_only_the_exception_type`
- `tests/test_preflight.py::test_ephemeral_media_staging_uses_a_lower_headroom_threshold`
- `tests/test_preflight.py::test_unmigrated_database_is_missing_not_unreachable`

### `tests/test_property_boundaries.py`

- `tests/test_property_boundaries.py::test_error_codes_are_always_safe_tokens`
- `tests/test_property_boundaries.py::test_cursor_round_trip_and_single_character_tamper_rejection`
- `tests/test_property_boundaries.py::test_cursor_never_authenticates_for_a_different_scope`
- `tests/test_property_boundaries.py::test_metric_labels_and_outcomes_are_bounded`
- `tests/test_property_boundaries.py::test_json_log_redaction_with_hostile_nested_values`
- `tests/test_property_boundaries.py::test_throttle_invariants_hold_for_random_sequences`

### `tests/test_retention.py`

- `tests/test_retention.py::test_encrypted_account_storage_retention_and_permanent_option`
- `tests/test_retention.py::test_store_rejects_unsafe_account_paths_and_permissions`
- `tests/test_retention.py::test_cache_ttl_capacity_and_inflight_invalidation`
- `tests/test_retention.py::test_cache_rechecks_policy_and_invalidates_on_mutation`
- `tests/test_retention.py::test_inflight_read_cannot_repopulate_cache_after_write`
- `tests/test_retention.py::test_http_permanent_media_survives_restart`
- `tests/test_retention.py::test_failed_encrypted_write_does_not_mutate_in_memory`
- `tests/test_retention.py::test_media_maintenance_includes_disabled_accounts_and_audits`

### `tests/test_retention_pruning.py`

- `tests/test_retention_pruning.py::test_job_pruning_is_bounded_dry_run_first_and_audited`
- `tests/test_retention_pruning.py::test_workflow_run_pruning_never_touches_unfinished_runs`
- `tests/test_retention_pruning.py::test_pruning_requires_owner_and_bounded_request`

### `tests/test_retention_scheduling.py`

- `tests/test_retention_scheduling.py::test_policy_defaults_are_disabled_and_bounded`
- `tests/test_retention_scheduling.py::test_disabled_policy_never_runs_and_never_prunes`
- `tests/test_retention_scheduling.py::test_runs_once_per_interval_and_only_while_the_guard_is_held`
- `tests/test_retention_scheduling.py::test_a_failing_step_does_not_block_the_others_and_is_audited`
- `tests/test_retention_scheduling.py::test_audit_write_failure_is_visible_in_scheduled_diagnostics`
- `tests/test_retention_scheduling.py::test_api_auto_prune_deletes_only_eligible_rows_and_audits`
- `tests/test_retention_scheduling.py::test_owner_enables_auto_prune_and_diagnostics_report_it`

### `tests/test_runtime_lifecycle.py`

- `tests/test_runtime_lifecycle.py::test_host_process_lifecycle_and_failure_modes`

### `tests/test_session_monitor.py`

- `tests/test_session_monitor.py::test_session_transitions_are_durable_deduplicated_and_account_scoped`
- `tests/test_session_monitor.py::test_monitor_bounds_status_calls_and_sanitizes_errors`
- `tests/test_session_monitor.py::test_monitor_normalizes_unavailable_and_malformed_statuses`
- `tests/test_session_monitor.py::test_monitor_reapplies_error_allowlist`
- `tests/test_session_monitor.py::test_monitor_rechecks_account_enabled_after_inflight_status`
- `tests/test_session_monitor.py::test_failed_audit_does_not_advance_monitor_state`
- `tests/test_session_monitor.py::test_monitor_rejects_corrupt_persisted_state`
- `tests/test_session_monitor.py::test_monitor_checks_all_accounts_with_bounded_concurrency`
- `tests/test_session_monitor.py::test_background_monitor_retries_sanitizes_and_cancels_on_shutdown`
- `tests/test_session_monitor.py::test_filtered_session_history_is_owner_only_and_sanitized`

### `tests/test_stdio_process.py`

- `tests/test_stdio_process.py::test_stdio_executable_uses_gateway_and_revocation`

### `tests/test_tdlib_boundary.py`

- `tests/test_tdlib_boundary.py::test_live_mode_fails_closed_without_override`
- `tests/test_tdlib_boundary.py::test_offline_flow_makes_no_network_calls`
- `tests/test_tdlib_boundary.py::test_authorization_state_machine_rejects_credentials_and_live_states`
- `tests/test_tdlib_boundary.py::test_updates_are_deduplicated_ordered_bounded_and_scoped`
- `tests/test_tdlib_boundary.py::test_reads_paginate_and_refuse_mutations_or_unsafe_chats`
- `tests/test_tdlib_boundary.py::test_reconnect_requires_fresh_synthetic_fixture`

### `tests/test_tdlib_outcomes.py`

- `tests/test_tdlib_outcomes.py::test_ack_is_not_success_and_success_is_correlated_without_content`
- `tests/test_tdlib_outcomes.py::test_never_confuse_accounts_chats_or_temporary_message_ids`
- `tests/test_tdlib_outcomes.py::test_failure_exposes_only_metadata_and_exact_server_delay`
- `tests/test_tdlib_outcomes.py::test_retry_is_not_authorized_for_changed_payload_payment_or_bad_delay`
- `tests/test_tdlib_outcomes.py::test_irrecoverable_deletion_or_malformed_success_is_unknown_not_retryable`
- `tests/test_tdlib_outcomes.py::test_identifiers_are_validated`
- `tests/test_tdlib_outcomes.py::test_offline_authorization_states_fail_closed_without_retaining_details`
- `tests/test_tdlib_outcomes.py::test_offline_router_rejects_invalid_or_ambiguous_identifiers`

### `tests/test_tdlib_read_model.py`

- `tests/test_tdlib_read_model.py::test_chat_classification_respects_private_and_public_types`
- `tests/test_tdlib_read_model.py::test_public_exclusions_and_private_explicit_grants_use_conservative_kind`
- `tests/test_tdlib_read_model.py::test_malformed_or_ambiguous_supergroup_never_falls_back_to_a_grant`
- `tests/test_tdlib_read_model.py::test_text_screen_is_structural_only_and_requires_fresh_known_chat`
- `tests/test_tdlib_read_model.py::test_protected_expiring_malformed_and_unsupported_content_is_rejected`
- `tests/test_tdlib_read_model.py::test_missing_safety_fields_fail_closed`
- `tests/test_tdlib_read_model.py::test_chat_protection_unknown_secret_and_message_timers_fail_closed`

### `tests/test_throttle.py`

- `tests/test_throttle.py::test_throttle_locks_out_then_recovers_after_the_window`
- `tests/test_throttle.py::test_throttle_keys_are_digests_and_bounded`
- `tests/test_throttle.py::test_throttle_configuration_is_bounded`
- `tests/test_throttle.py::test_api_throttles_repeated_invalid_credentials_with_retry_after`
- `tests/test_throttle.py::test_a_valid_credential_resets_the_failure_history`
- `tests/test_throttle.py::test_auth_failures_and_lockouts_are_audited_without_the_credential`

### `tests/test_traceability.py`

- `tests/test_traceability.py::test_every_matrix_reference_exists`
- `tests/test_traceability.py::test_every_listed_test_function_exists`
- `tests/test_traceability.py::test_every_test_file_is_listed`
- `tests/test_traceability.py::test_matrix_does_not_claim_live_evidence`
- `tests/test_traceability.py::test_every_area_row_has_an_evidence_label_and_a_contract`
- `tests/test_traceability.py::test_hosted_pipeline_evidence_has_revision_and_run`
- `tests/test_traceability.py::test_markdown_local_link_targets_exist`

### `tests/test_workflows.py`

- `tests/test_workflows.py::test_definition_validation_rejects_unsafe_or_unbounded_shapes`
- `tests/test_workflows.py::test_create_requires_known_enabled_client`
- `tests/test_workflows.py::test_manual_run_executes_actions_through_core`
- `tests/test_workflows.py::test_conditions_and_parameters_control_steps`
- `tests/test_workflows.py::test_dry_run_authorizes_without_effects`
- `tests/test_workflows.py::test_step_denial_stops_or_continues_per_policy`
- `tests/test_workflows.py::test_definition_versions_are_immutable_per_run`
- `tests/test_workflows.py::test_disabled_workflow_and_unknown_ids`
- `tests/test_workflows.py::test_step_rechecks_authorization_after_earlier_effect`
- `tests/test_workflows.py::test_interrupted_run_recovery_is_conservative`
- `tests/test_workflows.py::test_run_with_revoked_credential_does_not_execute`
- `tests/test_workflows.py::test_run_history_is_owner_visible_and_bounded`
- `tests/test_workflows.py::test_concurrent_runs_are_bounded_and_refused_without_side_effects`

### `tests/test_workflows_api.py`

- `tests/test_workflows_api.py::test_workflow_owner_lifecycle_over_http`
- `tests/test_workflows_api.py::test_workflow_denied_when_client_policy_revoked`
- `tests/test_workflows_api.py::test_workflow_validation_and_disable`
