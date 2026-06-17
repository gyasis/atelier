# maisi sidecar — cloud GPU cost rationale (Modal vs AWS)

Decision (2026-06-16): run MONAI MAISI 3D-CT on **Modal** (serverless A100), reached via
the local `maisi` cloud-passthrough sidecar — NOT a GPU EC2. Rationale below.

## Measured MAISI workload
- Model: 6.1 GB bundle (cached in a Modal Volume). Volume: 256×256×128 HU CT.
- Warm inference ~215 s on A100-40GB; first run +235 s one-time bundle download.

## AWS — every fee (GPU EC2 path)
| Fee | Rate | Bills when | Notes |
|---|---|---|---|
| GPU compute | g5.xlarge A10G 24GB $1.21/hr (spot ~$0.40); g6e.xlarge L40S 48GB ~$1.86/hr | running only | A10G 24GB risks OOM on MAISI → realistically L40S $1.86/hr |
| EBS root/model disk | gp3 $0.08/GB-mo | **even when STOPPED** | ~60GB (OS+CUDA+MONAI+6.1GB model) ≈ **$4.80/mo forever** |
| Custom AMI/snapshot | ~$0.05/GB-mo | always | ~30GB ≈ $1.5/mo |
| Elastic IP (idle) | ~$3.65/mo | reserved + stopped | avoidable |
| Egress | $0.09/GB | per pull | ~8MB CT ≈ negligible |
| Setup/ops | your hours | once + upkeep | build CUDA+MONAI+MAISI AMI, start/stop scripts, driver upkeep |
| "Forgot to stop" | $1.86/hr×720 | left running | forgotten weekend ≈ $89; month ≈ $1,340 |

Even with disciplined stop-after-use, AWS bleeds ~$5–6/mo standing (EBS+AMI) regardless of use.

## Modal — what we used
- Idle: **$0** (scales GPU to zero; verified 0 tasks/0 containers).
- Per CT warm: A100-40GB $2.10/hr × ~215s = **~$0.13**; first CT $0.263.
- Standing storage ~$0 (Volume, small, often within free credits). Setup: `modal deploy` (minutes). Nothing to stop.

## Scenarios
| Scenario | Modal | AWS (g6e + disciplined stop) |
|---|---|---|
| 10 CTs/mo | ~$1.30 (or $0 credits) | ~$1.11 GPU + $4.80 EBS + $1.5 AMI = ~$7.4 |
| 0 CTs (idle mo) | $0 | ~$6.30 standing |
| 200 CTs/mo | ~$26 | ~$22 GPU + $6.3 standing ≈ ~$28 |
| forgotten weekend | impossible | +$89 |

## Verdict
Occasional CT gen → **Modal** wins decisively ($0 idle, ~$0.13/CT, zero ops, no stop-risk,
A100-40GB on demand). AWS only competes at high steady volume, and still carries the EBS
standing cost + catastrophic forgotten-instance risk. (2D X-ray stays LOCAL on the Mac MPS
via radiogen — ~$0.)
