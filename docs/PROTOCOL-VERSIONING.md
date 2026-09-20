# Protocol versioning policy

Status: current, effective 2026-09-21 (router.jobs/v2). Owner: this document.

Until this round the repository had no stated versioning policy: `router.jobs/v1` existed in code,
but nothing said what a version meant, what counted as a breaking change, how long an old version
would be kept, or how a caller was supposed to find out what a peer supports. This page states the
policy, and `model_router/job_contract.py` is where it is implemented.

## 1. Two independent versions, never one

| Axis | Meaning | Declared where |
| --- | --- | --- |
| **Wire protocol** `router.jobs/vN` | The *shape* of the contract: method set, closed payload key sets, reply keys, error literals, stop semantics | `job_contract.PROTOCOL` (v1), `job_contract.PROTOCOL_V2`, and the supported set `PROTOCOLS` |
| **Implementation release** `0.x.y` | Which build of this service is running | `pyproject.toml`, `model_router.__version__`, and `describe()["server_version"]` (which reads `__version__`, never a literal) |

They move independently. `server_version` is **not** a capability signal: a caller must decide what
it may do from the negotiated protocol and the declared features, never from the release string.

## 2. What counts as a breaking change

Breaking — a new protocol version is required:

* adding, removing or renaming a **required** payload key of an existing method;
* narrowing or widening a **closed** key set (including "accepting an extra optional key");
* changing the meaning, type or units of a declared fact;
* changing an error literal a caller matches on, or the rejection code;
* changing what a state *means* (for example claiming a remote stop this service cannot observe);
* changing the digest that decides idempotency for an existing version.

Not breaking — no new version, but the declaration must be updated:

* adding a **new** method or a **new** key to a reply, provided no caller can depend on its absence;
* adding a **new** entry to `features`, `budgets`, `scope` or `limits` in `describe()`;
* adding optional fields to a *diagnostic* payload that is not part of a closed set.

## 3. How a version is added

1. Register the new id in `job_contract` (`PROTOCOL_V2`, `PROTOCOLS`, and the schema table
   `SCHEMAS`). `PROTOCOLS` is ordered newest first and is what `describe()["protocol_versions"]`
   reports.
2. Keep the **previous version's behaviour byte-identical** in the same commit: same closed key
   set, same error literals, same digest algorithm. A caller must be able to keep using v1 without
   changing anything, including retrying a job it submitted before the upgrade.
3. Accept **every** version in `PROTOCOLS` at `initialize`. A second `initialize` is the documented
   upgrade step for a connection, and it is not an error.
4. Add the new version's tests beside the existing ones; never edit an existing assertion to make a
   new version pass. The v1 suite passing unchanged is the legacy-safety evidence.
5. Record the version, the implementation release and the commit hash in the round's evidence
   bundle, so a later reader can tell which bytes were verified.

## 4. How long an old version is kept

At least one full round after its successor ships, and until every caller in the compatibility
matrix has been verified against the successor on real processes. Removal requires a human
decision; it is never implied by "the new version exists". v1 stays until such a decision is
recorded, and this page must be updated in the same round that removes it.

## 5. What a caller may conclude

* `protocol_versions` — the versions this peer serves; absent means the peer predates the field, so
  the callers must assume v1 only.
* `protocol` in the `initialize` reply — the version **in force for that connection**. Every method
  on the connection is validated against that schema, and nothing else.
* A version that is absent, malformed or unknown to the caller is a stop condition: a caller must
  classify it explicitly and refuse rather than guess. Silently downgrading is not permitted; if a
  caller chooses a lower version, it must be able to say so as a recorded fact.
* `features.stop_acknowledgement` — the peer reports `stop_acknowledgement` on cancel replies and in
  settled results.
* `cancellation.remote_provider` — `not_observable_in_v2` means exactly that: the peer has **no**
  evidence about the remote provider. `remote_stopped` stays `null`, and no caller may read a
  cancelled state as proof that the remote side stopped.

## 6. Evidence rule

Every protocol round records: the protocol version, the implementation release, the commit hash, the
command lines, the exit codes and the raw output — including the mixed-version combination it was
verified in (caller version × peer version). A claim about compatibility without a named
combination is not evidence.
