# pgvector/pgvector:pg18 + current Debian security updates.
#
# Why: the upstream tag is rebuilt only on pgvector releases; between them
# its Debian packages go stale (Trivy 2026-10-23: 13 fixable CRITICAL, perl et
# al.). This layer only runs apt-get upgrade on top of it.
#
# Stays on bookworm on purpose: the cluster's collation version is glibc 2.36
# (bookworm). trixie ships a newer glibc, which changes text sort order and
# would need a REINDEX of every text index. Move to trixie only together with
# a planned REINDEX.
FROM pgvector/pgvector:0.8.7-pg18-bookworm

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && ldd --version | head -1 | grep -q ' 2\.36$'
