# CI/CD — GitHub Actions setup (Phase 10 Part D)

The pipeline (`.github/workflows/release.yml`) is the **factory** half of the update story; the
**central server** is the dispatcher and the **supervisor** is the installer. They compose:

```
  git tag v1.2.3  ──▶  GitHub Actions  ──▶  GitHub Release (binaries + manifest.json + SHA256SUMS)
                          builds + signs        │                + GHCR central image
                                                ▼
                       you: admin publish-release  +  start-rollout (canary)
                                                ▼
                       central hands the signed URL+sha256 in each heartbeat reply
                                                ▼
                       the edge supervisor verifies sha256 → atomic swap → health-gate → rollback
```

Edges **never** pull "latest" from GitHub directly — central stays the version authority so every
rollout is staged, health-gated, and auto-rolled-back. CI just produces trustworthy artifacts.

---

## What the workflow does

| Trigger | Jobs | Ships? |
|---|---|---|
| **push to any branch / PR** | `test` → `build` (3 platforms, uploads artifacts) | No — artifacts only, for smoke-testing |
| **git tag `v*`** (or manual `workflow_dispatch`) | `test` → `build` → `release` + `central-image` | **Yes** — publishes a GitHub Release + pushes the central image to GHCR |

`build` runs a matrix on **native runners**, one per target:

| Runner | Produces |
|---|---|
| `ubuntu-latest` | `wisp-edge-linux-amd64`, `wisp-supervisor-linux-amd64`, `.deb`, `.rpm` |
| `ubuntu-24.04-arm` | `wisp-edge-linux-arm64`, `wisp-supervisor-linux-arm64`, `.deb`, `.rpm` (Raspberry Pi / ARM) |
| `windows-latest` | `wisp-edge-win-amd64.exe`, `wisp-supervisor-win-amd64.exe`, `wisp-edge-setup-<ver>.exe` (Inno) |

The version is `git describe` stamped into `src/wisp/_buildinfo.py` at build time, so commit →
artifact → running version → rollout decision is **one string** (`apps/daemon/main.py --version`
prints it; the supervisor compares it to central's target).

---

## One-time setup

### 1. Enable Actions + the ARM runner
- Settings ▸ Actions ▸ General — allow Actions to run. The default `GITHUB_TOKEN` already has the
  `contents: write` (Release) + `packages: write` (GHCR) the workflow declares.
- `ubuntu-24.04-arm` is a **GitHub-hosted arm64 Linux runner**. It's available on public repos and
  on paid plans for private repos. If your plan doesn't include it, drop that matrix row (you lose
  the prebuilt Pi/arm64 artifact; everything else still ships).

### 2. Secrets (Settings ▸ Secrets and variables ▸ Actions)
Nothing is **required** to build — forks and PRs build unsigned. For a trustworthy fleet rollout,
add signing:

| Secret | For | Notes |
|---|---|---|
| `MINISIGN_KEY` | Linux binary signing | The minisign private key. The workflow's sign step no-ops when unset; wire the real `minisign -S` call there and pin the **public** key in `deploy/install-edge.sh`. |
| `WINDOWS_CERT_PFX` / `WINDOWS_CERT_PASSWORD` | Authenticode (the `.exe` + installer) | base64-encoded code-signing cert + its password. Decode to a file in CI and pass `SignTool=signtool sign /f ... /p ... $f` on the `iscc` line; unsigned installers trip SmartScreen on a fleet. |

`GITHUB_TOKEN` is injected automatically — you do **not** create it. It's what pushes the central
image to `ghcr.io/<owner>/<repo>/central`.

> Why signing matters here: the supervisor already refuses to swap in a binary whose **sha256**
> doesn't match the directive (the published checksum is the supply-chain gate). Signing adds
> authenticity on top — it's what makes the `curl|sh` Linux install and the Windows installer
> trustworthy, not just integrity-checked.

