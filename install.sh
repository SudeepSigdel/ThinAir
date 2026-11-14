#!/bin/sh
# Thinair installer. Needs only Docker (with the Compose plugin) and curl or wget.
#
#   curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | sh
#
# Server (default): downloads docker-compose.yml into ~/thinair, writes a .env with a fresh
# admin token (first time only), downloads the ready-made images and starts Thinair.
# On a fresh Linux machine it offers to install Docker (and Tailscale) first, asking each time.
# Runner:  ... | sh -s -- --runner   sets up a remote runner in ~/thinair-runner instead.
# Run it again to upgrade: your .env is kept, the images are updated.
#
# Settings (environment variables, all optional):
#   THINAIR_DIR       install folder           (default ~/thinair, runner: ~/thinair-runner)
#   THINAIR_PORT      port on this machine     (default 8080; first install only)
#   THINAIR_VERSION   image tag                (default latest; e.g. 0.2.0 or edge)
#   THINAIR_PHONE     how phones reach it (first install, or whenever given):
#                     auto (default) = detect: a public IP gets https://<ip>.sslip.io, else
#                     Tailscale is offered | public | tailscale | funnel | domain | skip
#   THINAIR_DOMAIN    your domain, for THINAIR_PHONE=domain
#   THINAIR_PUBLIC_IP this machine's public IPv4 (default: looked up once at ipv4.icanhazip.com)
#   THINAIR_SERVER_URL, THINAIR_RUNNER_TOKEN   runner only; asked for when missing
#   THINAIR_INSTALL_DOCKER, THINAIR_INSTALL_TAILSCALE, THINAIR_OPEN_FIREWALL   yes | no:
#                     answer those questions in advance (default: ask; no terminal = no)
set -eu

RAW_URL="${THINAIR_RAW_URL:-https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main}"
IMAGE_NAME="${THINAIR_IMAGE:-ghcr.io/sudeepsigdel/thinair}"
MODE=server
case "${1:-}" in
  --runner) MODE=runner ;;
  "") ;;
  -h | --help)
    sed -n '2,23p' "$0" 2>/dev/null || echo "See https://github.com/SudeepSigdel/ThinAir"
    exit 0
    ;;
  *) printf 'Unknown option: %s (use --runner or nothing)\n' "$1" >&2; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() {
  printf '\nError: %s\n' "$*" >&2
  exit 1
}

fetch() { # url file (file:// works too, for offline installs and tests)
  case "$1" in
    file://*)
      cp "${1#file://}" "$2"
      return
      ;;
  esac
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$2" "$1"
  else
    die "Neither curl nor wget is installed."
  fi
}

has_tty() { # a terminal we can really open (-r alone is not enough: it can exist but fail)
  (exec </dev/tty) 2>/dev/null
}

ask() { # prompt var: read from the terminal even when this script comes through a pipe
  has_tty || die "Set $2 (no terminal to ask for it)."
  printf '%s' "$1" >/dev/tty
  # shellcheck disable=SC2034 # used through eval below
  IFS= read -r answer </dev/tty || die "Set $2."
  eval "$2=\$answer"
}

confirm() { # question VAR: true if VAR says yes, else ask (no terminal or no answer = no)
  preset=""
  eval "preset=\${$2:-}"
  case "$preset" in
    y | Y | yes | YES | true | 1) return 0 ;;
    n | N | no | NO | false | 0) return 1 ;;
  esac
  has_tty || return 1
  reply=""
  ask "$1 [y/N] " reply
  case "$reply" in y | Y | yes | YES) return 0 ;; *) return 1 ;; esac
}

