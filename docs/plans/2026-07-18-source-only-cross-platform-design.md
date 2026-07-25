# Source-Only Cross-Platform Validation Design

## Scope

Keep Orpheus as a source repository that a contributor can clone and validate on macOS or Windows. Add Windows PowerShell setup instructions, test-only GitHub Actions coverage for both operating systems, and common local-output ignore rules.

## Boundaries

- CI installs committed dependency manifests and runs source checks only.
- CI does not sign, package, publish, upload artifacts, build installers, or create releases.
- Base-profile validation does not download optional model weights.
- macOS remains the primary MLX runtime; Windows source validation covers the portable base profile. CUDA remains an explicit hardware-dependent profile.
- Setup guidance uses the existing Python, npm, and Cargo manifests rather than adding bootstrap scripts or another dependency layer.

## Verification

A repository contract test will assert the documented PowerShell path, the macOS/Windows CI matrix and its prohibited release operations, and representative `.gitignore` behavior. Each contract is introduced red, followed by the smallest documentation or configuration change that makes it green.
