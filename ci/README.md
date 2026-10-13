# Automatic security evidence policy

All executable audits and tests run only in GitHub CI. The dependency audit resolves the declared, pinned server and CLI runtime requirements and fails on known vulnerabilities or unresolved packages. Project source distributions are not queried as if they were published PyPI dependencies.

The workflow audit runs pinned zizmor with ignores disabled, auditor persona, strict collection and JSON-v1 output. Its complete findings are retained as `workflow-audit.json`; classification is retained separately as `workflow-audit-classified.json`. A successful policy gate does not mean zero scanner findings.

Unknown medium/high findings block delivery. Informational/low findings remain visible. The private GitHub plan cannot provide the assumed environment isolation, so narrowly named fresh signing/publishing and owner-gated deployment jobs have explicit, visible `secrets-outside-env` limitations. This is not protection against an authorized repository owner rewriting automation.

The generic high-severity `workflow_run` trigger warning is separately reviewed for one exact deployment workflow digest. That runner never checks out or executes upstream code or artifacts. Before any SSH-key use it verifies the repository, workflow path, successful push/main CI producer, owner identities, source SHA and current main. The host independently requires an owner-merged PR, a coherent completed CI attempt and its bound artifact. Any deployment workflow byte change invalidates this recorded trigger review; automatic source-bound negative tests exercise its exact inline guard. This finding remains in the report with its high severity and rationale, rather than being suppressed by zizmor configuration.

These authored guards and tests are not runtime proof. The current-head automatic result and real deployment remain required delivery evidence.
