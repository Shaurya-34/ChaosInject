# Local prevention demo — under three minutes

This recording is explicitly a LOCAL DEMO. It shows a real separate HTTP target and prevention of an unsafe injection attempt. It is not a replay or an Online Boutique chaos demonstration.

1. 0–25s: show LOCAL DEMO banner and running target. Explain request validation happens before execution.
2. 25–50s: show the 90-second pause request and fixed maximum of 60 seconds.
3. 50–85s: click Inject test error. Show popup: Error has occurred / Injection blocked before execution. Explain the error is in the rejected request; the service did not fail.
4. 85–120s: acknowledge popup; show BLOCK, zero dispatched mutations and healthy target. STOP remains latched.
5. 120–150s: show the Findings view with the rejection rule/evidence. Explain that Tooshita owns the later live executor and Navya independently verifies impact and recovery.
6. 150–170s: state limits: no cluster injected, no SQL executed, no learned model or resilience score claimed. Stop recording.

For the event's final video, replace/extend this with authentic approved Kubernetes fault, measured impact, verified cleanup/recovery and findings. Never label fixture numbers live.
