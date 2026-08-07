#!/usr/bin/env bash
# Smoke-test a RUNNING rya server's console. Usage: scripts/smoke_console.sh [BASE_URL]
#
# The console's unit suite is good at components and blind to serving. The two ways
# this console breaks in production are both invisible to vitest, because neither
# involves React at all:
#
#   1. an asset-hash mismatch — `index.html` from one build, `/assets/*` from another
#      (a stale layer, a partial copy, a cached index). The page loads, the bundle
#      404s, and the operator gets a blank screen with no error anywhere;
#   2. a CSP that blocks the bundle it is meant to protect. `_CONSOLE_CSP` is asserted
#      in the Python tests by string-matching the header, which cannot notice that the
#      policy forbids something the page needs.
#
# So this fetches the index the server actually serves, follows every asset it
# actually references, and checks the headers that actually came back. No browser and
# no Node — curl against a real process, which is what CI can afford to run on every
# push and what `docker compose up` deserves before anyone trusts it.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8787}"
BASE="${BASE%/}"

pass=0
fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail + 1)); }
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$2', got '$1')"; fi; }

echo "smoke: $BASE"

# --- the API is up at all ------------------------------------------------------
code=$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE/healthz" 2>/dev/null || echo 000)
if [ "$code" != "200" ]; then
  echo "  server is not answering /healthz (got $code) — nothing else can be checked" >&2
  exit 1
fi
ok "/healthz 200"

# --- the console page ----------------------------------------------------------
hdr=$(mktemp) && body=$(mktemp)
trap 'rm -f "$hdr" "$body"' EXIT
code=$(curl -sS -D "$hdr" -o "$body" -w '%{http_code}' "$BASE/")
check "$code" 200 "GET / is 200"

# A 503 here is the designed "bundle not built" explainer. It is a legal state for a
# source checkout and a RELEASE BUG for an image or a wheel, which is the whole reason
# this script exists — so name it specifically rather than letting it fail as "no root".
if grep -qi 'Console bundle not built' "$body"; then
  bad "the server is serving the unbuilt-bundle explainer — the console was never compiled into this deployment"
fi

grep -q '<div id="root"></div>' "$body" \
  && ok "/ serves the React shell" \
  || bad "/ has no <div id=\"root\"> — this is not the console index"

# --- security headers ----------------------------------------------------------
# `_CONSOLE_HEADERS` in api/app.py. Checked on the wire because a mount bypasses
# route-level headers, so "it is in the dict" and "it reached the client" are two
# different claims.
csp=$(grep -i '^content-security-policy:' "$hdr" | tr -d '\r' | cut -d' ' -f2- || true)
if [ -z "$csp" ]; then
  bad "no Content-Security-Policy on /"
else
  ok "/ carries a CSP"
  case "$csp" in
    *"script-src 'self';"*) ok "script-src is 'self' with nothing else" ;;
    *) bad "script-src is not exactly 'self' — got: $csp" ;;
  esac
fi
grep -qi '^x-frame-options: *DENY' "$hdr" && ok "X-Frame-Options: DENY" || bad "no X-Frame-Options: DENY"
grep -qi '^x-content-type-options: *nosniff' "$hdr" && ok "X-Content-Type-Options: nosniff" || bad "no nosniff"

# `connect-src` must be exactly 'self'. Bare `ws:`/`wss:` are SCHEME sources — a
# socket to any host on the internet — sitting in the one directive that decides who
# the console may talk to. Matched with its delimiter so `'self'` is distinguishable
# from `'self' ws: wss:`; a plain substring test for "'self'" passes either way.
case "$csp" in
  *"connect-src 'self';"*|*"connect-src 'self'") ok "connect-src is exactly 'self'" ;;
  *) bad "connect-src is not exactly 'self' — got: $csp" ;;
esac

# --- cache policy on the index -------------------------------------------------
# The index NAMES the current asset hashes, so an intermediary allowed to hold it
# will, after a deploy, serve an index pointing at hashes that no longer exist: 200
# for the page, 404 for the bundle, blank screen, nothing in any log. It shipped with
# no Cache-Control and no validator at all, which is an open invitation to a CDN's
# heuristic TTL — so "absent" is reported as its own failure, not lumped in with wrong.
cc=$(grep -i '^cache-control:' "$hdr" | tr -d '\r' | cut -d' ' -f2- || true)
case "$cc" in
  "") bad "/ has no Cache-Control — a CDN may pin the index and 404 the bundle after the next deploy" ;;
  *no-cache*|*no-store*) ok "/ must be revalidated before reuse ($cc)" ;;
  *) bad "/ may be reused without revalidation (cache-control: $cc)" ;;
esac

# --- every asset the index references ------------------------------------------
# The regression this catches: Vite content-hashes its filenames, so an index served
# from a different build than the /assets mount references files that do not exist.
# The page still returns 200 and still renders nothing.
# `|| true`: no matches is a grep exit of 1, which `set -e` would turn into a silent
# early exit — and "no assets referenced" is precisely one of the failures being
# tested for, so it has to be reportable rather than fatal.
assets=$(grep -oE '(src|href)="/assets/[^"]+"' "$body" | sed -E 's/.*"(\/assets\/[^"]+)"/\1/' | sort -u || true)
if [ -z "$assets" ]; then
  bad "the index references no /assets/* at all — expected a hashed JS and CSS pair"
else
  n=0
  while IFS= read -r a; do
    [ -n "$a" ] || continue
    ah=$(mktemp)
    ac=$(curl -sS -D "$ah" -o /dev/null -w '%{http_code}' "$BASE$a")
    ct=$(grep -i '^content-type:' "$ah" | tr -d '\r' | cut -d' ' -f2- || true)
    acsp=$(grep -ci '^content-security-policy:' "$ah" || true)
    acc=$(grep -i '^cache-control:' "$ah" | tr -d '\r' | cut -d' ' -f2- || true)
    rm -f "$ah"
    if [ "$ac" != "200" ]; then
      bad "$a is $ac — the index and the /assets mount are from different builds"
      continue
    fi
    case "$a:$ct" in
      *.js:*javascript*|*.css:text/css*) ok "$a 200 ($ct)" ;;
      # A wrong content-type is not cosmetic: `nosniff` is set, so the browser
      # REFUSES to execute a module served as text/plain.
      *) bad "$a 200 but content-type is '$ct'" ;;
    esac
    [ "$acsp" -ge 1 ] || bad "$a is served with no CSP (the _ConsoleStatic subclass is not stamping headers)"
    # These names are content-hashed, so their bytes can never change under a given
    # URL — anything short of a year of `immutable` means every page load spends a
    # round trip revalidating a file that is incapable of being stale. StaticFiles
    # ships an ETag and Last-Modified and no Cache-Control, so that round trip was
    # real: it returned 304, over and over, forever.
    case "$acc" in
      *immutable*) ok "$a is cached immutably ($acc)" ;;
      "") bad "$a has no Cache-Control — a content-hashed asset revalidated on every page load" ;;
      *) bad "$a is not immutable (cache-control: $acc)" ;;
    esac
    n=$((n + 1))
  done <<EOF
$assets
EOF
  ok "followed $n asset(s) from the index"
fi

# --- the bookmark redirect -----------------------------------------------------
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/v2")
check "$code" 308 "/v2 permanently redirects to the console"

echo
if [ "$fail" -gt 0 ]; then
  printf '\033[31m%d failed\033[0m, %d passed\n' "$fail" "$pass"
  exit 1
fi
printf '\033[32mall %d checks passed\033[0m\n' "$pass"