as_root() { # run a command as root (directly, or through sudo)
  if [ "$(id -u)" = 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "This step needs root and sudo is not installed: $*"
  fi
}

on_oracle_cloud() { # Oracle Cloud sets this asset tag on every instance (cloud-init uses it too)
  grep -qs OracleCloud.com /sys/class/dmi/id/chassis_asset_tag
}

install_docker() {
  [ "$(uname -s)" = Linux ] ||
    die "Docker is not installed. Install Docker Desktop (https://docs.docker.com/get-docker/) and run this again."
  say "Docker is not installed on this machine."
  confirm "Install it now with Docker's official script (https://get.docker.com)?" THINAIR_INSTALL_DOCKER ||
    die "Install Docker (https://docs.docker.com/engine/install/) and run this again."
  tmp="$(mktemp)"
  fetch https://get.docker.com "$tmp" || die "Could not download Docker's install script."
  as_root sh "$tmp" || die "Docker's install script failed (see above)."
  rm -f "$tmp"
  if [ "$(id -u)" != 0 ]; then
    as_root usermod -aG docker "$(id -un)" ||
      say "Could not add you to the docker group; use sudo for docker commands."
  fi
}

use_sudo_for_docker() { # this run only: you're in the docker group from your next login on
  # The function shadows the docker command; sudo runs the real binary (that's the point).
  # shellcheck disable=SC2032,SC2033,SC2317
  docker() { sudo docker "$@"; }
  say "Using sudo for Docker during this install. Log out and back in to use docker without sudo."
}

install_tailscale() {
  [ "$(uname -s)" = Linux ] ||
    die "Tailscale is not installed here. Install it (https://tailscale.com/download), sign in, then run this again."
  confirm "Tailscale is not installed. Install it now with its official script (https://tailscale.com/install.sh)?" THINAIR_INSTALL_TAILSCALE ||
    die "Install Tailscale (https://tailscale.com/download), run 'sudo tailscale up', then run this again."
  tmp="$(mktemp)"
  fetch https://tailscale.com/install.sh "$tmp" || die "Could not download Tailscale's install script."
  as_root sh "$tmp" || die "Tailscale's install script failed (see above)."
  rm -f "$tmp"
}

oracle_firewall() { # Oracle Cloud blocks 80/443 twice: in its console and on the machine itself
  on_oracle_cloud || return 0
  say ""
  say "This is an Oracle Cloud machine. Ports 80 and 443 are blocked in two places:"
  say "  1. Oracle's console: Networking > Virtual cloud networks > (your network) > Security"
  say "     Lists > Default Security List > Add Ingress Rules: source 0.0.0.0/0, TCP, port 80;"
  say "     then the same for port 443. Only you can do this one."
  say "  2. This machine's own firewall."
  if confirm "Open ports 80 and 443 in this machine's firewall now?" THINAIR_OPEN_FIREWALL; then
    if command -v firewall-cmd >/dev/null 2>&1; then # Oracle Linux
      as_root firewall-cmd --permanent --add-service=http --add-service=https &&
        as_root firewall-cmd --reload
    elif command -v iptables >/dev/null 2>&1; then # Oracle's Ubuntu images
      for p in 443 80; do
        as_root iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null ||
          as_root iptables -I INPUT 1 -p tcp --dport "$p" -m state --state NEW -j ACCEPT
      done
      if command -v netfilter-persistent >/dev/null 2>&1; then
        as_root netfilter-persistent save
      else
        say "Opened until the next reboot (netfilter-persistent is not installed to keep it)."
      fi
    else
      say "No firewall tool found here; nothing to change on this machine."
    fi
  else
    say "Skipped. Without both steps phones can't reach Thinair through your domain."
  fi
}

set_env() { # KEY VALUE: set one line in .env, keeping everything else
  { grep -v "^$1=" .env || true; } >.env.tmp
  printf '%s=%s\n' "$1" "$2" >>.env.tmp
  mv .env.tmp .env
}

unset_env() { # KEY: remove that line from .env
  { grep -v "^$1=" .env || true; } >.env.tmp
  mv .env.tmp .env
}

env_value() { # KEY: its value in .env (empty if unset)
  sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1
}

has_subcommand() { # args...: does the running image know this command? (older pinned images)
  docker compose exec -T thinair thinair "$@" --help >/dev/null 2>&1
}

wait_healthy() {
  i=0
  until docker compose exec -T thinair python -c \
    "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz')" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -le 45 ] || die "Thinair did not come up. See: cd $DIR && docker compose logs thinair"
    sleep 2
  done
}