### 3. (First release only) make the GHCR package public, if you want anonymous `docker pull`
After the first tag pushes the image, GitHub creates a private package under your account. To let a
GCE VM pull without auth, set the `central` package to **Public** (Packages ▸ central ▸ Settings),
or keep it private and `docker login ghcr.io` on the VM with a PAT.

---

## Cutting a release

```bash
# 1. make sure main is green (CI ran on the push).
# 2. tag with semver and push the tag — that is the ONLY thing that ships.
git tag v0.11.0
git push origin v0.11.0
```

CI builds all three platforms, signs (if secrets are set), publishes a **GitHub Release** with the
binaries + installer + `.deb`/`.rpm` + `SHA256SUMS` + `manifest.json`, and pushes the central image.

`manifest.json` maps each platform to its **agent** binary URL + sha256 (installers/packages are
excluded — they aren't supervisor-pullable):

```json
{
  "version": "0.11.0",
  "artifacts": {
    "linux-amd64": { "url": "https://github.com/.../wisp-edge-linux-amd64", "sha256": "..." },
    "linux-arm64": { "url": "https://github.com/.../wisp-edge-linux-arm64", "sha256": "..." },
    "win-amd64":   { "url": "https://github.com/.../wisp-edge-win-amd64.exe", "sha256": "..." }
  }
}
```

## Feeding a release into central (staged rollout)

Register the release with central, then roll it out canary-first (see `README.md` §"Fleet deploy"
and `central/admin.py`):

```bash
# pull the URLs + sha256 straight from the published manifest:
curl -fsSL https://github.com/<owner>/ping_tool/releases/download/v0.11.0/manifest.json

# tell central about the version + its artifacts (one --artifact per platform):
central-entrypoint admin publish-release --version 0.11.0 \
  --artifact linux-amd64 https://github.com/.../wisp-edge-linux-amd64 <sha256> \
  --artifact linux-arm64 https://github.com/.../wisp-edge-linux-arm64 <sha256>

# roll out to one org, canary first; central promotes fleet-wide only when the canary comes back
# healthy on the target, and auto-halts if it doesn't:
central-entrypoint admin start-rollout  --tenant ispA --version 0.11.0 --canary edge-a1
central-entrypoint admin rollout-status --tenant ispA
```

(`central-entrypoint admin ...` is the container form; on a bare host it's
`PYTHONPATH=src python -m wisp.central.admin ...`.)

---

## Local dry-run before you trust CI

The pieces are runnable locally (you just need the real toolchains):

```bash
# the test gate (what CI runs first):
python -m unittest discover -s tests

# the agent binary (needs pyinstaller in a venv):
pip install -r requirements.txt pyinstaller
pyinstaller --clean -y deploy/wisp-edge.spec        # -> dist/wisp-edge

# the .deb (needs nfpm):
VERSION=0.11.0 PKG_ARCH=amd64 PKG_PLAT=linux-amd64 \
  nfpm pkg -p deb -f deploy/nfpm.yaml -t out/        # after the binary is in out/

# the central image:
docker build -f deploy/central.Dockerfile -t wisp-central:dev .
```

The Windows installer (`deploy/wisp-edge.iss`, Inno Setup) needs a Windows host with Inno Setup 6;
CI builds it on `windows-latest`.

---

## Gotchas / honesty

- **Tag, don't branch, to ship.** A push to `main` builds artifacts but publishes nothing. Only a
  `v*` tag (or a manual `workflow_dispatch` on a tag) cuts a Release. This is deliberate — the
  cardinal rule is never auto-shipping every commit to a live fleet.
- **GHCR names must be lowercase.** `${{ github.repository }}` already is here; if you fork to an
  org with capitals, lowercase the image tag.
- **Unsigned is a landmine on Windows.** Until the Authenticode cert is in secrets, the installer
  is unsigned and SmartScreen will warn on every fleet box. The sha256 still protects integrity;
  signing protects authenticity + UX.
- **The arm64 runner is the one external dependency.** If `ubuntu-24.04-arm` isn't on your plan,
  remove that matrix row or build arm64 with QEMU on `ubuntu-latest` (slower).
