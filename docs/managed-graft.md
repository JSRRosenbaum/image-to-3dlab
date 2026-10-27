# Managed Graft integration

AssetFurnace's CLI and studio can be run locally. This fork's GPU-capable model
jobs require a host-local canonical Canopy admission profile and the shared
admission implementation distributed with [Graft](https://github.com/JSRRosenbaum/Graft).
The central Graft repository is private; access requires authorization.

When a managed endpoint is configured, the studio and MCP client connect to that
endpoint. They do not start a local worker if it is unavailable. Before accepting a
mutation, Graft checks the requested operation against the worker's advertised
capabilities; installed engines alone do not grant permission to run an operation.
Unsupported operations fail explicitly rather than being redirected to a different
engine or replayed through another workflow.

Before model work starts, the application wraps each GPU-capable native job with
Canopy's canonical admission mechanism. It retains the lease while the job and
its owned worker processes are cleaned up. This protects the wrapped application
paths; it does not claim to prevent an unrelated program from invoking an engine
outside this application.

Mutations use durable request identities. If a connection drops after submission, the
client reconciles the original request and does not submit it again. Accepted work remains
with its assigned worker. Completed bundles are published with their owner and content
identity, so clients can discover finished outputs without treating partial files as
results.

Local non-model CLI operations, such as `python pipeline.py --help`, do not require an
admission profile. To run GPU-capable jobs locally, set
`ASSETFURNACE_ADMISSION_PROFILE` to the local profile file. Put the Graft checkout
on `PYTHONPATH` and set `ASSETFURNACE_SYNC_LIB` to its `sync/` directory.
Local admission does not itself require a managed HTTP endpoint. Managed
Studio/MCP clients additionally connect to their configured endpoint and do not
start a local worker when it is unavailable. No endpoint or credentials are
embedded in this repository.
