# unclecode/crawl4ai + security updates, until upstream publishes a newer tag.
#
# Why: 0.9.5 is the latest upstream tag, but Trivy (2026-10-23) counts 42
# fixable CRITICAL in it: stale Debian packages, pillow 12.3 / PyJWT 2.10.1 /
# certifi in site-packages, and lockfiles of an older crawl4ai in the source
# checkout /tmp/project, which nothing reads at runtime.
FROM unclecode/crawl4ai:0.9.5

USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* /tmp/project \
 && pip install --no-cache-dir --upgrade \
      "pillow>=13.0.0" "pyjwt>=2.14.0" "certifi>=2026.10.19" "anyio>=4.14.2" \
      "aiohttp>=3.14.4" "urllib3>=2.8.0" "pypdf>=6.19.0" "nltk>=3.10.3" \
 && python -c "import crawl4ai, PIL, jwt; print('crawl4ai', crawl4ai.__version__, '| pillow', PIL.__version__, '| pyjwt', jwt.__version__)"
USER appuser
