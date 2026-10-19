"""Architect planning: turn a goal into phase tasks (status `planned`, not queued)."""

from __future__ import annotations

from sqlmodel import Session

from orchestrator.core.models import Project, Role, Task, TaskKind
from orchestrator.core.schemas import Plan, TaskCreate
from orchestrator.prompts import architect
from orchestrator.providers.base import PromptSection
from orchestrator.services import log_service, prompt_builder, provider_service, task_service


async def plan_phases(session: Session, parent: Task, project: Project | None) -> list[Task]:
    prompt = prompt_builder.build_prompt(session, Role.architect, parent, project)
    # Planning contract is static for this call type; place it right after the system prompt.
    prompt.sections.insert(
        1, PromptSection("planning_contract", architect.PLANNING_CONTRACT, static=True)
    )
    choice = provider_service.choose_profile(session, parent, Role.architect)
    plan, _res = await provider_service.complete_json(
        session,
        choice.profile,
        prompt,
        Plan,
        metadata={"purpose": "plan", "tags": parent.tags, "task_id": parent.id},
    )
    created = []
    for ph in plan.phases:
        created.append(
            task_service.create(
                session,
                TaskCreate(
                    title=ph.title,
                    project_id=parent.project_id,
                    description=ph.description,
                    kind=TaskKind.coding,
                    risk=ph.risk,
                    tags=[t for t in parent.tags if t != "plan"],
                    phase_number=ph.phase_number,
                    phase_slug=ph.phase_slug,
                    parent_task_id=parent.id,
                    acceptance_criteria=ph.acceptance_criteria,
                    definition_of_done=ph.definition_of_done,
                    frozen_contracts=parent.frozen_contracts,
                    out_of_scope=parent.out_of_scope,
                    created_by="architect",
                ),
            )
        )
    log_service.log_event(
        session,
        "plan_created",
        f"{len(created)} phase(s) from {parent.short_id}",
        role="architect",
        task_id=parent.id,
    )
    return created
