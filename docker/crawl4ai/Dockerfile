# unclecode/crawl4ai + security updates, until upstream publishes a newer tag.
#
# Why: 0.9.5 is the latest upstream tag, but Trivy (2026-10-23) counts 42
# fixable CRITICAL in it: stale Debian packages, pillow 12.3 / PyJWT 2.10.1 /
# certifi in site-packages, and lockfiles of an older crawl4ai in the source
# checkout /tmp/project, which nothing reads at runtime.
#
# 2026-11-15: the patched image still showed 2 CRITICAL / 8 HIGH (running
# upstream image: 27 / 224). Four were packages not yet in the list (brotli,
# dnspython, filelock, soupsieve); the rest (certifi, msgpack, urllib3,
# setuptools) are the copies pip and setuptools carry inside themselves, so
# pip and setuptools are upgraded first. The smoke test selects with bs4,
# because soupsieve moves a major version.
FROM unclecode/crawl4ai:0.9.5

USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* /tmp/project \
 && pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" \
 && pip install --no-cache-dir --upgrade \
      "pillow>=13.0.0" "pyjwt>=2.14.0" "certifi>=2026.10.19" "anyio>=4.14.2" \
      "aiohttp>=3.14.4" "urllib3>=2.8.0" "pypdf>=6.19.0" "nltk>=3.10.3" \
      "brotli>=1.2.1" "dnspython>=2.9.2" "filelock>=4.2.1" "soupsieve>=4.0.0" \
      "msgpack>=1.2.3" \
 && python -c "import crawl4ai, PIL, jwt, brotli, bs4, soupsieve; print('crawl4ai', crawl4ai.__version__, '| pillow', PIL.__version__, '| pyjwt', jwt.__version__, '| soupsieve', soupsieve.__version__); print(bs4.BeautifulSoup('<p class=a>x</p>', 'html.parser').select_one('p.a').text)"
USER appuser
