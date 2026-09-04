# lightrag-builder

This repository owns the LightRAG Controller source, tests and image build,
plus Docling/LightRAG deployment configuration and lifecycle smokes.
Backend owns business records, authorization and indexing jobs and calls Controller
over authenticated HTTP. Do not import, copy or build from Backend source.

Keep Controller small and keep deployment scripts standard-library-only.
Prefer direct changes over extra layers or compatibility scaffolding.
Preserve service authentication, namespace/resource ownership and idempotent
instance creation and deletion.

PostgreSQL and the shared Docker network belong to the environment. Keep only
connection settings; do not manage their lifecycle or data.
Never commit credentials, documents, databases, caches or image archives.

For Controller changes, run its tests and Ruff, then build its image.
For deployment changes, validate Compose with config --quiet and run the basic
smoke (health, DOCX conversion, disposable instance creation and cleanup).
Use --index when model indexing/retrieval validation is in scope.
Preserve existing namespaces and volumes. Do not push or deploy shared
environments without authorization. Use English for code and commit messages.
