# Platform workstream status

This checkout began empty: it had no Git metadata, upstream Online Boutique revision,
root dependency lock, shared Pydantic contract, cluster enrollment, or live cluster
evidence. The files here establish platform-owned boundaries and a synthetic fixture;
they do not claim that the application has been deployed or tested.

## Current foundation

- `backend/app/platform/` contains platform types, reverse impact traversal, a fixture
  adapter, and a read-only kubectl inventory adapter.
- `fixtures/v1/topology.sample.json` is explicitly synthetic and must not be used to
  approve live experiments.
- `infrastructure/k8s/` only creates the `boutique` namespace. No workload is present
  until the team pins and vendors the upstream application.
- `infrastructure/kind/bosch-ananya.yaml` and `scripts/platform/` provide explicit
  cluster setup, identity enrollment, loopback port-forward, and ownership-bound
  cleanup paths. The cluster setup refuses to create an unrequested cluster.
- `scripts/platform/audit.ps1` requires an explicit context and checks that it is the
  current context before issuing read-only inventory commands.
- `scripts/platform/export-topology.py` exports JSON plus a Mermaid graph from either
  an explicitly labeled fixture or a sandbox identity file. The live adapter verifies
  API endpoint, CA fingerprint, and enrolled namespace UID before reading resources.

The selected upstream release is Google Online Boutique `v0.10.7`, release commit
prefix `5b608cb`. The release is documented by upstream; clone is opt-in via
`scripts/platform/fetch-upstream.ps1 -Fetch` and checks that prefix before continuing.
The machine could not resolve GitHub during this session, so the source has not been
fetched and the exact full SHA is not yet recorded here. The upstream release includes
a mutable `redis:alpine` reference, so the release overlay is not yet a fully immutable
image lock; this needs a digest-pinned override after the runtime can pull and validate
the image.

## Remaining integration work

1. Create/use the team's Git repository and agree on the authoritative v1 Pydantic
   models with the integration owner.
2. Run the opt-in upstream fetch once GitHub is reachable. Record the full resolved
   commit SHA and preserve the upstream Apache-2.0 notices.
3. Build and validate `infrastructure/k8s/overlays/online-boutique`; it applies the
   pinned upstream release manifest into `boutique` and disables the upstream
   load-generator so the team's measured workload can own offered traffic.
4. Enroll the real cluster endpoint, CA fingerprint, and namespace UIDs. The current
   kubectl adapter checks context and namespace UID but does not yet independently
   attest the API endpoint or CA fingerprint; it is read-only and is not suitable for
   mutation authorization.
5. Derive dependency edges from source/config evidence and manually review target
   eligibility. Kubernetes service inventory by itself is not proof of call paths.
6. Implement and verify the kind lifecycle, functional browser/cart/checkout smoke
   path, PostgreSQL lab, Toxiproxy route, and team-reviewed runtime RBAC.

## Audit

On Windows PowerShell, run `scripts/platform/audit.ps1 -Context kind-bosch-ananya` only
after that context exists and is the current context. It does not install dependencies
or create a cluster. Run `scripts/platform/setup-cluster.ps1 -CreateCluster` to create
the named local kind cluster after starting Docker Desktop and installing kind. A
destructive teardown requires the explicit `-Cluster bosch-ananya` argument and
PowerShell confirmation; PVC data requires the separate `-DeletePvcData` switch.
No live audit or cluster operation was run as part of this foundation.

After setup, enroll only the namespaces created by the platform overlay with
`scripts/platform/enroll.ps1 -Context kind-bosch-ananya`. Then export a live, read-only
snapshot with `python scripts/platform/export-topology.py --identity sandbox.identity.json
--context kind-bosch-ananya --namespace boutique`. The identity file is gitignored and
contains cluster identifiers and CA fingerprint, never credentials.
