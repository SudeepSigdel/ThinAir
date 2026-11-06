# Thinair on Oracle Cloud (Always Free)

Oracle's Always Free tier includes an Arm machine (Ampere A1) with up to 4 CPUs and 24 GB of
memory, plenty for Thinair. Thinair's images are built for Arm, so nothing special is needed.
These steps were written for Ubuntu images; Oracle Linux works for Tailscale but Docker's
install script does not support it.

## 1. Create the machine

In the Oracle Cloud console: **Compute > Instances > Create instance**.

- **Image:** Canonical Ubuntu 24.04. **Shape:** Ampere, `VM.Standard.A1.Flex` (for example
  2 OCPUs and 12 GB, still free).
- **SSH keys:** add your public key (you need SSH once, to get the login token).
- Optional, to have everything installed while the machine boots: open **Show advanced
  options > Management > Initialization script**, choose *Paste cloud-init script*, and paste:

```yaml
#cloud-config
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - usermod -aG docker ubuntu
  - su - ubuntu -c 'curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | THINAIR_PHONE=skip sh > /home/ubuntu/thinair-install.log 2>&1'
```

This installs Docker and Thinair as the `ubuntu` user and writes the installer's output
(including your login token) to `~/thinair-install.log`. Phone access is set up in step 3.

## 2. Install (if you didn't use the cloud-init script)

SSH in (`ssh ubuntu@<public IP>`) and run:

```sh
curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | sh
```

It offers to install Docker for you (Docker's official script), then asks how your phone will
reach Thinair. Your token is printed at the end and saved in `~/thinair/.env`.

## 3. Phone access

**Tailscale (recommended on Oracle).** Nothing is opened to the internet, so Oracle's two
firewalls don't matter. Run the installer again with:

```sh
curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | THINAIR_PHONE=tailscale sh
```

It offers to install Tailscale, prints a link to sign this machine in to your tailnet, and sets
up HTTPS (`tailscale serve`). In the Tailscale admin console (DNS page) turn on **MagicDNS** and
**HTTPS Certificates** first. Install Tailscale on your phone with the same account, then scan
the QR code the installer shows.

**Your own domain.** Point the domain's DNS (an A record) at the machine's public IP, then
run the installer with `THINAIR_PHONE=domain THINAIR_DOMAIN=thinair.example.com`. Oracle blocks
ports 80 and 443 twice, and both must be opened:

1. **In the console:** Networking > Virtual cloud networks > your network > Security Lists >
   Default Security List > **Add Ingress Rules**: source `0.0.0.0/0`, IP protocol TCP,
   destination port `80`. Add a second rule for `443`.
2. **On the machine:** Oracle's Ubuntu images reject everything but SSH in iptables. The
   installer notices it is on Oracle Cloud and offers to add the two rules (and saves them with
   `netfilter-persistent`). By hand:

   ```sh
   sudo iptables -I INPUT 1 -p tcp --dport 80 -m state --state NEW -j ACCEPT
   sudo iptables -I INPUT 1 -p tcp --dport 443 -m state --state NEW -j ACCEPT
   sudo netfilter-persistent save
   ```

## 4. Log in an agent

```sh
cd ~/thinair && docker compose exec thinair thinair auth claude --print-command
```

Run the printed `docker run -it ...` command over SSH and follow the agent's login. More agents:
`docker compose exec thinair thinair agent search`.

## Updating

`thinair doctor` tells you when a new version is out. To update, run the installer again: it
keeps `.env` and your data and pulls the new images.

## What wasn't tested yet

This guide hasn't been run on a real Oracle machine. The installer's Docker, Tailscale and
firewall steps were checked on paper and with shellcheck. The Arm image itself was tested under
emulation. Please report anything that doesn't match.
