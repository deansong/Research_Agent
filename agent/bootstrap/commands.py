"""
WHAT:  The slash commands the bootstrap graph accepts.
WHY:   Separate from the generated agent's, which come from its own JSON. Two
       phases, two vocabularies -- which is why commands.parse() takes a
       registry= parameter.
CONCEPT: Not LangGraph.
"""

from __future__ import annotations

from agent.commands import REGISTRY, SlashCommand

BOOTSTRAP_REGISTRY: tuple[SlashCommand, ...] = tuple(
    c for c in REGISTRY if c.scope == "terminal"
) + (
    SlashCommand(
        name="plan",
        scope="graph",
        summary="Stop discussing and design the agent",
        argument="[extra guidance for the designer]",
        contexts=("discussion",),
    ),
    SlashCommand(
        name="approve",
        scope="graph",
        summary="Accept this and continue (the plan, or the designed agent)",
        contexts=("plan_review", "design_review"),
    ),
    SlashCommand(
        name="revise",
        scope="graph",
        summary="Have the planner change the plan",
        argument="<what to change>",
        contexts=("plan_review",),
    ),
    SlashCommand(
        name="retry",
        scope="graph",
        summary="Try designing again",
        argument="[what to do differently]",
        contexts=("design_failed", "design_review"),
    ),
    SlashCommand(
        name="discuss",
        scope="graph",
        summary="Go back to talking it through",
        contexts=("design_failed", "plan_review", "design_review"),
    ),
    SlashCommand(
        name="use",
        scope="graph",
        summary="Give up designing and use the built-in default agent",
        contexts=("design_failed", "design_review"),
    ),
    SlashCommand(
        name="exit",
        scope="graph",
        summary="Stop without running anything",
        aliases=("quit", "q"),
    ),
)
