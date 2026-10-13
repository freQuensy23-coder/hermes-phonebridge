# Automatic security evidence policy

All executable audits and tests run only in GitHub CI. The dependency audit resolves the declared, pinned server and CLI runtime requirements and fails on known vulnerabilities or unresolved packages. Project source distributions are not queried as if they were published PyPI dependencies.

The workflow audit runs pinned zizmor with ignores disabled, auditor persona, strict collection and JSON-v1 output. Its complete findings are retained as `workflow-audit.json`; classification is retained separately as `workflow-audit-classified.json`. A successful policy gate does not mean zero scanner findings.

Unknown medium/high findings block delivery. Informational/low findings remain visible. The private GitHub plan cannot provide the assumed environment isolation, so narrowly named fresh signing/publishing and owner-gated deployment jobs have explicit, visible `secrets-outside-env` limitations. This is not protection against an authorized repository owner rewriting automation.

The generic high-severity `workflow_run` trigger warning is separately reviewed for one exact deployment workflow digest. That runner never checks out or executes upstream code or artifacts. Before any SSH-key use it verifies the repository, workflow path, successful push/main CI producer, owner identities, source SHA and current main. The host independently requires an owner-merged PR, a coherent completed CI attempt and its bound artifact. Any deployment workflow byte change invalidates this recorded trigger review; automatic source-bound negative tests exercise its exact inline guard. This finding remains in the report with its high severity and rationale, rather than being suppressed by zizmor configuration.

These authored guards and tests are not runtime proof. The current-head automatic result and real deployment remain required delivery evidence.

## Physical OEM gate

Only an exact owner main-push job receives GitHub OIDC permissions. It uses pinned Google auth/SDK actions and SDK 589.0.0, the isolated no-billing `hermes-phonebridge-ci-6147c4` project and its approved service-managed Test Lab bucket. A fresh authenticated catalog supplies runnable Galaxy S25 Ultra and Pixel 9 IDs and their latest stable supported OS; streaming-only catalog aliases without supported test versions are excluded. One matrix has two physical devices, zero flaky retries and a checked five-test daily Spark cap. Quota or permission errors remain failures; no billed fallback is permitted.

The physical job consumes this run's debug app and instrumentation APKs, uses a temporary synthetic HTTPS/WSS server, preserves the stock Clock APKs and saves actual native package/signature/UI/readback evidence. Unsupported stock profiles fail the positive test. Emulator jobs route every nonhardware instrumentation class separately; they never run a Samsung-only positive fixture as emulator proof. Release and deployment require an explicit successful `android-physical` job, including when other CI jobs are green. The first physical execution still requires reviewed default-branch activation and successful prerequisite CI; this wiring alone is not a hardware result.
