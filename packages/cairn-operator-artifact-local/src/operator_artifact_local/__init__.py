"""obs-operator-artifact-local — local filesystem artifact store.

Reference operator pack #3 of the Phase-2 set. Provides ``storage.put``
and ``storage.get`` capabilities backed by the local filesystem. No
external service, no external dependencies — the FS impl lives in
:mod:`operator_artifact_local.operators.put` and ``.get`` directly per
the locked architecture (``architecture_obs_agent_locked.md`` §2: "a
tiny local filesystem artifact store can be built-in for dev").

Storage root: ``CAIRN_ARTIFACTS_DIR`` env var, defaults to
``~/.cairn/artifacts``. Per-artifact-name subdirs scope each
``[requires.artifacts.<name>]`` decl so different artifact kinds (RCAs,
post-mortems, datasets) don't collide in the same root.

Production deployments swap this out for cairn-operator-s3 / cairn-operator-gcs /
cairn-operator-minio; manifest stays the same.
"""