# ---- phone access -----------------------------------------------------------------------------
# Default (THINAIR_PHONE=auto): detect instead of asking. Tailscale already set up with HTTPS ->
# use it (private). A public IPv4 -> Caddy serves https://<ip-with-dashes>.sslip.io with a Let's
# Encrypt certificate (no account, no domain). Only when neither works is anything asked.
NET_KIND="" NET_IP="" NET_HOST="" NET_CLOUD="" ADDRESS_CHANGED=""

dmi() { cat "/sys/class/dmi/id/$1" 2>/dev/null || true; }

given_public_ip() { # set by hand now, or on an earlier run (then saved in .env)
  printf '%s' "${THINAIR_PUBLIC_IP:-$(env_value THINAIR_PUBLIC_IP)}"
}

detect_network() { # sets NET_KIND (direct|cloud|nat|none|unknown), NET_IP, NET_HOST, NET_CLOUD
  NET_KIND=unknown NET_IP="" NET_HOST="" NET_CLOUD=""
  has_subcommand net detect || return 0 # an older pinned image
  local_ip=""
  if command -v ip >/dev/null 2>&1; then # the source address for internet traffic (no packet sent)
    local_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)"
  fi
  if ! out="$(docker compose exec -T thinair thinair net detect --local-ip "$local_ip" \
    --dmi-vendor "$(dmi sys_vendor)" --dmi-product "$(dmi product_name)" \
    --dmi-asset "$(dmi chassis_asset_tag)" --public-ip "$(given_public_ip)")"; then
    die "Could not check this machine's network (see above). THINAIR_PUBLIC_IP must be a public IPv4 address."
  fi
  NET_KIND="$(printf '%s\n' "$out" | sed -n 's/^KIND=//p')"
  NET_IP="$(printf '%s\n' "$out" | sed -n 's/^PUBLIC_IP=//p')"
  NET_HOST="$(printf '%s\n' "$out" | sed -n 's/^HOST=//p')"
  NET_CLOUD="$(printf '%s\n' "$out" | sed -n 's/^CLOUD=//p')"
}

wait_https() { # host seconds: until https://host answers with a valid certificate (via Caddy)
  waited=0
  hinted=""
  say "Getting an HTTPS certificate for $1 from Let's Encrypt (usually under a minute)..."
  until docker compose exec -T thinair thinair net https-check "$1" --via caddy:443 >/dev/null 2>&1; do
    if [ "$waited" -ge 45 ] && [ -z "$hinted" ]; then
      hinted=1
      say ""
      docker compose exec -T thinair thinair net firewall-hint --cloud "$NET_CLOUD" || true
      say "Waiting for that (Caddy keeps retrying by itself; Ctrl-C to stop)..."
    fi
    [ "$waited" -lt "$2" ] || return 1
    sleep 5
    waited=$((waited + 5))
    [ $((waited % 60)) -ne 0 ] || say "  still waiting (${waited}s)"
  done
  say "HTTPS is ready: https://$1"
}

use_public_host() { # host: point Caddy and the app at it
  set_env THINAIR_DOMAIN "$1"
  set_env THINAIR_PUBLIC_URL "https://$1"
  set_env COMPOSE_PROFILES https # so every later 'docker compose up' starts Caddy too
  # A hand-set address is kept, so later checks (and re-runs) trust it instead of a lookup.
  [ -z "$(given_public_ip)" ] || set_env THINAIR_PUBLIC_IP "$(given_public_ip)"
}

drop_public_host() {
  unset_env THINAIR_DOMAIN
  unset_env THINAIR_PUBLIC_URL
  unset_env COMPOSE_PROFILES
  unset_env THINAIR_PUBLIC_IP
  docker compose --profile https rm -sf caddy >/dev/null 2>&1 || true
}

