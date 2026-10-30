#!/usr/bin/env bash
# ChangeWatch installer: one command from an empty Ubuntu server (or your own machine) to a
# running ChangeWatch with HTTPS, a first project and its keys.
#
#   curl -fsSL https://raw.githubusercontent.com/aarizbobbydeol/changewatch/main/scripts/install.sh \
#     | sudo bash -s -- --domain changewatch.example.com
#
# or from a checkout: ./scripts/install.sh   (it asks what it needs)
#
# Safe to run again: it keeps your secrets, settings and data, and only creates what is missing.
set -euo pipefail

VERSION="1.0.1"   # the release whose published images it runs
REPO_URL="https://github.com/aarizbobbydeol/changewatch.git"

DIR="" DOMAIN="" PROJECT="" SLACK="" ANTHROPIC="" DASHBOARD="" DEMO=0 BUILD=0 YES=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --domain NAME        Your domain for HTTPS (its DNS must point at this server).
                       Without it, ChangeWatch runs on this machine only (http://localhost).
  --dashboard-url URL  Where your dashboard lives when it is not on this server, e.g. on
                       Vercel (https://your-project.vercel.app). Alert links open it.
  --project NAME       Create a project with this name (first install: my-app).
  --slack-webhook URL  Slack incoming webhook for that project's alerts.
  --anthropic-key KEY  Anthropic API key for AI explanations (without it: rules-based ones).
  --demo               Also run the demo shop, which deploys and breaks itself for people to watch.
  --dir PATH           Where ChangeWatch lives (default: this checkout, else /opt/changewatch).
  --build              Build the images here instead of using the published ones.
  --yes                Do not ask anything; use the options and defaults.
  -h, --help           This help.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=${2:?--domain needs a value}; shift 2 ;;
    --dashboard-url) DASHBOARD=${2:?--dashboard-url needs a value}; shift 2 ;;
    --project) PROJECT=${2:?--project needs a value}; shift 2 ;;
    --slack-webhook) SLACK=${2:?--slack-webhook needs a value}; shift 2 ;;
    --anthropic-key) ANTHROPIC=${2:?--anthropic-key needs a value}; shift 2 ;;
    --demo) DEMO=1; shift ;;
    --dir) DIR=${2:?--dir needs a value}; shift 2 ;;
    --build) BUILD=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; printf '\nUnknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }
apt_get() {  # a new server updates itself in its first minutes: wait for that instead of failing
  local try
  for try in 1 2 3; do
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -y -qq "$@" >/dev/null && return 0
    [ "$try" = 3 ] || sleep 20
  done
  return 1
}
interactive() { [ "$YES" = 0 ] && { true </dev/tty; } 2>/dev/null; }
# ask VAR "question" [default] [secret]: reads the terminal even when this script arrives on stdin
ask() {
  local reply=""
  if interactive; then
    if [ "${4:-}" = secret ]; then
      read -r -s -p "$2: " reply </dev/tty || true; printf '\n'
    else
      read -r -p "$2${3:+ [$3]}: " reply </dev/tty || true
    fi
  fi
  printf -v "$1" '%s' "${reply:-${3:-}}"
}

# ---------------------------------------------------------------------------------------------
# 1. The ChangeWatch files: this checkout, or a fresh clone of the release
script_dir=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
if [ -z "$DIR" ] && [ -n "$script_dir" ] && [ -f "$script_dir/../docker-compose.yml" ]; then
  DIR=$(cd "$script_dir/.." && pwd)
fi
DIR=${DIR:-/opt/changewatch}

if [ ! -f "$DIR/docker-compose.yml" ]; then
  if ! command -v git >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1 || [ "$(id -u)" != 0 ]; then
      fail "git is needed. Install it, or run again with sudo."
    fi
    apt_get update
    apt_get install git
  fi
  say "Downloading ChangeWatch $VERSION to $DIR"
  git clone --quiet --depth 1 --branch "v$VERSION" "$REPO_URL" "$DIR"
fi
cd "$DIR"

