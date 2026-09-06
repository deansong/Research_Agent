"""
WHAT:  The `agent` node template -- one structured model turn, configured by
       nodes.json.
WHY:   This single function replaces today's discussor, planner, orchestrator
       and executor. Everything that used to differ between those four files
       is now a field in JSON.
CONCEPT: A LangGraph node is a function (state) -> partial update. This one is
       built by a factory that closes over the node's config, exactly like
       make_planner(backend) does in the hand-written graph.
"""

from __future__ import annotations

from typing import Any

from agent.agentfolder.render import render
from agent.agentfolder.schema import END_TARGET, AgentNodeConfig, BranchSpec, EdgeSpec
from agent.backends.base import Access, BackendError
from agent.git_utils import snapshot
from agent.telemetry import record_usage
from agent.work.state import WorkState, render_context

# Written into `route` when a node's routing value matched no case. The
# compiled path_map sends it to the branch's default.
DEFAULT_ROUTE = "__default__"


def make_agent_node(
    name: str,
    config: AgentNodeConfig,
    *,
    backend,
    output_model,
    transition: EdgeSpec | BranchSpec | None,
):
    """Build the node function for one `kind: "agent"` entry.

    `transition` is this node's single outgoing edge or its branch. The node
    needs it for one reason: if the transition it takes carries an `ask`, the
    node must write `pending` so the human node knows what to show. That is
    why the branch is evaluated TWICE -- once here (to pick the ask) and once
    in the router (to pick the target). Both lookups are pure and read the same
    `cases` list; see the comment in compile.py.
    """

    def agent_node(state: WorkState) -> dict[str, Any]:
        if config.announce:
            print(f"\n[{name}] {config.announce}")

        # ---- step 1: which provider conversation is this? -----------------
        context = render_context(state)
        thread_suffix = render(config.thread_key, context) if config.thread_key else ""
        thread_id_key = f"{name}/{thread_suffix}"
        thread_id = state.get("threads", {}).get(thread_id_key)

        # ---- step 2: first prompt or next prompt? -------------------------
        # THE ONE RULE: `first` when there is no conversation yet, OR when the
        # counter named by refresh_on has moved since this conversation last
        # ran. Otherwise `next`. That reproduces both of the hand-written
        # agent's behaviours -- "no thread yet" and "the plan was revised".
        counters = state.get("counters", {})
        current_mark = int(counters.get(config.refresh_on, 0)) if config.refresh_on else 0
        last_mark = state.get("thread_marks", {}).get(thread_id_key)

        use_first = thread_id is None or (
            bool(config.refresh_on) and last_mark != current_mark
        )
        template = config.prompts.first if use_first else (
            config.prompts.next or config.prompts.first
        )
        prompt = render(template, context)

        # ---- step 3: call the model ---------------------------------------
        run = backend.run_structured(
            thread_id=thread_id,
            repo_path=state["repo_path"],
            access=Access(config.access),
            developer_instructions=config.instructions,
            prompt=prompt,
            output_model=output_model,
        )
        data = run.data.model_dump()

        # ---- step 4: capture anything the model does not report -----------
        if config.capture:
            git = snapshot(state["repo_path"])
            if "git_status" in config.capture:
                data["git_status"] = git.status
            if "git_diff" in config.capture:
                data["git_diff"] = git.diff_stat

        _print_summary(name, config, data)

        # ---- step 5: decide where we are going, and what to ask -----------
        route_value, ask = _resolve_transition(transition, data)

        update: dict[str, Any] = {
            # Each of these returns ONLY this node's own entry. The reducers on
            # these channels merge them; returning the whole dict would erase
            # every other node's. See work/state.py.
            "outputs": {name: data},
            "threads": {thread_id_key: run.thread_id},
            "route": {name: route_value},
            "usage": {thread_id_key: _usage_dict(run.usage)},
        }

        if config.refresh_on:
            update["thread_marks"] = {thread_id_key: current_mark}
        if config.bump:
            update["counters"] = {
                counter: int(counters.get(counter, 0)) + 1 for counter in config.bump
            }
        if config.record:
            # Re-render with this turn's output already visible, so a node can
            # record something it just produced.
            after = render_context({**state, "outputs": {**state.get("outputs", {}), name: data}})
            text = render(config.record, after).strip()
            if text:
                update["transcript"] = [{"role": name, "text": text}]
        if ask is not None:
            update["pending"] = {
                "purpose": ask.purpose,
                "resume_to": ask.resume_to,
                "question": render(ask.question, render_context(
                    {**state, "outputs": {**state.get("outputs", {}), name: data}})),
                "context": render(ask.context, render_context(
                    {**state, "outputs": {**state.get("outputs", {}), name: data}})),
            }

        record_usage(state.get("usage", {}), thread_id_key, run.usage)
        return update

    return agent_node


def _resolve_transition(transition, data: dict) -> tuple[str, Any]:
    """Return (route key, ask spec or None) for the transition being taken."""
    if transition is None:
        return END_TARGET, None

    if isinstance(transition, EdgeSpec):
        # An unconditional edge: the route key is unused by the compiled graph
        # (a plain add_edge needs no router) but we still write it so /state
        # shows where each node went.
        return transition.to, transition.ask

    value = str(data.get(transition.route_on, ""))
    for case in transition.cases:
        if case.when == value:
            return value, case.ask
    return DEFAULT_ROUTE, None


def _usage_dict(usage) -> dict:
    from agent.telemetry import usage_to_dict

    return usage_to_dict(usage)


def _print_summary(name: str, config: AgentNodeConfig, data: dict) -> None:
    """Show the human what happened, without dumping the whole payload."""
    for field in config.output:
        value = data.get(field.name)
        if not value:
            continue
        if field.type == "enum":
            print(f"[{name}] {field.name} = {value}")
        elif field.type == "string" and len(str(value)) > 200:
            print(f"\n[{name}] {field.name}:")
            print(value)
    if data.get("git_diff"):
        print(f"\n[{name}] diff:")
        print(data["git_diff"])