setup_public() { # port: https://<ip>.sslip.io through Caddy; returns 1 if it can't work here
  host="$NET_HOST"
  use_public_host "$host"
  if ! docker compose up -d --remove-orphans; then
    say "Caddy could not start. Is another web server using ports 80 or 443 on this machine?"
    drop_public_host
    docker compose up -d --remove-orphans
    return 1
  fi
  wait_healthy
  [ "$NET_CLOUD" != oracle ] || oracle_firewall
  case "$NET_KIND" in
    direct) limit=180 ;;
    cloud) if has_tty; then limit=900; else limit=300; fi ;;
    *) limit=90 ;; # nat: nobody may have opened anything for this machine
  esac
  wait_https "$host" "$limit" && return 0
  if [ "$NET_KIND" = nat ]; then
    if has_tty && confirm "Keep waiting while you open ports 80 and 443?" THINAIR_KEEP_WAITING &&
      wait_https "$host" 900; then
      return 0
    fi
    say "No HTTPS for $host: this machine isn't reachable from the internet on ports 80/443."
    drop_public_host
    docker compose up -d --remove-orphans
    return 1
  fi
  say "HTTPS isn't ready yet. Caddy keeps trying; 'thinair doctor' shows what's missing."
  return 0
}

tailscale_cert_domain() { # the tailnet's HTTPS name, empty while certificates are off
  tailscale status --json 2>/dev/null | docker compose exec -T thinair python -c \
    "import json,sys; print((json.load(sys.stdin).get('CertDomains') or [''])[0])" 2>/dev/null
}

setup_tailscale() { # port serve|funnel
  command -v tailscale >/dev/null 2>&1 || install_tailscale
  if ! tailscale status >/dev/null 2>&1; then
    say "Connecting this machine to your tailnet: open the link below and sign in."
    as_root tailscale up || die "Tailscale is installed but not connected. Run 'sudo tailscale up' first."
  fi
  # `tailscale serve` / `funnel` show a consent link when HTTPS certificates (and, for funnel,
  # the funnel attribute) still need enabling. Changing them needs root or a Tailscale operator.
  if ! tailscale "$2" --bg --https=443 "http://127.0.0.1:$1" &&
    ! as_root tailscale "$2" --bg --https=443 "http://127.0.0.1:$1"; then
    say "'tailscale $2' failed. Try: sudo tailscale $2 --bg --https=443 http://127.0.0.1:$1"
    return 0
  fi
  host="$(tailscale_cert_domain)"
  if [ -z "$host" ]; then
    say "HTTPS certificates are off for your tailnet. Turn on 'HTTPS Certificates' at"
    say "https://login.tailscale.com/admin/dns, then run this again with THINAIR_PHONE=$2."
    return 0
  fi
  set_env THINAIR_PUBLIC_URL "https://$host"
  if [ "$2" = funnel ]; then
    say "Anyone can open https://$host (through Tailscale Funnel); nothing to install on the phone."
  else
    say "Phones on your tailnet can open https://$host"
  fi
}

setup_domain() {
  domain="${THINAIR_DOMAIN:-}"
  [ -n "$domain" ] || ask "Your domain for Thinair (its DNS must point at this machine): " domain
  printf '%s' "$domain" | grep -Eq '^([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$|^localhost$' ||
    die "That doesn't look like a domain name: $domain"
  set_env THINAIR_DOMAIN "$domain"
  set_env THINAIR_PUBLIC_URL "https://$domain"
  set_env COMPOSE_PROFILES https # so every later 'docker compose up' starts Caddy too
  say "Caddy will get a certificate for $domain. Ports 80 and 443 must be open to the internet."
  oracle_firewall
}

