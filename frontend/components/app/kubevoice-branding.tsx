// components/kubevoice-branding.tsx
// A self-contained branding block for the KubeVoice welcome screen:
// "Powered by" logos row + a visual of the voice pipeline.
// No external image assets required — uses text badges so it drops in cleanly.
// Restyle freely with Tailwind to match your theme.

export function KubeVoiceBranding() {
  const pipeline = [
    { label: 'STT', detail: 'Deepgram nova-3' },
    { label: 'LLM', detail: 'gpt-4.1-mini' },
    { label: 'TTS', detail: 'Deepgram aura-2' },
    { label: 'VAD', detail: 'Silero' },
  ];

  return (
    <div className="flex flex-col items-center gap-6 mt-8">
      {/* Powered-by logos row, centered */}
      <div className="flex items-center justify-center gap-3 text-sm text-muted-foreground">
        <span>Powered by</span>
        <span className="font-semibold text-foreground">LiveKit</span>
        <span className="opacity-40">+</span>
        <span className="font-semibold text-foreground">Deepgram</span>
      </div>

      {/* Voice pipeline depiction */}
      <div className="flex items-center justify-center gap-2 flex-wrap max-w-lg">
        {pipeline.map((stage, i) => (
          <div key={stage.label} className="flex items-center gap-2">
            <div className="flex flex-col items-center rounded-lg border border-border bg-card px-3 py-2 min-w-[92px]">
              <span className="text-xs font-bold tracking-wide text-primary">
                {stage.label}
              </span>
              <span className="text-[10px] text-muted-foreground mt-0.5">
                {stage.detail}
              </span>
            </div>
            {i < pipeline.length - 1 && (
              <span className="text-muted-foreground text-lg" aria-hidden>
                &rarr;
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
