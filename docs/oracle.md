# Thinair on Oracle Cloud (Always Free)

Oracle's Always Free tier includes an Arm machine (Ampere A1) with up to 4 CPUs and 24 GB of
memory, plenty for Thinair. Thinair's images are built for Arm, so nothing special is needed.
These steps were written for Ubuntu images; Oracle Linux works for Tailscale but Docker's
install script does not support it.

## 1. Create the machine

In the Oracle Cloud console: **Compute > Instances > Create instance**.

- **Image:** Canonical Ubuntu 24.04. **Shape:** Ampere, `VM.Standard.A1.Flex` (for example
  2 OCPUs and 12 GB, still free).
- **SSH keys:** add your public key.
- **Use a reserved public IP.** Thinair's address is made from the machine's public IP
  (`https://129-146-10-20.sslip.io`). An ephemeral IP can change when the instance is stopped
  or re-created, and then your phone's address stops working until you run the installer again.
  Give the instance a *reserved* public IPv4 address instead (Oracle's documentation calls them
  "reserved public IPs"; you can also switch an existing instance's VNIC to one later).
- Optional, to have everything installed while the machine boots: open **Show advanced
  options > Management > Initialization script**, choose *Paste cloud-init script*, and paste:

```yaml
#cloud-config
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - usermod -aG docker ubuntu
  - su - ubuntu -c 'curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | sh > /home/ubuntu/thinair-install.log 2>&1'
```

This installs Docker and Thinair as the `ubuntu` user. The installer's output, including the
QR code, goes to `~/thinair-install.log`. Open the ports (step 2) before or right after creating
the machine: the installer waits five minutes for the certificate without a terminal.

## 2. Open ports 80 and 443 (once)

Thinair gets its HTTPS certificate from Let's Encrypt, which has to reach the machine on port 80
or 443. Oracle blocks both in its console, and only you can open them there: Networking >
Virtual cloud networks > your network > Security Lists > Default Security List > **Add Ingress
Rules**: source `0.0.0.0/0`, IP protocol TCP, destination port range `80,443`. If the instance
uses a Network Security Group, add the same rule there.

Nothing needs changing on the machine itself. Oracle's Ubuntu images do reject most incoming
traffic in iptables (`INPUT`), but Docker publishes Caddy's ports through its own rules, which
that chain never sees (tested on an Oracle Ubuntu VM, 2026-11-14). Only for your own services
outside Docker would you add `sudo iptables -I INPUT 1 -p tcp --dport <port> -m state --state NEW -j ACCEPT`.
On Oracle Linux (firewalld; untested) the installer offers to open 80 and 443.

## 3. Install (if you didn't use the cloud-init script)

SSH in (`ssh ubuntu@<public IP>`) and run:

```sh
curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | sh
```

It offers to install Docker for you (Docker's official script), sees the public IP and sets up
`https://<ip-with-dashes>.sslip.io`. If the console rule from step 2 is still missing, it says
exactly that and waits for you to add it (Caddy picks the certificate up by itself once the
ports open). It ends with a QR code: scan it with your phone's camera.

**Prefer to stay off the internet?** Use Tailscale instead: nothing is opened, so neither
firewall matters, but the phone needs the Tailscale app:

```sh
curl -fsSL https://raw.githubusercontent.com/SudeepSigdel/ThinAir/main/install.sh | THINAIR_PHONE=tailscale sh
```

It offers to install Tailscale, prints a link to sign this machine in to your tailnet, and sets
up HTTPS (`tailscale serve`; if HTTPS certificates are off for your tailnet it shows a link to
turn them on). Install Tailscale on your phone with the same account, then scan the QR code.

**Your own domain** instead of sslip.io: point its DNS (an A record) at the machine's public IP,
then run the installer with `THINAIR_PHONE=domain THINAIR_DOMAIN=thinair.example.com`.

## 4. Log in an agent

```sh
cd ~/thinair && docker compose exec thinair thinair auth claude --print-command
```

Run the printed `docker run -it ...` command over SSH and follow the agent's login. More agents:
`docker compose exec thinair thinair agent search`.

## Updating, and if the IP changes

`thinair doctor` tells you when a new version is out, and when the machine's public IP no
longer matches its sslip.io address. In both cases run the installer again: it keeps `.env` and
your data, pulls the new images, moves to the new address if the IP changed, and then shows a
new QR code to scan.

## What wasn't tested yet

This guide hasn't been run on a real Oracle machine. The installer's detection, certificate wait
and IP-change handling were tested in a container against a local Caddy (internal CA, no Let's
Encrypt traffic), and with shellcheck. The Arm image was tested under emulation. Please report
anything that doesn't match.