choose_tunnel() { # port: the one question, asked only when detection found no way in
  if ! has_tty; then
    say "Phones can't reach this machine directly. Run this again with THINAIR_PHONE=funnel"
    say "(Tailscale Funnel, a public link) or THINAIR_PHONE=tailscale (private) to set that up."
    return 0
  fi
  say ""
  say "Phones can't reach this machine directly (it's behind a router or your provider's NAT)."
  say "  1) Tailscale Funnel: a public https link, nothing to install on the phone (recommended)"
  say "  2) Tailscale: private, the phone needs the Tailscale app"
  say "  3) My own domain (its DNS points here and ports 80/443 are forwarded to this machine)"
  say "  4) Skip for now"
  ask "Choose 1-4 [1]: " choice
  case "$choice" in
    "" | 1) step "Setting up Tailscale Funnel" && setup_tailscale "$1" funnel ;;
    2) step "Setting up Tailscale HTTPS" && setup_tailscale "$1" serve ;;
    3) step "Setting up HTTPS for your domain" && setup_domain ;;
    *) return 0 ;;
  esac
}

auto_phone() { # port
  if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1 &&
    [ -n "$(tailscale_cert_domain)" ]; then
    step "Tailscale with HTTPS is set up on this machine: using it (private to your tailnet)"
    setup_tailscale "$1" serve
    return 0
  fi
  step "Checking how phones can reach this machine"
  detect_network
  case "$NET_KIND" in
    direct | cloud | nat)
      say "Public IP $NET_IP: setting up https://$NET_HOST"
      setup_public "$1" && return 0
      ;;
    none) say "This machine has no public IPv4 address (carrier NAT, IPv6-only or offline)." ;;
  esac
  choose_tunnel "$1"
}

phone_access() { # port
  choice="${THINAIR_PHONE:-auto}"
  case "$choice" in
    auto) auto_phone "$1" ;;
    public)
      step "Setting up HTTPS on this machine's public IP"
      detect_network
      [ -n "$NET_HOST" ] || die "No public IPv4 address found. Set THINAIR_PUBLIC_IP=<address>."
      setup_public "$1" || true
      ;;
    1 | tailscale) step "Setting up Tailscale HTTPS" && setup_tailscale "$1" serve ;;
    funnel) step "Setting up Tailscale Funnel" && setup_tailscale "$1" funnel ;;
    2 | domain) step "Setting up HTTPS for your domain" && setup_domain ;;
    3 | skip) return 0 ;;
    *) die "Unknown THINAIR_PHONE: $choice (auto, public, tailscale, funnel, domain or skip)" ;;
  esac
  docker compose up -d --remove-orphans # picks up the new settings
  wait_healthy
}

refresh_public_address() { # on re-runs: follow a changed public IP (sslip.io names only)
  old="$(env_value THINAIR_DOMAIN)"
  case "$old" in *.sslip.io) ;; *) return 0 ;; esac
  detect_network
  if [ -z "$NET_HOST" ]; then
    [ "$NET_KIND" = unknown ] || say "Could not look up this machine's public IP; keeping https://$old"
    return 0
  fi
  [ "$NET_HOST" != "$old" ] || return 0
  step "This machine's public IP changed: moving Thinair to https://$NET_HOST"
  use_public_host "$NET_HOST"
  docker compose up -d --remove-orphans
  wait_healthy
  wait_https "$NET_HOST" 180 || say "HTTPS isn't ready yet; 'thinair doctor' shows what's missing."
  ADDRESS_CHANGED=1
}

random_token() { # like `thinair init`: thinair_ + 32 random bytes, URL-safe base64
  printf 'thinair_%s' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
}

