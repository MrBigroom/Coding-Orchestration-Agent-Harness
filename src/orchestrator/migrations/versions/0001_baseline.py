"""baseline schema (frozen after baseline approval)

Revision ID: 0001_baseline
Revises: 
Create Date: 2026-10-19 07:32:05.363171
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001_baseline'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('build_log_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('phase', sa.String(), nullable=True),
    sa.Column('task_id', sa.Integer(), nullable=True),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('event', sa.String(), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('commit_sha', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('build_log_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_build_log_entries_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_build_log_entries_timestamp'), ['timestamp'], unique=False)

    op.create_table('harness_adapters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('command', sa.String(), nullable=False),
    sa.Column('args_template_json', sa.JSON(), nullable=False),
    sa.Column('env_vars_json', sa.JSON(), nullable=False),
    sa.Column('timeout_seconds', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('capabilities_json', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('harness_adapters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_harness_adapters_name'), ['name'], unique=True)

    op.create_table('memory_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.String(), nullable=False),
    sa.Column('project_slug', sa.String(), nullable=True),
    sa.Column('role', sa.String(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('tags_json', sa.JSON(), nullable=False),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('rejected', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('memory_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memory_entries_approved'), ['approved'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_entries_category'), ['category'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_entries_project_slug'), ['project_slug'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_entries_role'), ['role'], unique=False)
        batch_op.create_index(batch_op.f('ix_memory_entries_scope'), ['scope'], unique=False)

    op.create_table('projects',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('slug', sa.String(), nullable=False),
    sa.Column('path', sa.String(), nullable=False),
    sa.Column('base_branch', sa.String(), nullable=False),
    sa.Column('test_command', sa.String(), nullable=True),
    sa.Column('lint_command', sa.String(), nullable=True),
    sa.Column('build_command', sa.String(), nullable=True),
    sa.Column('allowed_commands', sa.JSON(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_projects_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_projects_slug'), ['slug'], unique=True)

    op.create_table('providers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('base_url', sa.String(), nullable=True),
    sa.Column('api_key_env', sa.String(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('providers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_providers_name'), ['name'], unique=True)

    op.create_table('schedules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('cron_expression', sa.String(), nullable=False),
    sa.Column('task_template_json', sa.JSON(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_schedules_name'), ['name'], unique=True)

    op.create_table('settings',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value_json', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('model_profiles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('model_name', sa.String(), nullable=False),
    sa.Column('temperature', sa.Float(), nullable=True),
    sa.Column('max_tokens', sa.Integer(), nullable=False),
    sa.Column('timeout_seconds', sa.Integer(), nullable=False),
    sa.Column('effort', sa.String(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('model_profiles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_model_profiles_name'), ['name'], unique=True)

    op.create_table('tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('short_id', sa.String(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('parent_task_id', sa.Integer(), nullable=True),
    sa.Column('phase_number', sa.Integer(), nullable=True),
    sa.Column('phase_slug', sa.String(), nullable=True),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('risk', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.String(length=16), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('acceptance_criteria', sa.JSON(), nullable=False),
    sa.Column('definition_of_done', sa.JSON(), nullable=False),
    sa.Column('frozen_contracts', sa.JSON(), nullable=False),
    sa.Column('out_of_scope', sa.JSON(), nullable=False),
    sa.Column('branch_name', sa.String(), nullable=True),
    sa.Column('assigned_provider_profile', sa.String(), nullable=True),
    sa.Column('assigned_harness_adapter', sa.String(), nullable=True),
    sa.Column('handoff_status', sa.String(), nullable=True),
    sa.Column('review_status', sa.String(), nullable=True),
    sa.Column('verification_status', sa.String(), nullable=True),
    sa.Column('merge_status', sa.String(), nullable=True),
    sa.Column('created_by', sa.String(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_short_id'), ['short_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_tasks_status'), ['status'], unique=False)

    op.create_table('runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('short_id', sa.String(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('provider_profile', sa.String(), nullable=True),
    sa.Column('harness_adapter', sa.String(), nullable=True),
    sa.Column('command_preview', sa.Text(), nullable=True),
    sa.Column('worktree_path', sa.String(), nullable=True),
    sa.Column('branch_name', sa.String(), nullable=True),
    sa.Column('commit_sha', sa.String(), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('usage', sa.JSON(), nullable=False),
    sa.Column('artifacts_path', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runs_short_id'), ['short_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_runs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_runs_task_id'), ['task_id'], unique=False)

    op.create_table('reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('reviewer_kind', sa.String(), nullable=False),
    sa.Column('reviewer_model', sa.String(), nullable=True),
    sa.Column('verdict', sa.String(length=32), nullable=False),
    sa.Column('findings', sa.JSON(), nullable=False),
    sa.Column('reasoning', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reviews_task_id'), ['task_id'], unique=False)

    op.create_table('verifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('verifier_kind', sa.String(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('checks_json', sa.JSON(), nullable=False),
    sa.Column('evidence_path', sa.String(), nullable=True),
    sa.Column('contradicts_worker', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('verifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_verifications_task_id'), ['task_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('verifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_verifications_task_id'))

    op.drop_table('verifications')
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reviews_task_id'))

    op.drop_table('reviews')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_task_id'))
        batch_op.drop_index(batch_op.f('ix_runs_status'))
        batch_op.drop_index(batch_op.f('ix_runs_short_id'))

    op.drop_table('runs')
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_tasks_short_id'))
        batch_op.drop_index(batch_op.f('ix_tasks_project_id'))

    op.drop_table('tasks')
    with op.batch_alter_table('model_profiles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_model_profiles_name'))

    op.drop_table('model_profiles')
    op.drop_table('settings')
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_schedules_name'))

    op.drop_table('schedules')
    with op.batch_alter_table('providers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_providers_name'))

    op.drop_table('providers')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_slug'))
        batch_op.drop_index(batch_op.f('ix_projects_name'))

    op.drop_table('projects')
    with op.batch_alter_table('memory_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memory_entries_scope'))
        batch_op.drop_index(batch_op.f('ix_memory_entries_role'))
        batch_op.drop_index(batch_op.f('ix_memory_entries_project_slug'))
        batch_op.drop_index(batch_op.f('ix_memory_entries_category'))
        batch_op.drop_index(batch_op.f('ix_memory_entries_approved'))

    op.drop_table('memory_entries')
    with op.batch_alter_table('harness_adapters', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_harness_adapters_name'))

    op.drop_table('harness_adapters')
    with op.batch_alter_table('build_log_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_build_log_entries_timestamp'))
        batch_op.drop_index(batch_op.f('ix_build_log_entries_task_id'))

    op.drop_table('build_log_entries')
