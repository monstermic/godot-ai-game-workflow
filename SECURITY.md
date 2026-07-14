# Security policy

Report vulnerabilities privately through GitHub’s security-advisory interface. Do not open a public issue containing credentials, exploitable prompt-injection examples tied to a live repository, or unpatched release bypasses.

The workflow treats issue text, prompts, downloaded media, filenames, and adapter output as untrusted data. It never interpolates those values into shell commands. Mutations require `--apply`; secrets are unavailable to PR jobs; release credentials are scoped to protected environments; and third-party Actions are pinned to full commit SHAs.

Supported security fixes target the latest `1.x` release. Compromised release artifacts or checksums invalidate the release and require a new candidate rather than an in-place replacement.