# Everything runs inside main, called on the last line: if a `curl | sh` download breaks off
# halfway, the shell has only read definitions and runs nothing.
main() {
# ---- checks -----------------------------------------------------------------------------------
step "Checking Docker"
command -v docker >/dev/null 2>&1 || install_docker
if ! docker info >/dev/null 2>&1; then
  # shellcheck disable=SC2033 # the real docker binary, through sudo
  if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    use_sudo_for_docker
  else
    die "Docker is installed but not reachable. Is it running? On Linux either run this with sudo, or
add yourself to the docker group (sudo usermod -aG docker \"\$USER\"), log out and back in, and
run this again."
  fi
fi
docker compose version >/dev/null 2>&1 ||
  die "The Docker Compose plugin is missing. See https://docs.docker.com/compose/install/linux/"
say "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), $(docker compose version --short 2>/dev/null || echo 'compose')"

if [ "$MODE" = runner ]; then
  DIR="${THINAIR_DIR:-$HOME/thinair-runner}"
  COMPOSE_FILE_NAME=docker-compose.runner.yml
else
  DIR="${THINAIR_DIR:-$HOME/thinair}"
  COMPOSE_FILE_NAME=docker-compose.yml
fi
mkdir -p "$DIR"
cd "$DIR"
umask 077 # settings files hold secrets

# ---- files ------------------------------------------------------------------------------------
step "Downloading $COMPOSE_FILE_NAME into $DIR"
fetch "$RAW_URL/$COMPOSE_FILE_NAME" "$COMPOSE_FILE_NAME.new" ||
  die "Could not download $RAW_URL/$COMPOSE_FILE_NAME"
if [ -f "$COMPOSE_FILE_NAME" ] && ! cmp -s "$COMPOSE_FILE_NAME" "$COMPOSE_FILE_NAME.new"; then
  cp "$COMPOSE_FILE_NAME" "$COMPOSE_FILE_NAME.bak"
  say "Your previous $COMPOSE_FILE_NAME was saved as $COMPOSE_FILE_NAME.bak"
fi
mv "$COMPOSE_FILE_NAME.new" "$COMPOSE_FILE_NAME"
chmod 644 "$COMPOSE_FILE_NAME"

NEW_TOKEN=""
if [ "$MODE" = server ]; then
  if [ -f .env ]; then
    say "Keeping your settings in $DIR/.env"
  else
    NEW_TOKEN="$(random_token)"
    cat >.env <<EOF
# Created by the Thinair installer. Every option: https://github.com/SudeepSigdel/ThinAir/blob/main/.env.example
# Your login token for the web app (keep it secret).
THINAIR_ADMIN_TOKEN=$NEW_TOKEN
# Who can connect: 127.0.0.1 = only this machine (use a tunnel or the https profile for phones).
THINAIR_BIND=127.0.0.1
THINAIR_PORT=${THINAIR_PORT:-8080}
THINAIR_MAX_CONCURRENT_JOBS=1
EOF
    say "Created $DIR/.env with a new admin token"
  fi
else
  if [ -f .env.runner ]; then
    say "Keeping your runner settings in $DIR/.env.runner"
  else
    server="${THINAIR_SERVER_URL:-}"
    token="${THINAIR_RUNNER_TOKEN:-}"
    [ -n "$server" ] || ask "Thinair server address (e.g. https://thinair.example.com): " server
    if [ -z "$token" ]; then
      say "Create a runner secret on the server with:  thinair runner add <name>"
      ask "Runner secret (thinair_runner_...): " token
    fi
    case "$token" in thinair_runner_*) ;; *) die "That is not a runner secret (it starts with thinair_runner_)." ;; esac
    case "$server" in https://* | http://localhost* | http://127.0.0.1*) ;;
      *) die "The server address must start with https:// (the secret is sent with every request)." ;;
    esac
    printf 'THINAIR_SERVER_URL=%s\nTHINAIR_RUNNER_TOKEN=%s\n' "$server" "$token" >.env.runner
    say "Created $DIR/.env.runner"
  fi
fi

# ---- images and start -------------------------------------------------------------------------
[ -n "${THINAIR_VERSION:-}" ] && export THINAIR_VERSION
IMAGE="$IMAGE_NAME:${THINAIR_VERSION:-latest}"
step "Downloading the images (first time: about 100 MB)"
if ! docker compose -f "$COMPOSE_FILE_NAME" pull; then
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    say "Could not download updates; using the image already on this machine ($IMAGE)."
  else
    die "Could not download $IMAGE. Check your internet connection and run this again."
  fi
fi

step "Starting Thinair"
docker compose -f "$COMPOSE_FILE_NAME" up -d --remove-orphans

