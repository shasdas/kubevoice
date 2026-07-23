"""
KubeVoice eval suite — behavioral tests using LiveKit Agents' testing
framework (text-only, no audio pipeline, no real cluster needed).

Run:
    uv run pytest tests/test_agent.py -v

Verbose per-turn output (agent responses + judge reasoning):
    LIVEKIT_EVALS_VERBOSE=1 uv run pytest -s -o log_cli=true tests/test_agent.py

Requires LIVEKIT_API_KEY / LIVEKIT_API_SECRET in the environment if using
LiveKit Inference (as below) — testing does NOT open a LiveKit room, so no
cluster or LiveKit Cloud project connection is needed for these to run.
"""
from unittest.mock import patch

import pytest
from livekit.agents import AgentSession, inference

from agent import KubeVoiceAssistant

# Cheap, fast model for both driving the test session and judging it.
# Swap for whatever you already have inference/API access to.
JUDGE_MODEL = "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_greeting() -> None:
    """The agent should introduce itself as KubeVoice and offer to help."""
    async with (
        inference.LLM(model=JUDGE_MODEL) as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(KubeVoiceAssistant())
        result = await session.run(user_input="Hi, what can you help me with?")

        await result.expect.next_event().is_message(role="assistant").judge(
                llm,
                intent="Responds in a friendly, helpful tone "
                "and offers to assist with the Kubernetes cluster.",
)
        result.expect.no_more_events()


@pytest.mark.asyncio
async def test_calls_cluster_status_tool_when_asked_about_health() -> None:
    """A general health question should trigger the cluster-status tool,
    not just a conversational guess."""
    with patch(
        "agent._sync_get_cluster_status",
        return_value="Cluster is healthy. All 8 pods are running normally.",
    ):
        async with (
            inference.LLM(model=JUDGE_MODEL) as llm,
            AgentSession(llm=llm) as session,
        ):
            await session.start(KubeVoiceAssistant())
            result = await session.run(
                user_input="Are there any problems in the cluster?"
            )

            # Confirms tool USE, not just a plausible-sounding answer —
            # this is the assertion that would catch a prompt regression
            # where the model starts guessing instead of calling the tool.
            result.expect.next_event().is_function_call(name="get_cluster_status")
            result.expect.next_event().is_function_call_output()
            await result.expect.next_event().is_message(role="assistant").judge(
                llm, intent="Reports that the cluster is healthy."
            )
            result.expect.no_more_events()


@pytest.mark.asyncio
async def test_reports_unhealthy_pods_accurately() -> None:
    """Grounding check: the agent's spoken summary must match what the
    (mocked) tool actually returned — no invented pod names, no dropped
    failures, no hallucinated extra problems."""
    fake_status = (
        "Total Pods: 8 (6 healthy, 2 unhealthy):\n"
        "Pod: orders-batch-abc123 | Namespace: orders | Phase: Running | "
        "Reason: CrashLoopBackOff | Restarts: 6\n"
        "Pod: payments-worker-def456 | Namespace: payments | Phase: Pending | "
        "Reason: ImagePullBackOff | Restarts: 0"
    )
    with patch("agent._sync_get_cluster_status", return_value=fake_status):
        async with (
            inference.LLM(model=JUDGE_MODEL) as llm,
            AgentSession(llm=llm) as session,
        ):
            await session.start(KubeVoiceAssistant())
            result = await session.run(user_input="Any problems in the cluster?")

            result.expect.next_event().is_function_call(name="get_cluster_status")
            result.expect.next_event().is_function_call_output()
            await result.expect.next_event().is_message(role="assistant").judge(
                llm,
                intent=(
                    "States that a pod in the orders namespace is crash-looping "
                    "AND that a pod in the payments namespace has an image pull "
                    "problem. Must mention both; must not invent any other "
                    "namespace or failure not present in the tool output."
                ),
            )


@pytest.mark.asyncio
async def test_graceful_degradation_on_cluster_error() -> None:
    """If the Kubernetes API call fails, the agent must say so plainly —
    never surface a raw stack trace or technical exception to the user."""
    with patch(
        "agent._sync_get_cluster_status",
        return_value="Error retrieving cluster status: HTTPSConnectionPool timeout",
    ):
        async with (
            inference.LLM(model=JUDGE_MODEL) as llm,
            AgentSession(llm=llm) as session,
        ):
            await session.start(KubeVoiceAssistant())
            result = await session.run(user_input="Check the cluster for me")

            result.expect.next_event().is_function_call(name="get_cluster_status")
            result.expect.next_event().is_function_call_output()
            await result.expect.next_event().is_message(role="assistant").judge(
                llm,
                intent=(
                    "Communicates that the cluster status couldn't be retrieved due to a "
                    "connectivity or timeout issue. Must NOT read out the raw exception "
                    "string, stack trace, or exception class name verbatim. Brief technical "
                    "suggestions (e.g. checking connectivity, the API server) are acceptable "
                    "since this agent's audience is a support/SRE engineer, not an end user."
                ),
            )


@pytest.mark.asyncio
async def test_does_not_call_tools_for_unrelated_chat() -> None:
    """Misuse/off-topic resistance: casual conversation shouldn't trigger
    a Kubernetes API call."""
    async with (
        inference.LLM(model=JUDGE_MODEL) as llm,
        AgentSession(llm=llm) as session,
    ):
        await session.start(KubeVoiceAssistant())
        result = await session.run(user_input="What's the weather like today?")

        # No function_call event should appear before the assistant message.
        await result.expect.next_event().is_message(role="assistant").judge(
            llm,
            intent="Redirects the conversation toward Kubernetes cluster topics. "
                "Must NOT provide any actual weather information (temperature, "
                "forecast, conditions, etc.) — an explicit refusal is not required, "
                "a topic redirect is sufficient.",
        )