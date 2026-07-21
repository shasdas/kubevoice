import sys
import asyncio

# Fix Windows asyncio IPC Proactor timeout issue when testing on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, RunContext, function_tool
from livekit.agents.inference import TurnDetector
from livekit.plugins import silero
from kubernetes import client, config

load_dotenv(".env")

# Initialize Kubernetes Client (Dual-Mode: Local vs In-Cluster)
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_api = client.CoreV1Api()
apps_api = client.AppsV1Api()


class KubeVoiceAssistant(Agent):
    def __init__(self):
        super().__init__(
            instructions=(
                "You are KubeVoice, an expert Kubernetes Site Reliability Engineer and support assistant. "
                "Your role is to help support engineers diagnose and troubleshoot cluster issues. "
                "When asked about cluster status or health, run your tools to inspect pods and events. "
                "Keep responses concise, human-friendly, and actionable (2 to 3 sentences per response)."
            )
        )

    @function_tool
    async def get_cluster_status(self, context: RunContext, namespace: str = None) -> str:
        """Get cluster health summary across all namespaces or a specific namespace."""
        try:
            if namespace:
                pods = core_api.list_namespaced_pod(namespace=namespace).items
            else:
                pods = core_api.list_pod_for_all_namespaces().items

            unhealthy_pods = []
            for pod in pods:
                is_ready = True
                status_reason = pod.status.phase

                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if not cs.ready:
                            is_ready = False
                            if cs.state.waiting:
                                status_reason = cs.state.waiting.reason
                            elif cs.state.terminated:
                                status_reason = cs.state.terminated.reason

                if not is_ready or pod.status.phase != "Running":
                    unhealthy_pods.append(
                        f"Pod '{pod.metadata.name}' in namespace '{pod.metadata.namespace}' status: {status_reason}"
                    )

            if not unhealthy_pods:
                return "All pods in the cluster appear to be healthy and running."

            return "Found the following unhealthy pods:\n" + "\n".join(unhealthy_pods)
        except Exception as e:
            return f"Error retrieving pod status: {str(e)}"

    @function_tool
    async def get_namespace_events(self, context: RunContext, namespace: str) -> str:
        """Get recent Kubernetes events for a specific namespace to diagnose errors."""
        try:
            events = core_api.list_namespaced_event(namespace=namespace).items
            if not events:
                return f"No recent events found in namespace '{namespace}'."

            # Sort by timestamp
            events.sort(key=lambda x: x.last_timestamp or x.event_time, reverse=True)
            summary = []
            for ev in events[:5]:
                summary.append(f"[{ev.type}] {ev.reason}: {ev.message}")
            return "\n".join(summary)
        except Exception as e:
            return f"Error fetching events for namespace '{namespace}': {str(e)}"


server = AgentServer()

@server.rtc_session(agent_name="kubevoice")
async def my_agent(ctx: agents.JobContext):
    session = AgentSession(
        stt="deepgram/nova-3:multi",
        llm="openai/gpt-4.1-mini",
        tts="cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        vad=silero.VAD.load(),
        turn_detection=TurnDetector(),
    )

    await session.start(room=ctx.room, agent=KubeVoiceAssistant())
    await session.generate_reply(
        instructions="Greet the user warmly as KubeVoice, your Kubernetes SRE assistant, and ask how you can help with the cluster today."
    )

if __name__ == "__main__":
    agents.cli.run_app(server)