# ---------------------------------------------------------------------------------------------
# 2. Docker with Compose v2
# Docker's own packages, the way docs.docker.com/engine/install does it: one method for Ubuntu,
# Debian and systems built on them, on Intel/AMD and ARM. Elsewhere, install Docker first.
install_docker() {
  local os family="" codename
  # Read in subshells: /etc/os-release also sets VERSION, which would replace ours
  # shellcheck disable=SC1091
  os=$(. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}")
  case " $os " in
    *" ubuntu "*) family=ubuntu ;;
    *" debian "*) family=debian ;;
  esac
  # shellcheck disable=SC1091
  codename=$(. /etc/os-release 2>/dev/null
    if [ "$family" = ubuntu ]; then echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"; else echo "${VERSION_CODENAME:-}"; fi)
  if [ -z "$family" ] || [ -z "$codename" ]; then
    fail "The installer adds Docker on Ubuntu and Debian. Here, install Docker with Compose v2 first (https://docs.docker.com/engine/install/), then run this again."
  fi
  say "Installing Docker (from docker.com, for $family $codename)"
  apt_get update
  apt_get install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL --retry 3 "https://download.docker.com/linux/$family/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  if ! grep -rqs download.docker.com /etc/apt/sources.list /etc/apt/sources.list.d; then
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
      "$(dpkg --print-architecture)" "$family" "$codename" >/etc/apt/sources.list.d/docker.list
  fi
  apt_get update
  apt_get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker >/dev/null 2>&1 || true
}

if ! docker compose version >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    fail "Docker is here but without Compose v2. Add the Compose plugin (https://docs.docker.com/compose/install/linux/), then run this again."
  fi
  command -v apt-get >/dev/null 2>&1 \
    || fail "Install Docker first (on a Mac or Windows: Docker Desktop, https://docs.docker.com/get-docker/), then run this again."
  [ "$(id -u)" = 0 ] || fail "Docker is not installed. Run again with sudo and the installer adds it."
  install_docker
fi
if ! docker info >/dev/null 2>&1; then
  [ "$(id -u)" = 0 ] || fail "Docker is not running, or this user cannot use it. Start Docker (Docker Desktop on a Mac or Windows), or run again with sudo."
  fail "Docker is installed but not running. Start it (systemctl start docker), then run this again."
fi

# ---------------------------------------------------------------------------------------------
# 3. Settings (.env): created once with fresh secrets; later runs keep what is there
fresh=0
if [ ! -f .env ]; then
  cp .env.example .env
  fresh=1
fi
chmod 600 .env

get_env() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true; }
set_env() {  # KEY VALUE: replace the line or add it (no sed, so no escaping problems)
  local tmp
  tmp=$(mktemp)
  awk -v k="$1" -v v="$2" 'index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next } { print }
    END { if (!done) print k "=" v }' .env >"$tmp"
  cat "$tmp" >.env
  rm -f "$tmp"
}
secret() { openssl rand -hex 32 2>/dev/null || od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }
add_profile() {  # keep COMPOSE_PROFILES a comma list without duplicates
  local current
  current=$(get_env COMPOSE_PROFILES)
  case ",$current," in *",$1,"*) ;; *) set_env COMPOSE_PROFILES "${current:+$current,}$1" ;; esac
}

if [ "$fresh" = 1 ]; then
  [ -n "$DOMAIN" ] || ask DOMAIN "Your domain for HTTPS, e.g. changewatch.example.com (empty: this machine only)"
  [ -n "$PROJECT" ] || ask PROJECT "Name of your first project" "my-app"
  [ -n "$SLACK" ] || ask SLACK "Slack incoming webhook for its alerts (optional)"
  [ -n "$ANTHROPIC" ] || ask ANTHROPIC "Anthropic API key for AI explanations (optional)" "" secret
  PROJECT=${PROJECT:-my-app}
fi

