# Remote Operations

These scripts provision and smoke-test a Debian remote host for the visible-browser
`resume-pilot` workflow.

They intentionally do not automate BOSS Zhipin login, captcha, SMS, scan-login, private
API calls, or real platform business actions.

## Provision

Run once as root:

```bash
ops/remote/bootstrap_debian.sh
```

The script creates the `resume-pilot` user, installs Chromium, TigerVNC, XFCE, fonts,
`pipx`, and `uv`, and configures a TigerVNC password. The password is stored root-readable
only at `/root/resume-pilot-vnc-password`.

## Sync Code

Expected remote app path:

```text
/home/resume-pilot/resume-pilot
```

## Start Runtime

```bash
ops/remote/start-vnc.sh
ops/remote/start-browser.sh
ops/remote/start-novnc.sh
```

VNC binds to `127.0.0.1:5901`. noVNC binds to `127.0.0.1:6080`. Chrome CDP binds to
`127.0.0.1:9222`.

Local tunnel, replacing the host with your own SSH endpoint:

```bash
ssh -L 6080:127.0.0.1:6080 -L 5901:127.0.0.1:5901 user@your-host.example
```

Open noVNC in a browser:

```text
http://127.0.0.1:6080/vnc.html
```

Do not open `http://127.0.0.1:5901` in a browser. Port `5901` is the raw VNC protocol,
not HTTP.

## Smoke

```bash
ops/remote/smoke.sh
RESUME_PILOT_PROBE_LLM=1 ops/remote/smoke.sh
```

The first smoke test validates the Python package, tests, lint, doctor, VNC, CDP, and a
deterministic dry-run fixture. The optional LLM probe validates Claude Code JSON output.
