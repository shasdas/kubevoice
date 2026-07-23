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
from metrics import start_metrics_server, TOOL_CALLS, TOOL_LATENCY, K8S_API_ERRORS
import time

load_dotenv(".env.local")

def _instrumented(tool_name):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                TOOL_CALLS.labels(tool_name=tool_name, outcome="success").inc()
                return result
            except Exception:
                TOOL_CALLS.labels(tool_name=tool_name, outcome="error").inc()
                K8S_API_ERRORS.labels(tool_name=tool_name).inc()
                raise
            finally:
                TOOL_LATENCY.labels(tool_name=tool_name).observe(time.monotonic() - start)
        return wrapper
    return decorator

# Initialize Kubernetes Client (Dual-Mode: Local vs In-Cluster)
_k8s_loaded = False

def _ensure_k8s_config():
    global _k8s_loaded
    if _k8s_loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _k8s_loaded = True

def get_core_v1():
    _ensure_k8s_config()
    return client.CoreV1Api()


# --- Synchronous Helper Functions (Run in background thread) ---
@_instrumented("get_cluster_status")
def _sync_get_cluster_status(namespace: str = None) -> str:
    """Synchronous k8s API call & data shaping logic to run off the main event loop."""
    try:
        core_api = get_core_v1()
        if namespace:
            pods = core_api.list_namespaced_pod(namespace=namespace).items
        else:
            pods = core_api.list_pod_for_all_namespaces().items

        unhealthy_pods = []
        total_pods = len(pods)
        healthy_count = 0

        for pod in pods:
            is_ready = True
            status_reason = pod.status.phase or "Unknown"
            restarts = 0

            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    restarts += cs.restart_count
                    if not cs.ready:
                        is_ready = False
                        if cs.state.waiting and cs.state.waiting.reason:
                            status_reason = cs.state.waiting.reason
                        elif cs.state.terminated and cs.state.terminated.reason:
                            status_reason = cs.state.terminated.reason

            if not is_ready or pod.status.phase != "Running":
                unhealthy_pods.append(
                    f"Pod: {pod.metadata.name} | Namespace: {pod.metadata.namespace} | "
                    f"Phase: {pod.status.phase} | Reason: {status_reason} | Restarts: {restarts}"
                )
            else:
                healthy_count += 1

        if not unhealthy_pods:
            return f"Cluster is healthy. All {total_pods} pods are running normally."

        summary = [f"Total Pods: {total_pods} ({healthy_count} healthy, {len(unhealthy_pods)} unhealthy):"]
        summary.extend(unhealthy_pods)
        return "\n".join(summary)

    except Exception as e:
        K8S_API_ERRORS.labels(tool_name="get_cluster_status").inc()
        return f"Error retrieving cluster status: {str(e)}"


@_instrumented("get_namespace_events")
def _sync_get_namespace_events(namespace: str) -> str:
    """Synchronous k8s event fetching & data shaping logic."""
    try:
        core_api = get_core_v1()
        events = core_api.list_namespaced_event(namespace=namespace).items
        if not events:
            return f"No recent events found in namespace '{namespace}'."

        # Sort by timestamp
        events.sort(key=lambda x: x.last_timestamp or x.event_time or "", reverse=True)
        summary = []
        for ev in events[:5]:
            msg = (ev.message or "").split("\n")[0]  # First line of message only
            summary.append(f"[{ev.type}] {ev.reason}: {msg}")
        return "\n".join(summary)

    except Exception as e:
        K8S_API_ERRORS.labels(tool_name="get_cluster_status").inc()
        return f"Error retrieving cluster status: {str(e)}"


# --- LiveKit Voice Agent & Tools ---

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
        # Non-blocking async execution offloaded to thread pool
        return await asyncio.to_thread(_sync_get_cluster_status, namespace)

    @function_tool
    async def get_namespace_events(self, context: RunContext, namespace: str) -> str:
        """Get recent Kubernetes events for a specific namespace to diagnose errors."""
        # Non-blocking async execution offloaded to thread pool
        return await asyncio.to_thread(_sync_get_namespace_events, namespace)


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
    if "download-files" not in sys.argv:
        start_metrics_server()
    
    agents.cli.run_app(server)