if [ -n "$DOMAIN" ]; then
  [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || fail "That does not look like a domain: $DOMAIN"
  if ! getent hosts "$DOMAIN" >/dev/null 2>&1; then
    printf '\nNote: %s does not resolve yet. HTTPS starts working once its DNS points at this server.\n' "$DOMAIN"
  fi
fi
DASHBOARD=${DASHBOARD%/}
if [ -n "$DASHBOARD" ] && ! [[ "$DASHBOARD" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
  fail "That does not look like a web address: $DASHBOARD (e.g. https://your-project.vercel.app)"
fi

say "Writing settings to $DIR/.env (keep a copy of it: MASTER_KEY unlocks your stored data)"
[ -n "$(get_env MASTER_KEY)" ] || set_env MASTER_KEY "$(secret)"
[ -n "$(get_env ADMIN_API_KEY)" ] || set_env ADMIN_API_KEY "$(secret)"
if [ "$fresh" = 1 ]; then
  # Only for a new install: Postgres keeps the password it was first created with
  set_env POSTGRES_PASSWORD "$(secret)"
  set_env REDIS_PASSWORD "$(secret)"
  set_env PUBLIC_HOSTNAME "${DOMAIN:-localhost}"
  [ -n "$ANTHROPIC" ] || set_env LLM_PROVIDER fake
fi
if [ -n "$DOMAIN" ]; then
  set_env PUBLIC_HOSTNAME "$DOMAIN"
  # Only Caddy (HTTPS on 80/443) faces the internet; the API and dashboard ports stay on this server
  set_env CHANGEWATCH_BIND 127.0.0.1
  add_profile tls
fi
[ -z "$DASHBOARD" ] || set_env DASHBOARD_URL "$DASHBOARD"
if [ -n "$ANTHROPIC" ]; then
  set_env LLM_PROVIDER anthropic
  set_env ANTHROPIC_API_KEY "$ANTHROPIC"
fi
# A new install runs exactly the release it downloaded; later runs keep the version you chose
if [ "$fresh" = 1 ] || [ -z "$(get_env CHANGEWATCH_VERSION)" ]; then
  set_env CHANGEWATCH_VERSION "$VERSION"
fi
# Later commands need no extra flags: docker compose reads these from .env
if [ "$BUILD" = 1 ]; then
  set_env COMPOSE_FILE docker-compose.yml
else
  set_env COMPOSE_FILE docker-compose.yml:docker-compose.release.yml
fi

# ---------------------------------------------------------------------------------------------
# 4. Start, and wait until the API answers
start() {
  if [ "$BUILD" = 1 ]; then
    docker compose up -d --build --quiet-pull
  else
    docker compose pull --quiet
    docker compose up -d --no-build
  fi
}
# After every start: services read .env, so a changed setting restarts them
wait_healthy() {
  local _
  for _ in $(seq 1 120); do
    if docker compose exec -T api python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  fail "ChangeWatch did not become healthy. See: cd $DIR && docker compose logs api"
}
say "Starting ChangeWatch (the first start downloads the images: a few minutes)"
start
wait_healthy

cli() { docker compose exec -T api python -m app.cli "$@"; }

# ---------------------------------------------------------------------------------------------
# 5. Projects: the first one, and the demo shop
has_project() { cli list-projects 2>/dev/null | awk -F'|' -v n="$1" '{ gsub(/^ +| +$/, "", $2) } $2 == n { found = 1 } END { exit !found }'; }
create_project() {  # NAME [webhook]: prints the keys once
  if [ -n "${2:-}" ]; then cli create-project "$1" --slack-webhook "$2"; else cli create-project "$1"; fi
}

if [ -n "$PROJECT" ]; then
  if has_project "$PROJECT"; then
    printf '\nProject "%s" already exists: its keys were shown when it was created.\n' "$PROJECT"
  else
    say "Creating project \"$PROJECT\". Save these keys now: they are shown only once."
    create_project "$PROJECT" "$SLACK"
  fi
fi

if [ "$DEMO" = 1 ] && [ -z "$(get_env DEMO_INGEST_KEY)" ]; then
  if has_project demo-shop; then
    fail "A demo-shop project exists but DEMO_INGEST_KEY is not in .env. Put its ingest key there and run again."
  fi
  say "Creating the demo shop (share only its READ key with people trying the demo)"
  demo_out=$(create_project demo-shop)
  printf '%s\n' "$demo_out" | grep -E "Project ID|Read Key"
  demo_key=$(printf '%s\n' "$demo_out" | sed -n 's/^Ingest Key:[[:space:]]*\(cw_ingest_[^[:space:]]*\).*/\1/p')
  [ -n "$demo_key" ] || fail "Could not read the demo project's ingest key."
  set_env DEMO_INGEST_KEY "$demo_key"
fi
if [ "$DEMO" = 1 ]; then
  add_profile demo
  start
  wait_healthy
fi

# ---------------------------------------------------------------------------------------------
# 6. Where things are
if [ -n "$(get_env PUBLIC_HOSTNAME)" ] && [ "$(get_env PUBLIC_HOSTNAME)" != localhost ] \
    && [[ ",$(get_env COMPOSE_PROFILES)," == *",tls,"* ]]; then
  site="https://$(get_env PUBLIC_HOSTNAME)" api="https://$(get_env PUBLIC_HOSTNAME)"
else
  site="http://localhost:3000" api="http://localhost:8000"
fi
dashboard=$(get_env DASHBOARD_URL)
cat <<EOF

ChangeWatch is running.

  Dashboard:   ${dashboard:-$site}   (Settings: paste the project's read key)
  API address: $api   (for the SDK and the GitHub Action${dashboard:+, and API_URL on Vercel})

Next, in your app:
  1. pip install changewatcher, then changewatch.init(...) with the ingest key
     (README: "Instrument your app").
  2. Add the ChangeWatch step to your deploy workflow with the same ingest key.

Later:
  Update:      cd $DIR && docker compose pull && docker compose up -d
  New project: cd $DIR && docker compose exec api python -m app.cli create-project NAME
  Back up $DIR/.env somewhere safe: without MASTER_KEY your stored data cannot be read.
EOF
