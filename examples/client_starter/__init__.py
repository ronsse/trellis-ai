"""Starter scaffold for integrating Trellis into a client repository.

Copy this directory into your own repo as a starting point. Every file
calls out the "replace-me" hotspots inline.

Layout mirrors what we recommend for a production client integration:

- ``types``     — namespaced entity/edge-type string constants + typed
                  property shapes (validated client-side).
- ``client``    — thin wrapper around :class:`trellis_sdk.TrellisClient`
                  that centralizes org defaults (base URL, timeouts,
                  future auth headers).
- ``extractor`` — pure-function :class:`trellis_sdk.extract.DraftExtractor`
                  that maps your domain data to entity/edge drafts.
- ``retrieve``  — thin helper that turns an agent intent into a context
                  pack, formatted for downstream consumption.
- ``run_demo``  — end-to-end script: ingest sample data, then retrieve.
                  Runs in-memory by default; flip ``--server`` to point
                  at a real Trellis deployment.
"""