if [ "$MODE" = runner ]; then
  i=0
  until docker compose -f "$COMPOSE_FILE_NAME" logs runner 2>/dev/null | grep -q "Runner started"; do
    i=$((i + 1))
    [ "$i" -le 30 ] || die "The runner did not start. See: cd $DIR && docker compose -f $COMPOSE_FILE_NAME logs runner"
    sleep 2
  done
  say ""
  say "Runner running. It connects to the server and waits for profile jobs."
  say "  Logs:      cd $DIR && docker compose -f $COMPOSE_FILE_NAME logs -f runner"
  say "  Upgrade:   run this installer again with --runner"
  exit 0
fi

wait_healthy
port="$(env_value THINAIR_PORT)"
# Set up on the first install (or whenever THINAIR_PHONE is given); on later runs only follow a
# changed public IP. Existing Tailscale or own-domain setups are never touched.
if [ -n "$NEW_TOKEN" ] || [ -n "${THINAIR_PHONE:-}" ]; then
  phone_access "${port:-8080}"
else
  refresh_public_address
fi

# Older images (pinned with THINAIR_VERSION) may not have these commands: then skip them.
if has_subcommand images; then
  # In the background: the phone can be set up meanwhile (the first job would wait for them).
  docker compose exec -d thinair thinair images >/dev/null 2>&1 ||
    say "Could not start preparing the sandbox images; the first job will do it."
fi
if has_subcommand doctor; then
  # Only problems are shown here; `thinair doctor` prints the full list any time.
  if ! report="$(docker compose exec -T thinair thinair doctor 2>&1)"; then
    step "Something needs your attention"
    printf '%s\n' "$report"
  fi
fi

bind="$(env_value THINAIR_BIND)"
public="$(env_value THINAIR_PUBLIC_URL)"
domain="$(env_value THINAIR_DOMAIN)"
show_qr=""
if [ -n "$public" ] && { [ -n "$NEW_TOKEN" ] || [ -n "$ADDRESS_CHANGED" ] || [ -n "${THINAIR_PHONE:-}" ]; }; then
  show_qr=1
fi
say ""
if [ -n "$public" ]; then
  say "Thinair is running at $public"
else
  say "Thinair is running."
  say ""
  say "  Open:      http://localhost:${port:-8080}   (on this machine)"
fi
if [ -n "$show_qr" ] && has_subcommand qr; then
  say ""
  [ -z "$ADDRESS_CHANGED" ] || say "The address changed, so sign your phone in again (one scan):"
  if docker compose exec -T thinair thinair qr --help 2>/dev/null | grep -q -- --ttl; then
    docker compose exec -T thinair thinair qr --ttl 30 || true
  else
    docker compose exec -T thinair thinair qr || true
  fi
  say "(New code any time: cd $DIR && docker compose exec thinair thinair qr)"
fi
if [ -n "$domain" ] && [ -n "$public" ]; then
  say ""
  say "Anyone on the internet can open this address. Signing in needs a one-time QR code"
  say "('thinair qr') or your token. To keep it private, run this again with THINAIR_PHONE=tailscale,"
  say "or allow only your own IP address to ports 80 and 443 in your provider's firewall."
fi
say ""
if [ -n "$NEW_TOKEN" ] && [ -z "$public" ]; then
  say "  Token:     $NEW_TOKEN"
  say "             (log in with it; it is also saved in $DIR/.env)"
else
  say "  Token:     THINAIR_ADMIN_TOKEN in $DIR/.env (to log in from a browser)"
fi
say ""
say "Next steps:"
if [ -z "$public" ] && [ "${bind:-127.0.0.1}" = 127.0.0.1 ]; then
  say "  - Phone access: run this again with THINAIR_PHONE=auto (public IP), funnel or tailscale,"
  say "    or domain with THINAIR_DOMAIN=... Details:"
  say "    https://github.com/SudeepSigdel/ThinAir#using-it-from-your-phone"
fi
say "  - Log in an agent:  cd $DIR && docker compose exec thinair thinair auth claude --print-command"
say "  - Upgrade later:    run this installer again"
}

main "$@"
